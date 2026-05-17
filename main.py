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
        from ingest_client import IngestClient
        backend = IngestClient(
            server_url=server_url,
            api_key=api_key,
            queue_db_path=str(paths.client_queue_db_path(config)),
            device_id=device_id,
        )
        flask_thread = None
        dashboard_url = server_url
    else:
        from database import DatabaseManager
        from app import run_flask
        backend = DatabaseManager(str(paths.server_db_path(config)))
        backend.cleanup_orphan_todo_sessions()  # tidy on startup
        flask_thread = threading.Thread(
            target=run_flask, args=(backend, None, config, shutdown_event),
            daemon=True, name="flask"
        )
        dashboard_url = None

    tracker = TrackerLoop(backend, config, shutdown_event, pause_event)
    tracker_thread = threading.Thread(target=tracker.run, daemon=True, name="tracker")
    tracker_thread.start()

    if server_url:
        threading.Thread(
            target=_run_heartbeat, args=(backend, tracker, shutdown_event),
            daemon=True, name="heartbeat"
        ).start()

    if flask_thread is not None:
        flask_thread.start()

    tray = TrayApp(tracker, None, config, shutdown_event, pause_event,
                   dashboard_url=dashboard_url)
    tray.run()  # blocks — owns main thread

    # Tray exited → shutdown_event is set. Serialize cleanup:
    # 1. wait for last tracker tick to finish (so we don't race close())
    # 2. force-close any open activity / todo
    # 3. close backend (drains queue / closes sqlite)
    tracker_thread.join(timeout=5)
    try:
        tracker.close_active_session()
    except Exception:
        pass
    try:
        backend.close()
    except Exception:
        pass


if __name__ == "__main__":
    main()
