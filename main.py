"""Local Windows entry point.

Two modes, picked from config / env:

  - LOCAL mode (default if `server_url` not set):
      tracker writes directly to local SQLite at %APPDATA%\\TimeChecker\\timetracker.db
      and Flask dashboard runs on http://localhost:<flask_port>.

  - REMOTE mode (`server_url` configured):
      tracker POSTs to that server's /api/ingest/* endpoints.
      Local Flask is NOT started. Tray "open dashboard" opens server_url.
"""
import json
import os
import socket
import threading
import uuid
from pathlib import Path

import paths
from tracker import TrackerLoop
from tray import TrayApp


def _load_config() -> dict:
    cfg_path = paths.config_path()
    if cfg_path.exists():
        with open(cfg_path) as f:
            return json.load(f)
    return {}


def _save_config(cfg: dict):
    cfg_path = paths.config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def _ensure_device_id(config: dict) -> str:
    did = os.environ.get("TIMECHECKER_DEVICE_ID") or config.get("device_id")
    if did:
        return did
    did = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
    config["device_id"] = did
    try:
        _save_config(config)
    except Exception:
        pass
    return did


def _resolve_server_url(config: dict) -> str:
    return (os.environ.get("TIMECHECKER_SERVER_URL")
            or config.get("server_url") or "").strip()


def _resolve_api_key(config: dict) -> str:
    return (os.environ.get("TIMECHECKER_API_KEY")
            or config.get("api_key") or "").strip()


def _run_heartbeat(client, tracker, shutdown_event):
    while not shutdown_event.is_set():
        try:
            status = tracker.get_status()
            client.heartbeat(
                state=status["state"],
                idle_seconds=status["idle_seconds"],
                excluded_app=status.get("excluded_app"),
            )
        except Exception:
            pass
        shutdown_event.wait(30)


def main():
    config = _load_config()
    device_id = _ensure_device_id(config)
    server_url = _resolve_server_url(config)
    api_key = _resolve_api_key(config)

    shutdown_event = threading.Event()
    pause_event = threading.Event()

    if server_url:
        # ── REMOTE mode ─────────────────────────────────────────
        from ingest_client import IngestClient
        client = IngestClient(
            server_url=server_url,
            api_key=api_key,
            queue_db_path=str(paths.client_queue_db_path(config)),
            device_id=device_id,
        )
        backend = client
        flask_thread = None
        dashboard_url = server_url
    else:
        # ── LOCAL mode ──────────────────────────────────────────
        from database import DatabaseManager
        from app import run_flask
        db = DatabaseManager(str(paths.server_db_path(config)))
        db.close_stale_sessions()
        backend = db
        flask_thread = threading.Thread(
            target=run_flask, args=(db, None, config, shutdown_event),
            daemon=True, name="flask"
        )
        dashboard_url = None  # tray will compute from config

    tracker = TrackerLoop(backend, config, shutdown_event, pause_event)
    tracker_thread = threading.Thread(target=tracker.run, daemon=True, name="tracker")
    tracker_thread.start()

    heartbeat_thread = None
    if server_url:
        # Late start so tracker.get_status() works
        heartbeat_thread = threading.Thread(
            target=_run_heartbeat, args=(backend, tracker, shutdown_event),
            daemon=True, name="heartbeat"
        )
        heartbeat_thread.start()

    if flask_thread is not None:
        flask_thread.start()

    # TrayApp signature accepts (tracker, _unused, config, shutdown, pause, dashboard_url=None)
    tray = TrayApp(tracker, None, config, shutdown_event, pause_event,
                   dashboard_url=dashboard_url)
    tray.run()  # blocks — owns main thread

    # Shutdown cleanup
    try:
        if not server_url:
            backend.close_stale_sessions()
            backend.close()
        else:
            backend.close()
    except Exception:
        pass


if __name__ == "__main__":
    main()
