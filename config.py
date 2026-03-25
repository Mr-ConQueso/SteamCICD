import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

UNITY_API_BASE_URL = os.getenv("UNITY_API_BASE_URL", "https://build-api.cloud.unity3d.com/api/v1")
UNITY_API_KEY = os.getenv("UNITY_API_KEY", "")
UNITY_ORG_ID = os.getenv("UNITY_ORG_ID", "")
UNITY_PROJECT_ID = os.getenv("UNITY_PROJECT_ID", "")
UNITY_BUILD_TARGET_ID = os.getenv("UNITY_BUILD_TARGET_ID", "")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "./downloads"))

STEAMWORKS_ROOT = Path(os.getenv("STEAMWORKS_ROOT", "./steamworks_sdk")).resolve()
STEAMWORKS_CONTENT_BUILDER = STEAMWORKS_ROOT / "tools" / "ContentBuilder"
STEAMCMD_PATH = STEAMWORKS_CONTENT_BUILDER / "builder_linux" / "steamcmd.sh"
STEAMCMD_CONFIG_FILE = STEAMWORKS_CONTENT_BUILDER / "builder_linux" / "update_hosts_cached.vdf"


def steamworks_sdk_is_ready() -> bool:
    return STEAMWORKS_CONTENT_BUILDER.exists() and STEAMCMD_PATH.exists() and STEAMCMD_CONFIG_FILE.exists()


def validate_settings() -> None:
    missing = [
        name
        for name, value in {
            "UNITY_API_KEY": UNITY_API_KEY,
            "UNITY_ORG_ID": UNITY_ORG_ID,
            "UNITY_PROJECT_ID": UNITY_PROJECT_ID,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")