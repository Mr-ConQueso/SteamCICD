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
    list_builds,
    list_project_build_targets,
    resolve_artifacts,
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


def _refresh_build_artifact_source(project: Any, build_target_id: str, build_number: int, fallback: dict[str, Any], api_key: str) -> dict[str, Any]:
    try:
        full_build = get_build(project.unity_org_id, project.unity_project_id, build_target_id, build_number, api_key)
        if isinstance(full_build, dict):
            return full_build
    except Exception:
        pass
    return fallback

def _download_build_artifacts(project: Any, build_target_id: str, build_number: int, artifact_source: dict[str, Any], api_key: str) -> list[str]:
    artifacts = resolve_artifacts(artifact_source)

    if not artifacts:
        return []

    build_download_dir = DOWNLOAD_DIR / f"{project.id}_{build_target_id}_{build_number}"
    downloaded_files: list[str] = []

    for artifact in artifacts:
        filename = artifact["name"]
        download_url = artifact.get("href")

        if filename in ("Build Reports", ".ZIP file") or filename.startswith("/api"):
            continue

        path = download_artifact(
            project.unity_org_id,
            project.unity_project_id,
            build_target_id,
            build_number,
            filename,
            build_download_dir,
            api_key,
            download_url=download_url,
        )
        downloaded_files.append(str(path))

    return downloaded_files


def process_new_builds() -> list[dict]:
    if not poll_cycle_lock.acquire(blocking=False):
        logger.warning("Previous poll cycle is still running; skipping overlapping cycle")
        return []

    from models import Project, GlobalSettings
    from config import POLL_INTERVAL_SECONDS
    
    api_key_setting = GlobalSettings.get_or_none(key="UNITY_API_KEY")
    api_key = api_key_setting.value if api_key_setting else None

    if not api_key:
        logger.warning("UNITY_API_KEY is not set in Global Settings; skipping poll cycle")
        poll_cycle_lock.release()
        return []

    newly_downloaded = []

    try:
        if not steamworks_sdk_is_ready():
            logger.warning("Steamworks SDK is not ready yet; build uploads are paused")
            return []

        logger.info("Starting poll cycle")
        
        projects = Project.select().where(Project.enabled == True)
        for project in projects:
            logger.info("Polling project: %s", project.name)
            try:
                targets = list_project_build_targets(project.unity_org_id, project.unity_project_id, api_key)
                logger.info("Project %s: Fetched %d build target(s) from Unity", project.name, len(targets))

                for target in targets:
                    build_target_id = _build_target_id_of(target)
                    build_target_name = target.get("name") or target.get("targetName") or "<unnamed>"

                    if not build_target_id:
                        continue

                    try:
                        builds = list_builds(project.unity_org_id, project.unity_project_id, build_target_id, api_key)
                    except Exception:
                        logger.exception("Failed to list builds for build target %s in project %s", build_target_id, project.name)
                        continue

                    builds.sort(key=_build_number_of)

                    for build in builds:
                        build_number = _build_number_of(build)
                        if build_number < 0:
                            continue

                        # Unique build ID for this project and target
                        full_build_id = f"{project.id}_{build_target_id}_{build_number}"
                        key = (project.id, build_target_id, build_number)

                        if has_uploaded_build(full_build_id):
                            processed_build_numbers.add(key)
                            continue

                        if key in processed_build_numbers:
                            logger.info("Build %s was seen before and is still pending", full_build_id)
                        else:
                            logger.info("Build %s is new to the poller", full_build_id)

                        if not needs_artifact_processing(full_build_id):
                            processed_build_numbers.add(key)
                            continue

                        artifact_source = _refresh_build_artifact_source(project, build_target_id, build_number, build, api_key)

                        try:
                            downloaded_files = _download_build_artifacts(project, build_target_id, build_number, artifact_source, api_key)
                        except Exception as exc:
                            # Re-poll logic could be improved, but keeping it simple for now
                            logger.exception("Artifact download failure for %s", full_build_id)
                            continue

                        successful_downloads = [item for item in downloaded_files if not item.startswith("FAILED:")]
                        if not successful_downloads:
                            continue

                        logger.info("Build %s produced %d artifact(s); starting upload", full_build_id, len(successful_downloads))

                        try:
                            # We need project's depots config
                            depots_config = []
                            for depot in project.depots:
                                depots_config.append({
                                    "os": depot.os,
                                    "depot_id": depot.depot_id,
                                    "name": f"{depot.os} Depot",
                                    "contentroot": f"../output/staging/{depot.os.lower()}"
                                })

                            register_and_process_artifact(
                                project_id=str(project.id),
                                build_target_id=build_target_id,
                                build_number=build_number,
                                artifact_paths=downloaded_files,
                                build_info=artifact_source,
                                appid=project.steam_app_id,
                                desc=project.steam_desc,
                                setlive=project.steam_set_live or "",
                                depots_config=depots_config
                            )
                            processed_build_numbers.add(key)
                            newly_downloaded.append({
                                "project": project.name,
                                "build_target_id": build_target_id,
                                "build_number": build_number,
                                "artifacts": downloaded_files,
                            })
                        except Exception:
                            logger.exception("Artifact processing failed for %s", full_build_id)
            except Exception:
                logger.exception("Failed to poll project %s", project.name)

        logger.info("Poll cycle complete; processed %d new build(s)", len(newly_downloaded))
        return newly_downloaded
    finally:
        poll_cycle_lock.release()

def needs_artifact_processing(full_build_id: str) -> bool:
    from artifact_manager import load_metadata
    metadata = load_metadata()
    return full_build_id not in metadata["uploaded"]


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