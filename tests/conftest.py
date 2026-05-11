import sys
import os
import types
from unittest.mock import MagicMock

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub win32 modules
for mod in ["win32gui", "win32process"]:
    stub = types.ModuleType(mod)
    sys.modules[mod] = stub

# Stub pystray with required attributes
_pystray = types.ModuleType("pystray")
_pystray.Icon = MagicMock
_menu_cls = MagicMock()
_menu_cls.SEPARATOR = MagicMock()
_pystray.Menu = _menu_cls
_pystray.MenuItem = MagicMock
sys.modules["pystray"] = _pystray

import pytest
from database import DatabaseManager


@pytest.fixture
def mem_db():
    db = DatabaseManager(":memory:")
    yield db
    db.close()


@pytest.fixture
def base_config():
    return {
        "idle_threshold_seconds": 300,
        "poll_interval_seconds": 30,
        "flask_port": 5000,
        "excluded_processes": ["vlc.exe"],
        "excluded_title_keywords": ["YouTube"],
    }
