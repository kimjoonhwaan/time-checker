"""Path resolution for TimeChecker.

Resolution order for every path: env var → config dict → OS-aware default.

Server mode (Linux/Railway): defaults to /data/...
Client mode (Windows):       defaults to %APPDATA%\\TimeChecker\\...
Dev fallback (anywhere else): the project directory next to this file.
"""
import os
import sys
from pathlib import Path

_PROJECT_DIR = Path(__file__).resolve().parent


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def _default_data_dir() -> Path:
    if _is_windows():
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "TimeChecker"
    if os.path.isdir("/data") and os.access("/data", os.W_OK):
        return Path("/data")
    return _PROJECT_DIR


def data_dir(config: dict | None = None) -> Path:
    env = os.environ.get("TIMECHECKER_DATA_DIR")
    if env:
        p = Path(env)
    elif config and config.get("data_dir"):
        p = Path(config["data_dir"])
    else:
        p = _default_data_dir()
    p.mkdir(parents=True, exist_ok=True)
    return p


def server_db_path(config: dict | None = None) -> Path:
    env = os.environ.get("TIMECHECKER_DB_PATH")
    if env:
        return Path(env)
    if config and config.get("db_path"):
        return Path(config["db_path"])
    return data_dir(config) / "timetracker.db"


def client_queue_db_path(config: dict | None = None) -> Path:
    """Local SQLite used as offline buffer when server unreachable."""
    env = os.environ.get("TIMECHECKER_QUEUE_DB_PATH")
    if env:
        return Path(env)
    if config and config.get("queue_db_path"):
        return Path(config["queue_db_path"])
    return data_dir(config) / "queue.db"


def config_path() -> Path:
    """Where to read/write config.json.

    Priority:
      1. TIMECHECKER_CONFIG_PATH env
      2. %APPDATA%\\TimeChecker\\config.json (if Windows) or /data/config.json
      3. Project directory config.json (dev fallback, always exists in repo)
    """
    env = os.environ.get("TIMECHECKER_CONFIG_PATH")
    if env:
        return Path(env)
    user_cfg = data_dir() / "config.json"
    if user_cfg.exists():
        return user_cfg
    return _PROJECT_DIR / "config.json"


def log_dir(config: dict | None = None) -> Path:
    env = os.environ.get("TIMECHECKER_LOG_DIR")
    if env:
        p = Path(env)
    elif config and config.get("log_dir"):
        p = Path(config["log_dir"])
    else:
        p = data_dir(config) / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def shutdown_marker_path(config: dict | None = None) -> Path:
    """JSON file written on clean tracker shutdown; read on next start
    to backdate the first session if the gap was small."""
    return data_dir(config) / "last_shutdown.json"


def project_dir() -> Path:
    return _PROJECT_DIR
