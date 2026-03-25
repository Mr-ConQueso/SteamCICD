import logging
import threading
from pathlib import Path

from flask import Flask, flash, redirect, render_template, request, session, url_for
from werkzeug.utils import secure_filename

from config import STEAMCMD_PATH, steamworks_sdk_is_ready
from poller import process_new_builds, start_poller_once
from uploader import extract_steamworks_sdk, get_steamworks_status, login_steamcmd
from models import Project, Depot, GlobalSettings, init_db, db

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

# Initialize DB on startup
with app.app_context():
    init_db()

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

@app.teardown_appcontext
def close_db(error):
    if not db.is_closed():
        db.close()

@app.get("/")
def index():
    status = get_steamworks_status()
    pending_login = session.get("pending_steamcmd_login")
    projects = Project.select()
    
    global_api_key = GlobalSettings.get_or_none(key="UNITY_API_KEY")
    steam_username = GlobalSettings.get_or_none(key="STEAMCMD_USERNAME")
    
    return render_template(
        "index.html",
        steamworks_status=status,
        pending_login=pending_login,
        projects=projects,
        global_api_key=global_api_key.value if global_api_key else "",
        steam_username=steam_username.value if steam_username else ""
    )

@app.post("/settings/global")
def save_global_settings():
    unity_api_key = request.form.get("unity_api_key", "").strip()
    steam_username = request.form.get("steam_username", "").strip()
    steam_password = request.form.get("steam_password", "").strip()
    
    if unity_api_key:
        GlobalSettings.get_or_create(key="UNITY_API_KEY")[0].value = unity_api_key
        GlobalSettings.update(value=unity_api_key).where(GlobalSettings.key == "UNITY_API_KEY").execute()
    
    if steam_username:
        GlobalSettings.get_or_create(key="STEAMCMD_USERNAME")[0].value = steam_username
        GlobalSettings.update(value=steam_username).where(GlobalSettings.key == "STEAMCMD_USERNAME").execute()
    
    if steam_password:
        GlobalSettings.get_or_create(key="STEAMCMD_PASSWORD")[0].value = steam_password
        GlobalSettings.update(value=steam_password).where(GlobalSettings.key == "STEAMCMD_PASSWORD").execute()
        
    flash("Global settings updated.", "success")
    return redirect(url_for("index"))

@app.post("/projects/create")
def create_project():
    name = request.form.get("name", "").strip()
    unity_org_id = request.form.get("unity_org_id", "").strip()
    unity_project_id = request.form.get("unity_project_id", "").strip()
    steam_app_id = request.form.get("steam_app_id", "").strip()
    steam_desc = request.form.get("steam_desc", "Automatic CI/CD Build").strip()
    steam_set_live = request.form.get("steam_set_live", "").strip()
    
    if not all([name, unity_org_id, unity_project_id, steam_app_id]):
        flash("All project fields are required.", "error")
        return redirect(url_for("index"))
    
    project = Project.create(
        name=name,
        unity_org_id=unity_org_id,
        unity_project_id=unity_project_id,
        steam_app_id=steam_app_id,
        steam_desc=steam_desc,
        steam_set_live=steam_set_live
    )
    
    # Add depots
    for i in range(1, 5):
        os_name = request.form.get(f"depot_os_{i}", "").strip()
        depot_id = request.form.get(f"depot_id_{i}", "").strip()
        if os_name and depot_id:
            Depot.create(project=project, os=os_name, depot_id=depot_id)
            
    flash(f"Project '{name}' created successfully.", "success")
    return redirect(url_for("index"))

@app.post("/projects/<int:project_id>/delete")
def delete_project(project_id):
    project = Project.get_by_id(project_id)
    name = project.name
    # Depots will be deleted automatically if we set ON DELETE CASCADE, 
    # but Peewee needs it explicitly if not using native constraints.
    # For simplicity, let's delete them manually.
    Depot.delete().where(Depot.project == project).execute()
    project.delete_instance()
    flash(f"Project '{name}' deleted.", "success")
    return redirect(url_for("index"))

@app.post("/projects/<int:project_id>/toggle")
def toggle_project(project_id):
    project = Project.get_by_id(project_id)
    project.enabled = not project.enabled
    project.save()
    status = "enabled" if project.enabled else "disabled"
    flash(f"Project '{project.name}' is now {status}.", "success")
    return redirect(url_for("index"))

@app.get("/health")
def health():
    return {
        "status": "ok",
        "steamworks_ready": steamworks_sdk_is_ready(),
        "steamcmd_present": _steamcmd_path_exists(),
    }, 200

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
def steamcmd_login_route(): # renamed to avoid conflict with import
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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)