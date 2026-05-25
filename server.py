"""Railway / standalone server entry point.

Runs only the Flask dashboard + ingest API. No tracker, no system tray.
Local Windows clients POST measurements to /api/ingest/*.

Run modes:
  - Production (Railway):  gunicorn -b 0.0.0.0:$PORT server:flask_app
  - Local dev:             python server.py
"""
import json
import os

import paths
from app import flask_app, init_app
from database import DatabaseManager


def _load_server_config() -> dict:
    cfg_path = paths.config_path()
    if cfg_path.exists():
        try:
            with open(cfg_path) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


config = _load_server_config()
db = DatabaseManager(str(paths.server_db_path(config)),
                     idle_threshold_seconds=config.get("idle_threshold_seconds", 60))
db.cleanup()
init_app(
    db,
    tracker=None,
    config_path=paths.config_path(),
    api_key=os.environ.get("TIMECHECKER_API_KEY"),
)


def main():
    port = int(os.environ.get("PORT", config.get("flask_port", 5000)))
    host = os.environ.get("HOST", "0.0.0.0")
    flask_app.run(host=host, port=port, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
