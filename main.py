import os

from app import app
from config import validate_settings
from poller import start_poller_once

if __name__ == "__main__":
    should_start_poller = not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true"
    if should_start_poller:
        start_poller_once()

    app.run(host="0.0.0.0", port=5000, debug=True)