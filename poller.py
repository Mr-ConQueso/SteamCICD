import logging
import threading
import time
from typing import Any

from requests import HTTPError

from artifact_manager import has_uploaded_build, needs_artifact_processing, register_and_process_artifact
from config import DOWNLOAD_DIR, POLL_INTERVAL_SECONDS, steamworks_sdk_is_ready
from unity_client import (
    download_artifact,
    get_build,
    get_primary_download_url,
    list_builds,
    list_project_build_targets,
    resolve_artifact_filenames,
)

logger = logging.getLogger(__name__)

processed_build_numbers: set[tuple[str, int]] = set()
poller_thread_started = False
poll_cycle_lock = threading.Lock()


def _build_number_of(item: dict) -> int:
    for key in ("build", "number", "buildNumber", "build_number", "id"):
        value = item.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)

    logger.debug("Could not determine build number from keys=%s payload=%s", list(item.keys()), item)
    return -1


def _build_target_id_of(target: dict) -> str | None:
    value = target.get("id") or target.get("buildTargetId") or target.get("buildtargetid")
    return str(value) if value is not None else None


def _download_retry_reason(exc: Exception) -> str | None:
    if isinstance(exc, HTTPError):
        response = getattr(exc, "response", None)
        if response is not None:
            if response.status_code == 401:
                return "unauthorized"
            if response.status_code == 403:
                return "forbidden"
            if response.status_code == 404:
                return "not_found"
    return None


def _refresh_build_artifact_source(build_target_id: str, build_number: int, fallback: dict[str, Any]) -> dict[str, Any]:
    try:
        full_build = get_build(build_target_id, build_number)
        if isinstance(full_build, dict):
            return full_build
    except Exception:
        logger.exception(
            "Failed to refresh build details for %s_%s; falling back to list payload",
            build_target_id,
            build_number,
        )
    return fallback


def _download_build_artifacts(build_target_id: str, build_number: int, artifact_source: dict[str, Any]) -> list[str]:
    filenames = resolve_artifact_filenames(artifact_source)
    download_url = get_primary_download_url(artifact_source)

    if not filenames and not download_url:
        return []

    build_download_dir = DOWNLOAD_DIR / f"{build_target_id}_{build_number}"
    downloaded_files: list[str] = []

    # Prefer the canonical API download path. It uses the API key auth and follows redirects.
    if filenames:
        for filename in filenames:
            if filename in ("Build Reports", ".ZIP file") or filename.startswith("/api"):
                continue

            path = download_artifact(
                build_target_id,
                build_number,
                filename,
                build_download_dir,
                download_url=None,
            )
            downloaded_files.append(str(path))
        return downloaded_files

    # Fallback: if no filenames are discoverable, use the primary download URL only.
    if download_url:
        primary_filename = "primary_artifact.zip"
        path = download_artifact(
            build_target_id,
            build_number,
            primary_filename,
            build_download_dir,
            download_url=download_url,
        )
        downloaded_files.append(str(path))

    return downloaded_files


def process_new_builds() -> list[dict]:
    if not poll_cycle_lock.acquire(blocking=False):
        logger.warning("Previous poll cycle is still running; skipping overlapping cycle")
        return []

    newly_downloaded = []

    try:
        if not steamworks_sdk_is_ready():
            logger.warning("Steamworks SDK is not ready yet; build uploads are paused")
            return []

        logger.info("Starting poll cycle")
        targets = list_project_build_targets()
        logger.info("Fetched %d build target(s) from Unity", len(targets))

        for target in targets:
            build_target_id = _build_target_id_of(target)
            build_target_name = target.get("name") or target.get("targetName") or "<unnamed>"

            logger.info(
                "Discovered build target candidate: id=%s name=%s keys=%s",
                build_target_id,
                build_target_name,
                list(target.keys()),
            )

            if not build_target_id:
                logger.warning("Skipping build target with no usable id: %s", target)
                continue

            logger.info("Polling build target %s (%s)", build_target_id, build_target_name)

            try:
                builds = list_builds(build_target_id)
            except Exception:
                logger.exception("Failed to list builds for build target %s", build_target_id)
                continue

            logger.info("Found %d build(s) for build target %s", len(builds), build_target_id)
            builds.sort(key=_build_number_of)

            for build in builds:
                build_number = _build_number_of(build)
                if build_number < 0:
                    logger.warning(
                        "Skipping build with no valid build number for target %s: keys=%s",
                        build_target_id,
                        list(build.keys()),
                    )
                    continue

                build_id = f"{build_target_id}_{build_number}"
                key = (build_target_id, build_number)

                if has_uploaded_build(build_id):
                    logger.info("Build %s is already uploaded; skipping", build_id)
                    processed_build_numbers.add(key)
                    continue

                if key in processed_build_numbers:
                    logger.info("Build %s was seen before and is still pending; checking again", build_id)
                else:
                    logger.info("Build %s is new to the poller; checking for artifacts", build_id)

                if not needs_artifact_processing(build_target_id, build_number):
                    logger.info("Build %s is already fully processed in metadata", build_id)
                    processed_build_numbers.add(key)
                    continue

                artifact_source = _refresh_build_artifact_source(build_target_id, build_number, build)

                # First try: canonical API download path.
                try:
                    downloaded_files = _download_build_artifacts(build_target_id, build_number, artifact_source)
                except Exception as exc:
                    reason = _download_retry_reason(exc)
                    if reason:
                        logger.info(
                            "Artifact for %s is currently not accessible via the download API (%s); refreshing build metadata and retrying",
                            build_id,
                            reason,
                        )
                        refreshed_source = _refresh_build_artifact_source(build_target_id, build_number, build)
                        try:
                            downloaded_files = _download_build_artifacts(build_target_id, build_number, refreshed_source)
                            artifact_source = refreshed_source
                        except Exception as retry_exc:
                            retry_reason = _download_retry_reason(retry_exc)
                            if retry_reason:
                                logger.info(
                                    "Artifact for %s is still not accessible (%s); will try again later",
                                    build_id,
                                    retry_reason,
                                )
                                continue
                            logger.exception("Unexpected artifact download failure for %s after refresh", build_id)
                            continue
                    else:
                        logger.exception("Unexpected artifact download failure for %s", build_id)
                        continue

                successful_downloads = [item for item in downloaded_files if not item.startswith("FAILED:")]
                if not successful_downloads:
                    logger.warning("Build %s produced no usable downloadable artifacts; skipping upload", build_id)
                    continue

                logger.info("Build %s produced %d artifact(s); starting upload", build_id, len(successful_downloads))

                try:
                    register_and_process_artifact(build_target_id, build_number, downloaded_files, artifact_source)
                    processed_build_numbers.add(key)
                    newly_downloaded.append(
                        {
                            "build_target_id": build_target_id,
                            "build_number": build_number,
                            "artifacts": downloaded_files,
                        }
                    )
                    logger.info("Build %s processed successfully", build_id)
                except Exception:
                    logger.exception("Artifact processing failed for %s; will retry on next poll", build_id)

        logger.info("Poll cycle complete; processed %d new build(s)", len(newly_downloaded))
        return newly_downloaded
    finally:
        poll_cycle_lock.release()


def poll_loop() -> None:
    logger.info("Poll loop started with interval=%ss", POLL_INTERVAL_SECONDS)
    while True:
        try:
            process_new_builds()
        except Exception:
            logger.exception("Unhandled error during poll cycle")
        time.sleep(POLL_INTERVAL_SECONDS)


def start_poller_once() -> None:
    global poller_thread_started
    if poller_thread_started:
        logger.debug("Poller already running; start request ignored")
        return

    logger.info("Starting background poller thread")
    thread = threading.Thread(target=poll_loop, daemon=True, name="unity-poller")
    thread.start()
    poller_thread_started = True