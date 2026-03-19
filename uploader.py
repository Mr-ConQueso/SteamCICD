import logging
import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

STEAMWORKS_ROOT = Path("steamworks_sdk_164")
SCRIPTS_DIR = STEAMWORKS_ROOT / "scripts"
STAGING_ROOT = STEAMWORKS_ROOT / "output" / "staging"
APP_BUILD_VDF = SCRIPTS_DIR / "app_4457910.vdf"
STEAMCMD_PATH = STEAMWORKS_ROOT / "builder_linux" / "steamcmd.sh"

WINDOWS_DEPOT_VDF = SCRIPTS_DIR / "depot_4457912.vdf"
LINUX_DEPOT_VDF = SCRIPTS_DIR / "depot_4457913.vdf"

WINDOWS_STAGE_DIR = STAGING_ROOT / "windows"
LINUX_STAGE_DIR = STAGING_ROOT / "linux"


def _detect_platform(build_id: str, artifact_path: Path) -> str:
    haystack = f"{build_id} {artifact_path.name}".lower()

    if "windows" in haystack or "win" in haystack:
        return "windows"
    if "linux" in haystack:
        return "linux"

    raise ValueError(f"Could not determine platform for artifact: build_id={build_id}, file={artifact_path}")


def _stage_dir_for_platform(platform_name: str) -> Path:
    if platform_name == "windows":
        return WINDOWS_STAGE_DIR
    if platform_name == "linux":
        return LINUX_STAGE_DIR
    raise ValueError(f"Unsupported platform: {platform_name}")


def _vdf_path_for_platform(platform_name: str) -> Path:
    if platform_name == "windows":
        return WINDOWS_DEPOT_VDF
    if platform_name == "linux":
        return LINUX_DEPOT_VDF
    raise ValueError(f"Unsupported platform: {platform_name}")


def _prepare_stage_dir(stage_dir: Path) -> Path:
    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    stage_dir.mkdir(parents=True, exist_ok=True)
    return stage_dir


def _extract_archive(artifact_path: Path, destination_dir: Path) -> None:
    if artifact_path.suffix.lower() != ".zip":
        raise ValueError(f"Unsupported artifact format for staging: {artifact_path.name}")

    with zipfile.ZipFile(artifact_path, "r") as archive:
        archive.extractall(destination_dir)


def _relative_to_scripts(path: Path) -> str:
    relative_path = Path(os.path.relpath(path.resolve(), SCRIPTS_DIR.resolve()))
    return relative_path.as_posix()


def _rewrite_vdf_contentroot(vdf_path: Path, contentroot: Path) -> None:
    if not vdf_path.exists():
        raise FileNotFoundError(f"VDF file not found: {vdf_path}")

    original = vdf_path.read_text(encoding="utf-8")
    relative_root = _relative_to_scripts(contentroot)

    updated, replacements = re.subn(
        r'("contentroot"\s*")([^"]*)(")',
        rf'\1{relative_root}\3',
        original,
        count=1,
    )

    if replacements != 1:
        raise ValueError(f'Could not update "contentroot" in {vdf_path}')

    vdf_path.write_text(updated, encoding="utf-8")


def _stage_artifact(build_id: str, artifact_path: Path) -> tuple[str, Path]:
    if not artifact_path.exists():
        raise FileNotFoundError(f"Artifact does not exist: {artifact_path}")

    platform_name = _detect_platform(build_id, artifact_path)
    stage_dir = _prepare_stage_dir(_stage_dir_for_platform(platform_name))

    logger.info(
        "Detected %s artifact for build %s; extracting %s to %s",
        platform_name,
        build_id,
        artifact_path,
        stage_dir,
    )
    _extract_archive(artifact_path, stage_dir)

    vdf_path = _vdf_path_for_platform(platform_name)
    _rewrite_vdf_contentroot(vdf_path, stage_dir)

    logger.info(
        "Prepared %s build for Steamworks. Updated %s contentroot -> %s",
        platform_name,
        vdf_path,
        stage_dir,
    )
    return platform_name, stage_dir


def _prompt_for_steam_guard_code() -> str:
    print()
    print("Steam Guard code required for SteamCMD upload.")
    print("Enter the current Steam Guard code from the Steam app.")
    code = input("Steam Guard code: ").strip()

    if not code:
        raise RuntimeError("Steam Guard code was not provided")

    return code


def _run_steamcmd_upload() -> None:
    username = os.getenv("STEAMCMD_USERNAME", "").strip()
    password = os.getenv("STEAMCMD_PASSWORD", "").strip()

    if not username or not password:
        raise RuntimeError("STEAMCMD_USERNAME and STEAMCMD_PASSWORD must be set")

    if not STEAMCMD_PATH.exists():
        raise FileNotFoundError(f"SteamCMD not found at {STEAMCMD_PATH}")

    if not APP_BUILD_VDF.exists():
        raise FileNotFoundError(f"App build VDF not found at {APP_BUILD_VDF}")

    command = [
        str(STEAMCMD_PATH.resolve()),
        "+login",
        username,
        password,
        "+run_app_build",
        str(APP_BUILD_VDF.resolve()),
        "+quit",
    ]

    logger.info("Starting SteamCMD depot upload with %s", APP_BUILD_VDF)
    logger.info("Using SteamCMD script at %s", STEAMCMD_PATH)

    result = subprocess.run(
        command,
        cwd=str(STEAMCMD_PATH.parent.resolve()),
        text=True,
        capture_output=True,
        check=False,
    )

    if result.stdout:
        logger.info("SteamCMD stdout:\n%s", result.stdout)

    if result.stderr:
        logger.warning("SteamCMD stderr:\n%s", result.stderr)

    if result.returncode != 0:
        raise RuntimeError(f"SteamCMD upload failed with exit code {result.returncode}")

    logger.info("SteamCMD upload completed successfully")


def _cleanup_files(artifact_paths: list[Path], staged_dirs: set[Path]) -> None:
    for artifact_path in artifact_paths:
        try:
            if artifact_path.exists():
                artifact_path.unlink()
                logger.info("Deleted uploaded artifact archive %s", artifact_path)
        except Exception:
            logger.exception("Failed to delete uploaded artifact archive %s", artifact_path)

    for stage_dir in staged_dirs:
        try:
            if stage_dir.exists():
                shutil.rmtree(stage_dir)
                logger.info("Deleted extracted staging directory %s", stage_dir)
        except Exception:
            logger.exception("Failed to delete extracted staging directory %s", stage_dir)


def upload_artifacts(build_id: str, artifact_paths: list[Path]) -> None:
    """
    Stages the given build artifacts into flat platform directories, uploads them to
    Steam depots using steamcmd.sh and the app build VDF, and only then removes both
    the original zip files and extracted files.
    """
    logger.info("Preparing %d artifact(s) for Steam depot upload for build %s", len(artifact_paths), build_id)

    staged_dirs: set[Path] = set()
    staged_platforms: set[str] = set()

    for artifact_path in artifact_paths:
        platform_name, stage_dir = _stage_artifact(build_id, artifact_path)
        staged_platforms.add(platform_name)
        staged_dirs.add(stage_dir)

    if not staged_platforms:
        raise RuntimeError(f"No artifacts were staged for build {build_id}")

    logger.info("Staged platform(s) for build %s: %s", build_id, ", ".join(sorted(staged_platforms)))

    try:
        _run_steamcmd_upload()
    except Exception:
        logger.exception(
            "SteamCMD upload failed for build %s; preserving zip and extracted files for retry",
            build_id,
        )
        raise

    _cleanup_files(artifact_paths, staged_dirs)
    logger.info("Finished Steam depot upload and cleanup for build %s", build_id)