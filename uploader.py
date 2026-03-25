import json
import logging
import os
import queue
import shutil
import subprocess
import threading
import zipfile
from pathlib import Path

from config import STEAMCMD_PATH, STEAMCMD_CONFIG_FILE, STEAMWORKS_ROOT, STEAMWORKS_CONTENT_BUILDER, \
    steamworks_sdk_is_ready

logger = logging.getLogger(__name__)


def _ensure_steamcmd_executable() -> None:
    builder_linux = STEAMCMD_PATH.parent
    if not builder_linux.exists():
        return

    for path in builder_linux.rglob("*"):
        try:
            if path.is_file():
                current_mode = path.stat().st_mode
                path.chmod(current_mode | 0o111)
        except Exception:
            logger.exception("Failed to mark SteamCMD helper executable: %s", path)

    if STEAMCMD_PATH.exists():
        logger.info("Marked SteamCMD tree as executable under: %s", builder_linux)


SCRIPTS_DIR = STEAMWORKS_CONTENT_BUILDER / "scripts"
STAGING_ROOT = STEAMWORKS_CONTENT_BUILDER / "output" / "staging"
APP_BUILD_VDF = SCRIPTS_DIR / "app_4457910.vdf"
SDK_METADATA_FILE = STEAMWORKS_ROOT / ".steamworks_upload_state.json"

def ensure_steamworks_root() -> None:
    STEAMWORKS_ROOT.mkdir(parents=True, exist_ok=True)

def get_steamworks_status() -> dict[str, bool | str]:
    cached_login = False
    cached_username = ""
    if SDK_METADATA_FILE.exists():
        try:
            data = json.loads(SDK_METADATA_FILE.read_text(encoding="utf-8"))
            cached_login = bool(data.get("cached_login", False))
            cached_username = str(data.get("cached_username", ""))
        except Exception:
            pass

    return {
        "sdk_present": STEAMWORKS_CONTENT_BUILDER.exists(),
        "steamcmd_config_present": STEAMCMD_CONFIG_FILE.exists(),
        "ready": steamworks_sdk_is_ready(),
        "cached_login": cached_login,
        "cached_username": cached_username,
    }


def _save_sdk_state(cached_login: bool | None = None, username: str | None = None) -> None:
    ensure_steamworks_root()
    status = get_steamworks_status()
    if cached_login is not None:
        status["cached_login"] = cached_login
    if username is not None:
        status["cached_username"] = username
    SDK_METADATA_FILE.write_text(json.dumps(status, indent=2), encoding="utf-8")


def _clear_directory_contents(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for item in root.iterdir():
        if item.is_dir() and not item.is_symlink():
            shutil.rmtree(item)
        else:
            item.unlink()


def extract_steamworks_sdk(zip_path: Path) -> Path:
    if not zip_path.exists():
        raise FileNotFoundError(f"Steamworks SDK archive not found: {zip_path}")

    ensure_steamworks_root()

    # Preserve login state before clearing the directory
    status = get_steamworks_status()
    cached_login = bool(status.get("cached_login", False))
    cached_username = str(status.get("cached_username", ""))

    _clear_directory_contents(STEAMWORKS_ROOT)

    with zipfile.ZipFile(zip_path, "r") as archive:
        archive.extractall(STEAMWORKS_ROOT)

    content_builder = None
    for candidate in STEAMWORKS_ROOT.rglob("ContentBuilder"):
        if candidate.is_dir() and candidate.as_posix().endswith("tools/ContentBuilder"):
            content_builder = candidate
            break

    if content_builder is None:
        raise FileNotFoundError("Could not find tools/ContentBuilder in the uploaded Steamworks SDK archive")

    if content_builder.resolve() != STEAMWORKS_CONTENT_BUILDER.resolve():
        STEAMWORKS_CONTENT_BUILDER.parent.mkdir(parents=True, exist_ok=True)
        if STEAMWORKS_CONTENT_BUILDER.exists():
            shutil.rmtree(STEAMWORKS_CONTENT_BUILDER)
        shutil.move(str(content_builder), str(STEAMWORKS_CONTENT_BUILDER))

    _ensure_steamcmd_executable()
    _save_sdk_state(cached_login=cached_login, username=cached_username)
    logger.info("Steamworks SDK extracted; ContentBuilder kept at %s", STEAMWORKS_CONTENT_BUILDER)
    return STEAMWORKS_CONTENT_BUILDER


def login_steamcmd(username: str, password: str | None = None, steam_guard_code: str | None = None) -> None:
    if not username.strip():
        raise ValueError("username is required")

    if not STEAMCMD_PATH.exists():
        raise FileNotFoundError(f"SteamCMD not found at {STEAMCMD_PATH}")

    _ensure_steamcmd_executable()

    command = [str(STEAMCMD_PATH.resolve()), "+login", username.strip()]
    if password:
        command.append(password.strip())
    command.append("+quit")

    logger.info("Running SteamCMD login for %s", username)

    process = subprocess.Popen(
        command,
        cwd=str(STEAMCMD_PATH.parent.resolve()),
        text=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
    )

    output_lines: list[str] = []
    steam_guard_requested = False

    assert process.stdout is not None
    assert process.stdin is not None

    try:
        current_line = ""
        while True:
            char = process.stdout.read(1)
            
            if not char:
                if current_line:
                    output_lines.append(current_line)
                    logger.info("SteamCMD: %s", current_line)
                break
                
            if char == '\n':
                output_lines.append(current_line + "\n")
                logger.info("SteamCMD: %s", current_line)
                current_line = ""
            else:
                current_line += char

                if "Steam Guard code:" in current_line:
                    output_lines.append(current_line)
                    logger.info("SteamCMD: %s", current_line)
                    current_line = ""
                    
                    steam_guard_requested = True
                    if not steam_guard_code:
                        raise RuntimeError(
                            "SteamCMD requested a Steam Guard code, but none was provided. "
                            "Start the login again and submit the code in the second step."
                        )

                    process.stdin.write(steam_guard_code.strip() + "\n")
                    process.stdin.flush()
                    logger.info("Steam Guard code submitted to SteamCMD")

        returncode = process.wait()
    finally:
        try:
            process.stdin.close()
        except Exception:
            pass

    if returncode != 0:
        joined_output = "".join(output_lines)
        raise RuntimeError(f"SteamCMD login failed with exit code {returncode}\n{joined_output}")

    _save_sdk_state(cached_login=True, username=username.strip())
    logger.info("SteamCMD login completed successfully")


def _detect_platform(build_id: str, artifact_path: Path) -> str:
    haystack = f"{build_id} {artifact_path.name}".lower()

    if "windows" in haystack or "win" in haystack:
        return "windows"
    if "linux" in haystack:
        return "linux"
    if "mac" in haystack:
        return "macos"
    if "android" in haystack:
        return "android"

    raise ValueError(f"Could not determine platform for artifact: build_id={build_id}, file={artifact_path}")


def _platform_folder(platform_name: str) -> str:
    mapping = {
        "windows": "windows",
        "linux": "linux",
        "macos": "macos",
        "osx": "macos",
        "android": "android",
    }
    return mapping.get(platform_name.lower(), platform_name.lower())


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
    return Path(os.path.relpath(path.resolve(), SCRIPTS_DIR.resolve())).as_posix()


def _write_app_build_vdf(appid: str, desc: str, setlive: str, depots: list[dict[str, str]]) -> None:
    SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)

    depot_lines = []
    for depot in depots:
        depot_lines.extend(
            [
                f'        "{depot["depot_id"]}"',
                "        {",
                f'            "FileMapping"',
                "            {",
                f'                "LocalPath" "{depot["contentroot"]}"',
                '                "DepotPath" "."',
                '                "recursive" "1"',
                "            }",
                "        }",
            ]
        )

    content = f'''"appbuild"
{{
    "appid" "{appid}"
    "desc" "{desc}"
    "buildoutput" "../output"
    "contentroot" "../output/staging"
    "setlive" "{setlive}"
    "depots"
    {{
{chr(10).join(depot_lines)}
    }}
}}
'''
    APP_BUILD_VDF.write_text(content, encoding="utf-8")


def _write_depot_vdf(depot_id: str, platform_name: str) -> Path:
    platform_folder = _platform_folder(platform_name)
    depot_vdf = SCRIPTS_DIR / f"depot_{depot_id}.vdf"
    depot_vdf.write_text(
        f'''"DepotBuild"
{{
    "DepotID" "{depot_id}"
    "ContentRoot" "../output/staging/{platform_folder}"
    "FileMapping"
    {{
        "LocalPath" "*"
        "DepotPath" "."
        "recursive" "1"
    }}
}}
''',
        encoding="utf-8",
    )
    return depot_vdf


def generate_steampipe_vdfs(appid: str, desc: str, setlive: str, depots: list[dict[str, str]]) -> None:
    if not depots:
        raise ValueError("At least one depot is required")

    _write_app_build_vdf(appid, desc, setlive, depots)
    for depot in depots:
        _write_depot_vdf(depot["depot_id"], depot["os"].lower())


def _run_steamcmd_upload(app_build_vdf: Path) -> None:
    from models import GlobalSettings

    username_setting = GlobalSettings.get_or_none(key="STEAMCMD_USERNAME")
    password_setting = GlobalSettings.get_or_none(key="STEAMCMD_PASSWORD")
    
    username = username_setting.value if username_setting else os.getenv("STEAMCMD_USERNAME", "").strip()
    password = password_setting.value if password_setting else os.getenv("STEAMCMD_PASSWORD", "").strip()

    if not steamworks_sdk_is_ready():
        raise RuntimeError("Steamworks SDK is not ready yet. Upload the SDK before running build uploads.")

    if not username:
        raise RuntimeError("STEAMCMD_USERNAME must be set in Global Settings or Environment")

    if not STEAMCMD_PATH.exists():
        raise FileNotFoundError(f"SteamCMD not found at {STEAMCMD_PATH}")

    if not app_build_vdf.exists():
        raise FileNotFoundError(f"App build VDF not found at {app_build_vdf}")

    command = [str(STEAMCMD_PATH.resolve()), "+login", username]
    if password:
        command.append(password)

    command.extend(["+run_app_build", str(app_build_vdf.resolve()), "+quit"])

    logger.info("Starting SteamCMD depot upload with %s", app_build_vdf)
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
        except Exception:
            logger.exception("Failed to delete uploaded artifact archive %s", artifact_path)

    for stage_dir in staged_dirs:
        try:
            if stage_dir.exists():
                shutil.rmtree(stage_dir)
        except Exception:
            logger.exception("Failed to delete extracted staging directory %s", stage_dir)


def upload_artifacts(
    project_id: str,
    build_id: str,
    artifact_paths: list[Path],
    appid: str,
    desc: str,
    setlive: str,
    depots_config: list[dict[str, str]]
) -> None:
    from artifact_manager import has_uploaded_build

    if not steamworks_sdk_is_ready():
        raise RuntimeError("Steamworks SDK is not uploaded yet. Build upload is paused.")

    # Unique build ID across projects
    full_build_id = f"{project_id}_{build_id}"
    build_staging_root = STAGING_ROOT / full_build_id

    if has_uploaded_build(full_build_id):
        logger.info("Build %s was already uploaded; skipping Steam upload", full_build_id)
        return

    staged_dirs: set[Path] = set()
    staged_platforms: set[str] = set()

    for artifact_path in artifact_paths:
        if not artifact_path.exists():
            raise FileNotFoundError(f"Artifact does not exist: {artifact_path}")

        platform_name = _detect_platform(build_id, artifact_path)
        stage_dir = build_staging_root / platform_name
        
        if platform_name not in staged_platforms:
            _prepare_stage_dir(stage_dir)
            staged_platforms.add(platform_name)
            # Add build-specific root to staged_dirs so it gets cleaned up
            staged_dirs.add(build_staging_root)

        logger.info("Extracting %s to %s", artifact_path.name, stage_dir)
        _extract_archive(artifact_path, stage_dir)

    if not staged_platforms:
        raise RuntimeError(f"No artifacts were staged for build {full_build_id}")

    # Generate project-specific VDFs
    project_scripts_dir = SCRIPTS_DIR / project_id
    project_scripts_dir.mkdir(parents=True, exist_ok=True)
    
    app_vdf_path = project_scripts_dir / f"app_{appid}.vdf"
    
    # Redefine _write_app_build_vdf and _write_depot_vdf to take paths
    def write_app_vdf(path: Path, appid: str, desc: str, setlive: str, depots: list[dict[str, str]]):
        depot_lines = []
        for depot in depots:
            # Try to match depot OS to staged platforms
            os_key = depot["os"].lower()
            platform_folder = _platform_folder(os_key)
            
            if platform_folder not in staged_platforms:
                # If we don't have an artifact for this depot's OS, skip it
                # (or we could include it with empty mapping, but skipping is safer)
                logger.warning("No artifact found for depot %s (OS: %s); skipping from this build", depot["depot_id"], os_key)
                continue

            depot_lines.extend([
                f'        "{depot["depot_id"]}"',
                "        {",
                f'            "FileMapping"',
                "            {",
                f'                "LocalPath" "{platform_folder}/*"',
                '                "DepotPath" "."',
                '                "recursive" "1"',
                "            }",
                "        }",
            ])
        
        content = f'''"appbuild"
{{
    "appid" "{appid}"
    "desc" "{desc}"
    "buildoutput" "{build_staging_root.parent.as_posix()}"
    "contentroot" "{build_staging_root.as_posix()}"
    "setlive" "{setlive}"
    "depots"
    {{
{chr(10).join(depot_lines)}
    }}
}}
'''
        path.write_text(content, encoding="utf-8")

    def write_depot_vdf(depot_id: str, platform_name: str, scripts_dir: Path):
        platform_folder = _platform_folder(platform_name)
        depot_vdf = scripts_dir / f"depot_{depot_id}.vdf"
        depot_vdf.write_text(
            f'''"DepotBuild"
{{
    "DepotID" "{depot_id}"
    "ContentRoot" "{ (build_staging_root / platform_folder).as_posix() }"
    "FileMapping"
    {{
        "LocalPath" "*"
        "DepotPath" "."
        "recursive" "1"
    }}
}}
''',
            encoding="utf-8",
        )

    write_app_vdf(app_vdf_path, appid, desc, setlive, depots_config)
    for depot in depots_config:
        write_depot_vdf(depot["depot_id"], depot["os"].lower(), project_scripts_dir)

    _run_steamcmd_upload(app_vdf_path)
    _cleanup_files(artifact_paths, staged_dirs)
    logger.info("Finished Steam depot upload and cleanup for build %s", full_build_id)


_upload_queue: queue.Queue = queue.Queue()


def _upload_worker():
    from artifact_manager import mark_uploaded
    logger.info("Starting Steam upload worker thread")
    while True:
        try:
            task = _upload_queue.get()
            if task is None:
                break
            
            project_id = task.get("project_id")
            build_id = task.get("build_id")
            full_build_id = f"{project_id}_{build_id}"
            
            logger.info("Upload worker: Processing build %s", full_build_id)
            try:
                upload_artifacts(**task)
                mark_uploaded(full_build_id)
                logger.info("Upload worker: Successfully uploaded and marked build %s", full_build_id)
            except Exception:
                logger.exception("Upload worker: Failed to upload build %s", full_build_id)
            finally:
                _upload_queue.task_done()
        except Exception:
            logger.exception("Upload worker: unexpected error in worker loop")


# Start worker thread
_worker_thread = threading.Thread(target=_upload_worker, daemon=True, name="steam-upload-worker")
_worker_thread.start()


def queue_upload_artifacts(**kwargs):
    logger.info("Queuing upload for build: %s_%s", kwargs.get("project_id"), kwargs.get("build_id"))
    _upload_queue.put(kwargs)