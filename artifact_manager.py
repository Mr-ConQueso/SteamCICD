import json
import logging
from pathlib import Path
from typing import Any

from uploader import upload_artifacts

logger = logging.getLogger(__name__)

# Store metadata in the downloads folder
METADATA_FILE = Path("downloads/metadata.json")


def load_metadata() -> dict[str, Any]:
    if METADATA_FILE.exists():
        try:
            with open(METADATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            logger.warning("Corrupted metadata file. Starting fresh.")
    return {}


def save_metadata(metadata: dict[str, Any]) -> None:
    METADATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(METADATA_FILE, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=4)


def is_already_downloaded(build_target_id: str, build_number: int) -> bool:
    metadata = load_metadata()
    build_id = f"{build_target_id}_{build_number}"
    build_entry = metadata.get(build_id)
    return isinstance(build_entry, dict) and build_entry.get("status") == "uploaded"


def register_and_process_artifact(
        build_target_id: str,
        build_number: int,
        artifact_paths: list[str],
        build_info: dict[str, Any]
) -> None:
    metadata = load_metadata()
    build_id = f"{build_target_id}_{build_number}"

    target_parts = build_target_id.split("-")
    os_name = target_parts[1] if len(target_parts) > 1 else "unknown"
    arch = "64-bit" if "64" in build_target_id else "32-bit" if "32" in build_target_id else "unknown"

    successful_artifacts = [
        Path(path_str)
        for path_str in artifact_paths
        if not path_str.startswith("FAILED:")
    ]

    metadata[build_id] = {
        "build_target_id": build_target_id,
        "build_number": build_number,
        "artifacts": [str(path) for path in successful_artifacts],
        "os": os_name,
        "architecture": arch,
        "build_date": build_info.get("created", "unknown"),
        "status": "downloaded",
    }
    save_metadata(metadata)
    logger.info("Registered metadata for build %s", build_id)

    if not successful_artifacts:
        logger.warning("No successful artifacts found for build %s", build_id)
        metadata[build_id]["status"] = "no_artifacts"
        save_metadata(metadata)
        return

    try:
        upload_artifacts(build_id, successful_artifacts)
    except Exception:
        logger.exception(
            "SteamCMD upload did not complete for build %s; keeping zip and extracted files for retry",
            build_id,
        )
        metadata[build_id]["status"] = "upload_failed"
        save_metadata(metadata)
        raise

    metadata[build_id]["status"] = "uploaded"
    metadata[build_id]["artifacts"] = []
    save_metadata(metadata)
    logger.info("Build %s uploaded successfully and local artifact files were cleaned up", build_id)