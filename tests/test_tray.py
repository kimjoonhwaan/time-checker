import threading
from unittest.mock import MagicMock, patch

import pytest

from tracker import TrackerState
from tray import TrayApp, ICONS, _make_icon


# ── Icon generation ───────────────────────────────────────────

class TestMakeIcon:
    def test_icon_is_64x64(self):
        img = _make_icon("#22c55e")
        assert img.size == (64, 64)

    def test_icon_mode_is_rgba(self):
        img = _make_icon("#22c55e")
        assert img.mode == "RGBA"

    def test_tracking_icon_exists(self):
        assert TrackerState.TRACKING in ICONS

    def test_idle_icon_exists(self):
        assert TrackerState.IDLE in ICONS

    def test_paused_icon_exists(self):
        assert TrackerState.PAUSED in ICONS


# ── TrayApp helpers ───────────────────────────────────────────

def _make_tray(tracker_status=None):
    shutdown = threading.Event()
    pause = threading.Event()

    tracker = MagicMock()
    tracker.get_status.return_value = tracker_status or {
        "state": TrackerState.TRACKING,
        "today_total_seconds": 13320,  # 3h 42m
    }
    db = MagicMock()
    config = {"flask_port": 5000}

    with patch("tray.pystray.Icon"):
        tray = TrayApp(tracker, db, config, shutdown, pause)

    tray._shutdown = shutdown
    tray._pause = pause
    return tray, shutdown, pause


class TestStatusLabel:
    def test_format_hours_minutes(self):
        tray, _, _ = _make_tray({"state": TrackerState.TRACKING, "today_total_seconds": 13320})
        label = tray._status_label()
        assert "3h" in label
        assert "42m" in label

    def test_includes_state_label_tracking(self):
        tray, _, _ = _make_tray({"state": TrackerState.TRACKING, "today_total_seconds": 0})
        assert "추적 중" in tray._status_label()

    def test_includes_state_label_idle(self):
        tray, _, _ = _make_tray({"state": TrackerState.IDLE, "today_total_seconds": 0})
        assert "유휴" in tray._status_label()

    def test_includes_state_label_paused(self):
        tray, _, _ = _make_tray({"state": TrackerState.PAUSED, "today_total_seconds": 0})
        assert "일시정지" in tray._status_label()

    def test_zero_time_format(self):
        tray, _, _ = _make_tray({"state": TrackerState.IDLE, "today_total_seconds": 0})
        label = tray._status_label()
        assert "0h" in label


class TestTogglePause:
    def test_sets_pause_when_clear(self):
        tray, _, pause = _make_tray()
        assert not pause.is_set()
        tray._toggle_pause(None, None)
        assert pause.is_set()

    def test_clears_pause_when_set(self):
        tray, _, pause = _make_tray()
        pause.set()
        tray._toggle_pause(None, None)
        assert not pause.is_set()


class TestQuit:
    def test_sets_shutdown_event(self):
        tray, shutdown, _ = _make_tray()
        mock_icon = MagicMock()
        tray._quit(mock_icon, None)
        assert shutdown.is_set()

    def test_calls_icon_stop(self):
        tray, _, _ = _make_tray()
        mock_icon = MagicMock()
        tray._quit(mock_icon, None)
        mock_icon.stop.assert_called_once()


class TestOpenDashboard:
    def test_opens_correct_url(self):
        tray, _, _ = _make_tray()
        with patch("tray.webbrowser.open") as mock_open:
            tray._open_dashboard(None, None)
        mock_open.assert_called_once_with("http://localhost:5000")

    def test_uses_actual_port_if_set(self):
        tray, _, _ = _make_tray()
        tray._config["_actual_port"] = 5001
        with patch("tray.webbrowser.open") as mock_open:
            tray._open_dashboard(None, None)
        mock_open.assert_called_once_with("http://localhost:5001")
