import json
import threading
from pathlib import Path

from database import DatabaseManager
from tracker import TrackerLoop
from app import run_flask
from tray import TrayApp

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
DB_PATH = BASE_DIR / "timetracker.db"


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


def main():
    config = load_config()

    db = DatabaseManager(str(DB_PATH))
    db.close_stale_sessions()

    shutdown_event = threading.Event()
    pause_event = threading.Event()

    tracker = TrackerLoop(db, config, shutdown_event, pause_event)
    tracker_thread = threading.Thread(target=tracker.run, daemon=True, name="tracker")

    flask_thread = threading.Thread(
        target=run_flask, args=(db, tracker, config, shutdown_event), daemon=True, name="flask"
    )

    tracker_thread.start()
    flask_thread.start()

    tray = TrayApp(tracker, db, config, shutdown_event, pause_event)
    tray.run()  # blocks — owns main thread

    db.close_stale_sessions()
    db.close()


if __name__ == "__main__":
    main()
