import threading
from datetime import datetime, timezone, timedelta

from tracker import WindowDetector, WindowInfo, TrackerLoop


# ── WindowDetector._is_excluded ───────────────────────────────

class TestWindowDetectorExclusion:
    def _make(self, processes=None, keywords=None):
        return WindowDetector(
            excluded_processes=processes or ["vlc.exe"],
            excluded_keywords=keywords or ["YouTube"]
        )

    def test_excluded_by_exact_process_name(self):
        assert self._make()._is_excluded("vlc.exe", "some title") is True

    def test_excluded_by_title_keyword(self):
        assert self._make()._is_excluded("chrome.exe", "YouTube - x") is True

    def test_case_insensitive_process(self):
        assert self._make()._is_excluded("VLC.EXE", "title") is True

    def test_case_insensitive_keyword(self):
        assert self._make()._is_excluded("chrome.exe", "youtube channel") is True

    def test_not_excluded(self):
        assert self._make()._is_excluded("code.exe", "main.py - VS Code") is False


# ── TrackerLoop accumulator ───────────────────────────────────

class FakeBackend:
    def __init__(self, active_todo_id=None):
        self.ticks = []
        self._active = active_todo_id

    def get_active_todo_id(self):
        return self._active

    def push_tick(self, **kwargs):
        self.ticks.append(kwargs)

    def get_today_todo_total_seconds(self, date):
        return 0


class FakeIdle:
    def __init__(self, idle):
        self.idle = idle

    def get_idle_seconds(self):
        return self.idle


class FakeWindow:
    def __init__(self, info):
        self.info = info

    def get_active_window(self):
        return self.info


def _loop(backend, idle, window_info, config=None):
    cfg = {"poll_interval_seconds": 30, "idle_threshold_seconds": 60,
           "device_id": "dev"}
    if config:
        cfg.update(config)
    loop = TrackerLoop(backend, cfg, threading.Event(), threading.Event())
    loop._idle_detector = FakeIdle(idle)
    loop._window_detector = FakeWindow(window_info)
    # Pretend the previous tick ran 30s ago so delta ≈ 30.
    loop._last_tick = datetime.now(timezone.utc) - timedelta(seconds=30)
    return loop


class TestTrackerLoop:
    def test_active_credits_todo(self):
        be = FakeBackend(active_todo_id=7)
        loop = _loop(be, idle=2, window_info=WindowInfo("code.exe", "x", False))
        loop._tick()
        t = be.ticks[-1]
        assert t["active_seconds"] >= 25          # ~30 - 2 idle
        assert t["todo_id"] == 7
        assert t["state"] == "active"
        assert t["excluded"] is False

    def test_idle_credits_nothing(self):
        be = FakeBackend(active_todo_id=7)
        loop = _loop(be, idle=500, window_info=WindowInfo("code.exe", "x", False))
        loop._tick()
        t = be.ticks[-1]
        assert t["active_seconds"] == 0
        assert t["todo_id"] is None
        assert t["state"] == "idle"

    def test_excluded_not_credited_to_todo(self):
        be = FakeBackend(active_todo_id=7)
        loop = _loop(be, idle=2, window_info=WindowInfo("vlc.exe", "x", True))
        loop._tick()
        t = be.ticks[-1]
        assert t["excluded"] is True
        assert t["todo_id"] is None
        assert t["state"] == "excluded"

    def test_paused_credits_nothing(self):
        be = FakeBackend(active_todo_id=7)
        loop = _loop(be, idle=2, window_info=WindowInfo("code.exe", "x", False))
        loop._pause.set()
        loop._tick()
        t = be.ticks[-1]
        assert t["active_seconds"] == 0
        assert t["state"] == "paused"

    def test_first_tick_anchors_without_push(self):
        be = FakeBackend(active_todo_id=7)
        cfg = {"poll_interval_seconds": 30, "idle_threshold_seconds": 60,
               "device_id": "dev"}
        loop = TrackerLoop(be, cfg, threading.Event(), threading.Event())
        loop._idle_detector = FakeIdle(2)
        loop._window_detector = FakeWindow(WindowInfo("code.exe", "x", False))
        loop._tick()  # _last_tick is None → just anchor
        assert be.ticks == []

    def test_sleep_delta_clamped(self):
        be = FakeBackend(active_todo_id=7)
        loop = _loop(be, idle=2, window_info=WindowInfo("code.exe", "x", False))
        loop._last_tick = datetime.now(timezone.utc) - timedelta(hours=8)
        loop._tick()
        # delta clamped to 2*poll=60; active ≈ 60-2
        assert be.ticks[-1]["active_seconds"] <= 60
