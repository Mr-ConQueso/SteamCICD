import logging
import threading
from pathlib import Path

from flask import Flask, flash, redirect, render_template, request, session, url_for
from werkzeug.utils import secure_filename

from config import STEAMCMD_PATH, steamworks_sdk_is_ready
from poller import process_new_builds, start_poller_once
from uploader import extract_steamworks_sdk, generate_steampipe_vdfs, get_steamworks_status, login_steamcmd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = "change-this-secret-key"

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "downloads" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def _steamcmd_path_exists() -> bool:
    return STEAMCMD_PATH.exists()


auto_login_started = False

def _start_auto_login() -> None:
    global auto_login_started
    if auto_login_started:
        return
    auto_login_started = True

    status = get_steamworks_status()
    if status.get("cached_login") and status.get("cached_username") and _steamcmd_path_exists():
        username = str(status["cached_username"])
        logger.info("Attempting auto-login for cached user %s", username)

        def _worker() -> None:
            try:
                login_steamcmd(username=username, password=None, steam_guard_code=None)
                logger.info("SteamCMD auto-login worker finished for %s", username)
            except Exception:
                logger.exception("SteamCMD auto-login worker failed for %s", username)

        thread = threading.Thread(target=_worker, daemon=True, name="steamcmd-autologin")
        thread.start()


@app.before_request
def ensure_background_tasks() -> None:
    start_poller_once()
    _start_auto_login()


@app.get("/")
def index():
    status = get_steamworks_status()
    pending_login = session.get("pending_steamcmd_login")
    return render_template(
        "index.html",
        steamworks_status=status,
        pending_login=pending_login,
    )


@app.get("/health")
def health():
    return {
        "status": "ok",
        "polling": True,
        "steamworks_ready": steamworks_sdk_is_ready(),
        "steamcmd_present": _steamcmd_path_exists(),
    }, 200


@app.get("/sdk-status")
def sdk_status():
    return get_steamworks_status(), 200


@app.get("/poll-now")
def poll_now():
    if not steamworks_sdk_is_ready():
        return {"error": "Steamworks SDK is not uploaded yet; build polling is paused"}, 409

    logger.info("Manual poll requested from /poll-now")
    try:
        results = process_new_builds()
        logger.info("Manual poll completed; downloaded %d build(s)", len(results))
        return {"downloaded": results}, 200
    except Exception:
        logger.exception("Manual poll failed")
        return {"error": "poll failed; check server logs"}, 500


@app.post("/sdk/upload")
def upload_sdk():
    uploaded = request.files.get("sdk_zip")
    if not uploaded or not uploaded.filename:
        flash("Please choose a Steamworks SDK zip file.", "error")
        return redirect(url_for("index"))

    filename = secure_filename(uploaded.filename)
    target = UPLOAD_DIR / filename
    uploaded.save(target)

    try:
        content_builder = extract_steamworks_sdk(target)
        flash(f"SDK extracted successfully. ContentBuilder kept at: {content_builder}", "success")
    except Exception as exc:
        logger.exception("Failed to process Steamworks SDK upload")
        flash(f"SDK upload failed: {exc}", "error")

    return redirect(url_for("index"))


@app.post("/steamcmd/login")
def steamcmd_login():
    if not _steamcmd_path_exists():
        flash(
            "SteamCMD was not found. Upload the Steamworks SDK first so steamcmd.sh is available.",
            "error",
        )
        return redirect(url_for("index"))

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()
    steam_guard_code = request.form.get("steam_guard_code", "").strip()
    action = request.form.get("action", "start").strip()

    status = get_steamworks_status()
    is_cached = bool(status.get("cached_login", False))

    if not username:
        flash("Username is required.", "error")
        return redirect(url_for("index"))

    try:
        if action == "start":
            if not is_cached and not password:
                flash("Password is required to start the SteamCMD login.", "error")
                return redirect(url_for("index"))

            session["pending_steamcmd_login"] = {
                "username": username,
                "password": password,
            }

            try:
                login_steamcmd(username=username, password=password if not is_cached else None, steam_guard_code=None)
                session.pop("pending_steamcmd_login", None)
                flash("SteamCMD login completed successfully.", "success")
            except RuntimeError as exc:
                if "Steam Guard code" in str(exc):
                    flash(
                        "SteamCMD login started. Steam Guard is required, please wait for the code and then submit it.",
                        "success",
                    )
                else:
                    session.pop("pending_steamcmd_login", None)
                    raise

        elif action == "complete":
            pending = session.get("pending_steamcmd_login")
            if not pending or pending.get("username") != username:
                flash("No pending SteamCMD login found for this username. Start the login again.", "error")
                return redirect(url_for("index"))

            if not steam_guard_code:
                flash("Steam Guard code is required to complete the login.", "error")
                return redirect(url_for("index"))

            login_steamcmd(
                username=username,
                password=pending["password"],
                steam_guard_code=steam_guard_code,
            )

            session.pop("pending_steamcmd_login", None)
            flash("SteamCMD login completed successfully.", "success")

        else:
            flash("Invalid SteamCMD login action.", "error")

    except Exception as exc:
        logger.exception("SteamCMD login failed")
        flash(f"SteamCMD login failed: {exc}", "error")

    return redirect(url_for("index"))


@app.post("/vdf/generate")
def generate_vdfs():
    appid = request.form.get("appid", "").strip()
    desc = request.form.get("desc", "").strip()
    setlive = request.form.get("setlive", "").strip()

    if not appid or not desc:
        flash("appid and desc are required.", "error")
        return redirect(url_for("index"))

    depots = []
    seen_os = set()

    for i in range(1, 5):
        os_name = request.form.get(f"depot_os_{i}", "").strip()
        depot_id = request.form.get(f"depot_id_{i}", "").strip()

        if not os_name and not depot_id:
            continue

        if os_name in seen_os:
            flash(f"Only one depot per OS is allowed. Duplicate: {os_name}", "error")
            return redirect(url_for("index"))

        if os_name not in {"Windows", "Linux", "MacOS", "Android"}:
            flash(f"Invalid OS selected: {os_name}", "error")
            return redirect(url_for("index"))

        if not depot_id:
            flash(f"DepotID missing for {os_name}.", "error")
            return redirect(url_for("index"))

        seen_os.add(os_name)
        depots.append(
            {
                "os": os_name,
                "depot_id": depot_id,
                "name": f"{os_name} Depot",
                "contentroot": f"../output/staging/{os_name.lower()}",
            }
        )

    if not depots:
        flash("At least one depot is required.", "error")
        return redirect(url_for("index"))

    try:
        generate_steampipe_vdfs(appid=appid, desc=desc, setlive=setlive, depots=depots)
        flash("VDF files generated successfully.", "success")
    except Exception as exc:
        logger.exception("Failed to generate VDF files")
        flash(f"VDF generation failed: {exc}", "error")

    return redirect(url_for("index"))


@app.get("/downloads")
def list_downloaded_files():
    from config import DOWNLOAD_DIR

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    files = [p.name for p in DOWNLOAD_DIR.iterdir() if p.is_file()]
    return {"files": files}, 200