import json
import logging
from pathlib import Path
from typing import Any

from config import steamworks_sdk_is_ready
from uploader import upload_artifacts

logger = logging.getLogger(__name__)

METADATA_FILE = Path("downloads/metadata.json")


def load_metadata() -> dict[str, list[str]]:
    if METADATA_FILE.exists():
        try:
            with open(METADATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            logger.warning("Corrupted metadata file. Starting fresh.")
            data = {}
    else:
        data = {}

    downloaded = data.get("downloaded")
    uploaded = data.get("uploaded")

    return {
        "downloaded": downloaded if isinstance(downloaded, list) else [],
        "uploaded": uploaded if isinstance(uploaded, list) else [],
    }


def save_metadata(metadata: dict[str, list[str]]) -> None:
    normalized = {
        "downloaded": sorted(set(metadata.get("downloaded", []))),
        "uploaded": sorted(set(metadata.get("uploaded", []))),
    }
    METADATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(METADATA_FILE, "w", encoding="utf-8") as f:
        json.dump(normalized, f, indent=4)


def _build_id(build_target_id: str, build_number: int) -> str:
    return f"{build_target_id}_{build_number}"


def mark_downloaded(build_id: str) -> None:
    metadata = load_metadata()
    if build_id not in metadata["downloaded"]:
        metadata["downloaded"].append(build_id)
        save_metadata(metadata)
        logger.info("Recorded downloaded build %s", build_id)


def mark_uploaded(build_id: str) -> None:
    metadata = load_metadata()
    if build_id not in metadata["uploaded"]:
        metadata["uploaded"].append(build_id)
        save_metadata(metadata)
        logger.info("Recorded uploaded build %s", build_id)


def is_already_downloaded(build_target_id: str, build_number: int) -> bool:
    build_id = _build_id(build_target_id, build_number)
    metadata = load_metadata()
    return build_id in metadata["downloaded"] or build_id in metadata["uploaded"]


def is_already_uploaded(build_target_id: str, build_number: int) -> bool:
    build_id = _build_id(build_target_id, build_number)
    metadata = load_metadata()
    return build_id in metadata["uploaded"]


def has_uploaded_build(build_id: str) -> bool:
    metadata = load_metadata()
    return build_id in metadata["uploaded"]


def needs_artifact_processing(build_target_id: str, build_number: int) -> bool:
    return not is_already_uploaded(build_target_id, build_number)


def register_and_process_artifact(
    build_target_id: str,
    build_number: int,
    artifact_paths: list[str],
    build_info: dict[str, Any],
) -> None:
    build_id = _build_id(build_target_id, build_number)

    successful_artifacts = [
        Path(path_str)
        for path_str in artifact_paths
        if not path_str.startswith("FAILED:")
    ]

    if not successful_artifacts:
        logger.warning("No successful artifacts found for build %s", build_id)
        return

    if not steamworks_sdk_is_ready():
        logger.warning("Steamworks SDK is not ready yet; pausing upload for build %s", build_id)
        return

    mark_downloaded(build_id)

    try:
        upload_artifacts(build_id, successful_artifacts)
    except Exception:
        logger.exception(
            "SteamCMD upload did not complete for build %s; keeping zip and extracted files for retry",
            build_id,
        )
        raise

    mark_uploaded(build_id)
    logger.info("Build %s uploaded successfully and local artifact files were cleaned up", build_id)