import logging

from flask import Flask

from config import DOWNLOAD_DIR
from poller import process_new_builds, start_poller_once
from unity_client import list_project_build_targets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)


@app.before_request
def ensure_poller_running() -> None:
    start_poller_once()


@app.get("/health")
def health():
    logger.info("Health check requested")
    return {"status": "ok", "polling": True}, 200


@app.get("/poll-now")
def poll_now():
    logger.info("Manual poll requested from /poll-now")
    try:
        results = process_new_builds()
        logger.info("Manual poll completed; downloaded %d build(s)", len(results))
        return {"downloaded": results}, 200
    except Exception:
        logger.exception("Manual poll failed")
        return {"error": "poll failed; check server logs"}, 500


@app.get("/downloads")
def list_downloaded_files():
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    files = [p.name for p in DOWNLOAD_DIR.iterdir() if p.is_file()]
    logger.info("Listing downloaded files; found %d file(s)", len(files))
    return {"files": files}, 200


@app.get("/build-targets")
def build_targets():
    logger.info("Fetching build targets from Unity DevOps")
    try:
        targets = list_project_build_targets()
    except Exception:
        logger.exception("Failed to fetch build targets")
        return {"error": "failed to fetch build targets; check server logs"}, 500

    simplified = []
    for target in targets:
        simplified.append(
            {
                "id": target.get("id") or target.get("buildTargetId") or target.get("buildtargetid"),
                "name": target.get("name") or target.get("targetName") or "<unnamed>",
            }
        )
    logger.info("Discovered %d build target(s): %s", len(simplified), simplified)
    return {"build_targets": simplified}, 200