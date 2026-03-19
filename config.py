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