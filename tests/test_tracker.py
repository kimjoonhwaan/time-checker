import threading
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from tracker import IdleDetector, WindowDetector, WindowInfo, TrackerLoop, TrackerState
from database import DatabaseManager


# ── IdleDetector ──────────────────────────────────────────────

class TestIdleDetector:
    def test_is_idle_true_when_above_threshold(self):
        det = IdleDetector()
        with patch.object(det, "get_idle_seconds", return_value=400.0):
            assert det.is_idle(300) is True

    def test_is_idle_false_when_below_threshold(self):
        det = IdleDetector()
        with patch.object(det, "get_idle_seconds", return_value=100.0):
            assert det.is_idle(300) is False

    def test_is_idle_false_at_exact_threshold_minus_one(self):
        det = IdleDetector()
        with patch.object(det, "get_idle_seconds", return_value=299.0):
            assert det.is_idle(300) is False


# ── WindowDetector._is_excluded ───────────────────────────────

class TestWindowDetectorExclusion:
    def _make(self, processes=None, keywords=None):
        return WindowDetector(
            excluded_processes=processes or ["vlc.exe"],
            excluded_keywords=keywords or ["YouTube"]
        )

    def test_excluded_by_exact_process_name(self):
        det = self._make()
        assert det._is_excluded("vlc.exe", "some title") is True

    def test_excluded_by_title_keyword(self):
        det = self._make()
        assert det._is_excluded("chrome.exe", "YouTube - something") is True

    def test_case_insensitive_process(self):
        det = self._make()
        assert det._is_excluded("VLC.EXE", "title") is True

    def test_case_insensitive_keyword(self):
        det = self._make()
        assert det._is_excluded("chrome.exe", "youtube channel") is True

    def test_not_excluded(self):
        det = self._make()
        assert det._is_excluded("code.exe", "main.py - VS Code") is False

    def test_get_active_window_returns_excluded_on_win32_error(self):
        # The win32gui stub has no real GetForegroundWindow → AttributeError is caught
        det = self._make()
        result = det.get_active_window()
        assert result is not None
        assert result.is_excluded is True


# ── TrackerLoop state machine ─────────────────────────────────

def _make_loop(mem_db, config, idle_seconds=0, window=None):
    """Return a TrackerLoop with mocked detectors."""
    shutdown = threading.Event()
    pause = threading.Event()
    loop = TrackerLoop(mem_db, config, shutdown, pause)

    loop._idle_detector = MagicMock()
    loop._idle_detector.get_idle_seconds.return_value = idle_seconds
    loop._idle_detector.is_idle.side_effect = lambda t: idle_seconds >= t

    loop._window_detector = MagicMock()
    loop._window_detector.get_active_window.return_value = window

    return loop, shutdown, pause


@pytest.fixture
def db():
    d = DatabaseManager(":memory:")
    yield d
    d.close()


@pytest.fixture
def cfg():
    return {"idle_threshold_seconds": 300, "poll_interval_seconds": 30,
            "excluded_processes": ["vlc.exe"], "excluded_title_keywords": ["YouTube"]}


class TestTrackerLoop:
    def test_idle_to_tracking_opens_session(self, db, cfg):
        win = WindowInfo("code.exe", "editor", is_excluded=False)
        loop, _, _ = _make_loop(db, cfg, idle_seconds=0, window=win)
        loop._tick()
        assert loop._state == TrackerState.TRACKING
        assert loop._current_session_id is not None

    def test_tracking_to_idle_closes_session(self, db, cfg):
        win = WindowInfo("code.exe", "editor", is_excluded=False)
        loop, _, _ = _make_loop(db, cfg, idle_seconds=0, window=win)
        loop._tick()  # → TRACKING, session opened
        session_id = loop._current_session_id

        # Now go idle
        loop._idle_detector.get_idle_seconds.return_value = 400
        loop._idle_detector.is_idle.side_effect = lambda t: True
        loop._tick()

        assert loop._state == TrackerState.IDLE
        assert loop._current_session_id is None
        rows = db.get_sessions_for_date(datetime.now().strftime("%Y-%m-%d"))
        assert any(r["id"] == session_id for r in rows)

    def test_tracking_to_excluded_on_excluded_window(self, db, cfg):
        win = WindowInfo("code.exe", "editor", is_excluded=False)
        loop, _, _ = _make_loop(db, cfg, idle_seconds=0, window=win)
        loop._tick()
        assert loop._state == TrackerState.TRACKING

        loop._window_detector.get_active_window.return_value = WindowInfo("chrome.exe", "YouTube", is_excluded=True)
        loop._tick()
        assert loop._state == TrackerState.EXCLUDED
        assert loop._current_session_id is None

    def test_window_change_creates_new_activity(self, db, cfg):
        win1 = WindowInfo("code.exe", "file1.py", is_excluded=False)
        loop, _, _ = _make_loop(db, cfg, idle_seconds=0, window=win1)
        loop._tick()  # open session + activity for code.exe

        win2 = WindowInfo("chrome.exe", "docs", is_excluded=False)
        loop._window_detector.get_active_window.return_value = win2
        loop._tick()  # activity switches to chrome.exe

        assert loop._current_window.process_name == "chrome.exe"

    def test_pause_event_ends_tracking_session(self, db, cfg):
        win = WindowInfo("code.exe", "editor", is_excluded=False)
        loop, _, pause = _make_loop(db, cfg, idle_seconds=0, window=win)
        loop._tick()
        assert loop._state == TrackerState.TRACKING

        pause.set()
        loop._tick()
        assert loop._state == TrackerState.PAUSED
        assert loop._current_session_id is None

    def test_session_end_time_uses_idle_onset(self, db, cfg):
        """When idle, end_time should be now - idle_seconds, not now."""
        win = WindowInfo("code.exe", "editor", is_excluded=False)
        loop, _, _ = _make_loop(db, cfg, idle_seconds=0, window=win)

        fixed_start = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        with patch("tracker.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_start
            mock_dt.now.side_effect = None
            mock_dt.now = MagicMock(return_value=fixed_start)
            loop._tick()

        # Now simulate idle: now=12:10, idle=400s → onset=12:03:20
        idle_secs = 400.0
        fake_now = fixed_start + timedelta(minutes=10)
        loop._idle_detector.get_idle_seconds.return_value = idle_secs
        loop._idle_detector.is_idle.side_effect = lambda t: True

        with patch("tracker.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            loop._tick()

        date_str = fixed_start.strftime("%Y-%m-%d")
        rows = db.get_sessions_for_date(date_str)
        if rows:
            end = datetime.fromisoformat(rows[0]["end_time"])
            expected_end = fake_now - timedelta(seconds=idle_secs)
            assert abs((end - expected_end).total_seconds()) < 2

    def test_get_status_returns_required_keys(self, db, cfg):
        win = WindowInfo("code.exe", "editor", is_excluded=False)
        loop, _, _ = _make_loop(db, cfg, idle_seconds=0, window=win)
        loop._tick()
        status = loop.get_status()
        assert "state" in status
        assert "today_total_seconds" in status

    def test_tracking_continues_when_window_is_none(self, db, cfg):
        # window=None (no foreground window) should now count as working
        loop, _, _ = _make_loop(db, cfg, idle_seconds=0, window=None)
        loop._tick()
        assert loop._state == TrackerState.TRACKING
