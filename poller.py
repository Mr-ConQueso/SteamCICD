import logging
import threading
import time

from artifact_manager import is_already_downloaded, register_and_process_artifact
from config import DOWNLOAD_DIR, POLL_INTERVAL_SECONDS
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


def process_new_builds() -> list[dict]:
    if not poll_cycle_lock.acquire(blocking=False):
        logger.warning("Previous poll cycle is still running; skipping overlapping cycle")
        return []

    newly_downloaded = []

    try:
        logger.info("Starting poll cycle")
        targets = list_project_build_targets()
        logger.info("Fetched %d build target(s) from Unity", len(targets))

        for target in targets:
            build_target_id = _build_target_id_of(target)
            build_target_name = target.get("name") or target.get("targetName") or "<unnamed>"

            logger.info("Discovered build target candidate: id=%s name=%s keys=%s", build_target_id, build_target_name, list(target.keys()))

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
                key = (build_target_id, build_number)

                if build_number < 0:
                    logger.warning(
                        "Skipping build with no valid build number for target %s: keys=%s",
                        build_target_id,
                        list(build.keys()),
                    )
                    continue

                if key in processed_build_numbers or is_already_downloaded(build_target_id, build_number):
                    logger.info("Skipping already processed build %s #%s", build_target_id, build_number)
                    processed_build_numbers.add(key)
                    continue

                logger.info("Checking build %s #%s for artifacts", build_target_id, build_number)
                logger.debug(
                    "Build list payload for %s #%s keys=%s links=%s projectVersion=%s",
                    build_target_id,
                    build_number,
                    list(build.keys()),
                    build.get("links"),
                    build.get("projectVersion"),
                )

                artifact_source = build

                try:
                    full_build = get_build(build_target_id, build_number)
                    logger.debug(
                        "Fetched full build details for %s #%s keys=%s links=%s projectVersion=%s",
                        build_target_id,
                        build_number,
                        list(full_build.keys()),
                        full_build.get("links"),
                        full_build.get("projectVersion"),
                    )
                except Exception:
                    logger.exception("Failed to fetch full build details for %s #%s; continuing with list payload", build_target_id, build_number)
                    full_build = None

                filenames = resolve_artifact_filenames(artifact_source)
                download_url = get_primary_download_url(artifact_source)

                logger.info(
                    "Artifact resolution for %s #%s returned %d candidate(s): %s | primary_download=%s",
                    build_target_id,
                    build_number,
                    len(filenames),
                    filenames,
                    download_url,
                )

                if not filenames and not download_url:
                    logger.info(
                        "No downloadable artifacts found for %s #%s; list links=%s list projectVersion=%s",
                        build_target_id,
                        build_number,
                        build.get("links"),
                        build.get("projectVersion"),
                    )
                    processed_build_numbers.add(key)
                    continue

                downloaded_files = []
                build_download_dir = DOWNLOAD_DIR / f"{build_target_id}_{build_number}"

                if download_url:
                    primary_filename = "primary_artifact.zip"
                    for f in filenames:
                        if f.endswith(".zip") and not f.startswith("/api"):
                            primary_filename = f
                            break

                    logger.info("Downloading primary artifact for %s #%s", build_target_id, build_number)
                    try:
                        path = download_artifact(
                            build_target_id,
                            build_number,
                            primary_filename,
                            build_download_dir,
                            download_url=download_url,
                        )
                        downloaded_files.append(str(path))
                        logger.info("Downloaded primary artifact to %s", path)
                    except Exception:
                        logger.exception(
                            "Failed to download primary artifact for %s #%s",
                            build_target_id,
                            build_number,
                        )
                        downloaded_files.append("FAILED:primary_artifact")
                else:
                    for filename in filenames:
                        if filename in ("Build Reports", ".ZIP file") or filename.startswith("/api"):
                            continue

                        logger.info("Downloading artifact %s for %s #%s", filename, build_target_id, build_number)
                        try:
                            path = download_artifact(
                                build_target_id,
                                build_number,
                                filename,
                                build_download_dir,
                            )
                            downloaded_files.append(str(path))
                            logger.info("Downloaded artifact %s to %s", filename, path)
                        except Exception:
                            logger.exception(
                                "Failed to download artifact %s for %s #%s",
                                filename,
                                build_target_id,
                                build_number,
                            )
                            downloaded_files.append(f"FAILED:{filename}")

                processed_build_numbers.add(key)

                successful_downloads = [item for item in downloaded_files if not item.startswith("FAILED:")]
                if not successful_downloads:
                    logger.warning(
                        "No valid downloadable artifact files were produced for %s #%s; skipping uploader",
                        build_target_id,
                        build_number,
                    )
                    continue

                register_and_process_artifact(build_target_id, build_number, downloaded_files, artifact_source)

                newly_downloaded.append(
                    {
                        "build_target_id": build_target_id,
                        "build_number": build_number,
                        "artifacts": downloaded_files,
                    }
                )

                logger.info(
                    "Finished processing build %s #%s; %d artifact result(s)",
                    build_target_id,
                    build_number,
                    len(downloaded_files),
                )

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