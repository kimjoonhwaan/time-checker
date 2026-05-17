import ctypes
import ctypes.wintypes
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

import psutil
import win32gui
import win32process

from database import DatabaseManager


@dataclass
class WindowInfo:
    process_name: str
    window_title: str
    is_excluded: bool


class IdleDetector:
    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.wintypes.UINT), ("dwTime", ctypes.wintypes.DWORD)]

    def get_idle_seconds(self) -> float:
        lii = self.LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(lii)
        ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii))
        millis = ctypes.windll.kernel32.GetTickCount() - lii.dwTime
        return millis / 1000.0

    def is_idle(self, threshold_seconds: int) -> bool:
        return self.get_idle_seconds() >= threshold_seconds


class WindowDetector:
    def __init__(self, excluded_processes: list, excluded_keywords: list):
        self._excluded_processes = [p.lower() for p in excluded_processes]
        self._excluded_keywords = [k.lower() for k in excluded_keywords]

    def get_active_window(self) -> Optional[WindowInfo]:
        try:
            hwnd = win32gui.GetForegroundWindow()
            if not hwnd:
                return None
            title = win32gui.GetWindowText(hwnd)
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            process_name = psutil.Process(pid).name()
            excluded = self._is_excluded(process_name, title)
            return WindowInfo(process_name=process_name, window_title=title, is_excluded=excluded)
        except Exception:
            return WindowInfo(process_name="unknown", window_title="", is_excluded=True)

    def _is_excluded(self, process_name: str, title: str) -> bool:
        pname = process_name.lower()
        if any(pname == ep or pname.endswith(ep) for ep in self._excluded_processes):
            return True
        title_lower = title.lower()
        return any(kw in title_lower for kw in self._excluded_keywords)


class TrackerState:
    IDLE = "idle"
    TRACKING = "tracking"
    EXCLUDED = "excluded"
    PAUSED = "paused"


class TrackerLoop:
    def __init__(self, db: DatabaseManager, config: dict,
                 shutdown_event: threading.Event, pause_event: threading.Event):
        self._db = db
        self._config = config
        self._shutdown = shutdown_event
        self._pause = pause_event
        self._idle_detector = IdleDetector()
        self._window_detector = WindowDetector(
            config.get("excluded_processes", []),
            config.get("excluded_title_keywords", [])
        )
        self._state = TrackerState.IDLE
        self._current_session_id: Optional[int] = None
        self._current_activity_id: Optional[int] = None
        self._current_window: Optional[WindowInfo] = None
        self._excluded_app_name: Optional[str] = None
        self._auto_paused_todo_id: Optional[int] = None
        self._lock = threading.Lock()
        self._resume_start_time: Optional[str] = None

    def run(self):
        poll = self._config.get("poll_interval_seconds", 30)
        while not self._shutdown.is_set():
            self._tick()
            self._shutdown.wait(timeout=poll)

    def _tick(self):
        if self._pause.is_set():
            if self._state == TrackerState.TRACKING:
                self._end_session()
            self._state = TrackerState.PAUSED
            return

        idle_secs = self._idle_detector.get_idle_seconds()
        threshold = self._config.get("idle_threshold_seconds", 60)

        if idle_secs >= threshold:
            idle_start = datetime.now(timezone.utc) - timedelta(seconds=idle_secs)
            if self._state == TrackerState.TRACKING:
                self._end_session(end_time=idle_start)
            if self._state != TrackerState.IDLE:
                self._auto_pause_active_todo("idle", end_time_iso=idle_start.isoformat())
            self._state = TrackerState.IDLE
            self._excluded_app_name = None
            return

        window = self._window_detector.get_active_window()

        if window is not None and window.is_excluded:
            if self._state == TrackerState.TRACKING:
                self._end_session()
            if self._state != TrackerState.EXCLUDED:
                self._auto_pause_active_todo(f"excluded:{window.process_name}")
            self._state = TrackerState.EXCLUDED
            self._excluded_app_name = window.process_name
            return

        # Active state: non-excluded window OR no window (None) — both count as working
        prev_state = self._state
        self._excluded_app_name = None

        if prev_state != TrackerState.TRACKING:
            # Resume tracking
            win_to_use = window if window is not None else WindowInfo("unknown", "", False)
            self._start_session(win_to_use)
            self._state = TrackerState.TRACKING
            self._auto_resume_todo_if_needed()
        elif window is not None:
            self._handle_window_change(window)

    def _auto_pause_active_todo(self, reason: str = "idle", end_time_iso: Optional[str] = None):
        if self._auto_paused_todo_id:
            return
        try:
            active = self._db.get_active_todo_session()
            if active:
                kwargs = {"reason": reason}
                if end_time_iso:
                    kwargs["end_time"] = end_time_iso
                self._db.stop_todo_timer(active["todo_id"], **kwargs)
                self._auto_paused_todo_id = active["todo_id"]
        except Exception:
            pass

    def _auto_resume_todo_if_needed(self):
        if self._auto_paused_todo_id:
            try:
                self._db.start_todo_timer(self._auto_paused_todo_id)
            except Exception:
                pass
            self._auto_paused_todo_id = None

    def set_resume_start_time(self, iso_time: str):
        """One-shot hint: backdate the next opened session to this start time.

        Used right after a restart so that a small gap between the previous
        tracker shutdown and now is not lost.
        """
        self._resume_start_time = iso_time

    def close_active_session(self):
        """Force-close any in-flight session and activity. Safe to call from
        the main thread on shutdown."""
        self._end_session()

    def _start_session(self, window: WindowInfo):
        now = datetime.now(timezone.utc)
        date = now.strftime("%Y-%m-%d")
        start_iso = self._resume_start_time or now.isoformat()
        self._resume_start_time = None  # consume once
        with self._lock:
            self._current_session_id = self._db.open_session(start_iso, date)
            self._current_activity_id = self._db.open_app_activity(
                self._current_session_id, window.process_name, window.window_title, start_iso
            )
            self._current_window = window

    def _end_session(self, end_time: Optional[datetime] = None):
        if self._current_session_id is None:
            return
        if end_time is None:
            end_time = datetime.now(timezone.utc)
        end_iso = end_time.isoformat()
        with self._lock:
            if self._current_activity_id is not None:
                self._db.close_app_activity(self._current_activity_id, end_iso)
                self._current_activity_id = None
            self._db.close_session(self._current_session_id, end_iso)
            self._current_session_id = None
            self._current_window = None

    def _handle_window_change(self, new_window: WindowInfo):
        if (self._current_window and
                new_window.process_name == self._current_window.process_name and
                new_window.window_title == self._current_window.window_title):
            return
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._lock:
            if self._current_activity_id is not None:
                self._db.close_app_activity(self._current_activity_id, now_iso)
            self._current_activity_id = self._db.open_app_activity(
                self._current_session_id,
                new_window.process_name,
                new_window.window_title,
                now_iso
            )
            self._current_window = new_window

    def get_status(self) -> dict:
        with self._lock:
            state = self._state
            if self._pause.is_set():
                state = TrackerState.PAUSED
            date = datetime.now().strftime("%Y-%m-%d")
            total = self._db.get_today_total_seconds(date)
            return {
                "state": state,
                "today_total_seconds": total,
                "excluded_app": self._excluded_app_name,
                "idle_seconds": self._idle_detector.get_idle_seconds(),
            }
