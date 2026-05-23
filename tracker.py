"""Foreground-app + idle detection tracker.

Single state machine:

  active   — user is at the keyboard, foreground app is not excluded
  paused   — idle threshold crossed, an excluded app is in front, or user
             manually paused. While paused, no app_activity row is open
             and any active todo is auto-paused.

Every state transition closes the previous app_activity row (with a
backdated end_time when we know one — e.g. idle_start) and either opens a
new one or leaves the slot empty. Todo pause/resume rides on the same
transition so the two never disagree about whether time is being counted.
"""
import ctypes
import ctypes.wintypes
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import psutil
import win32gui
import win32process


@dataclass
class WindowInfo:
    process_name: str
    window_title: str
    is_excluded: bool


class IdleDetector:
    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.wintypes.UINT),
                    ("dwTime", ctypes.wintypes.DWORD)]

    def get_idle_seconds(self) -> float:
        lii = self.LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(lii)
        ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii))
        millis = ctypes.windll.kernel32.GetTickCount() - lii.dwTime
        return millis / 1000.0


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
            return WindowInfo(
                process_name=process_name,
                window_title=title,
                is_excluded=self._is_excluded(process_name, title),
            )
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
    def __init__(self, db, config: dict,
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
        self._current_activity_id = None  # opaque handle (UUID string or int)
        self._current_window: Optional[WindowInfo] = None
        self._excluded_app_name: Optional[str] = None
        self._current_todo_id: Optional[int] = None  # in-progress todo (for activity tagging)
        self._auto_paused_todo_id: Optional[int] = None
        self._lock = threading.Lock()

    def run(self):
        poll = self._config.get("poll_interval_seconds", 30)
        while not self._shutdown.is_set():
            self._tick()
            self._shutdown.wait(timeout=poll)

    # ── Main tick ────────────────────────────────────────────────

    def _tick(self):
        # LOCAL mode (direct DatabaseManager): auto-complete any todo that ran
        # past the KST day boundary. REMOTE mode's _db is an IngestClient that
        # lacks this method — there the server's heartbeat handler does it.
        if hasattr(self._db, "complete_day_crossed_todos"):
            try:
                self._db.complete_day_crossed_todos()
            except Exception:
                pass

        if self._pause.is_set():
            self._transition_to_paused(TrackerState.PAUSED, "manual")
            return

        idle_secs = self._idle_detector.get_idle_seconds()
        threshold = self._config.get("idle_threshold_seconds", 60)
        if idle_secs >= threshold:
            idle_start = datetime.now(timezone.utc) - timedelta(seconds=idle_secs)
            self._transition_to_paused(TrackerState.IDLE, "idle",
                                       end_iso=idle_start.isoformat())
            return

        window = self._window_detector.get_active_window()
        if window is not None and window.is_excluded:
            self._transition_to_paused(TrackerState.EXCLUDED,
                                       f"excluded:{window.process_name}")
            self._excluded_app_name = window.process_name
            return

        self._transition_to_active(window or WindowInfo("unknown", "", False))

    # ── State transitions ───────────────────────────────────────

    def _transition_to_paused(self, new_state: str, reason: str,
                              end_iso: Optional[str] = None):
        """Close the current activity (if any) and pause the active todo
        (if any), using `end_iso` if provided otherwise now."""
        end_iso = end_iso or datetime.now(timezone.utc).isoformat()
        with self._lock:
            if self._current_activity_id is not None:
                try:
                    self._db.close_app_activity(self._current_activity_id, end_iso)
                except Exception:
                    pass
                self._current_activity_id = None
                self._current_window = None
        # Auto-pause the active todo. We don't hold _lock for the
        # potentially-HTTP server call.
        if self._state == TrackerState.TRACKING and self._auto_paused_todo_id is None:
            self._pause_active_todo(reason, end_iso)
        self._state = new_state
        if new_state != TrackerState.EXCLUDED:
            self._excluded_app_name = None

    def _transition_to_active(self, window: WindowInfo):
        was_paused = self._state != TrackerState.TRACKING
        if was_paused:
            self._resume_auto_paused_todo()
            now_iso = datetime.now(timezone.utc).isoformat()
            self._open_activity(window, now_iso)
            self._state = TrackerState.TRACKING
            self._excluded_app_name = None
        else:
            # Already tracking — handle window change.
            if (self._current_window and
                    window.process_name == self._current_window.process_name and
                    window.window_title == self._current_window.window_title):
                return
            now_iso = datetime.now(timezone.utc).isoformat()
            with self._lock:
                if self._current_activity_id is not None:
                    try:
                        self._db.close_app_activity(self._current_activity_id, now_iso)
                    except Exception:
                        pass
            self._open_activity(window, now_iso)

    def _open_activity(self, window: WindowInfo, start_iso: str):
        with self._lock:
            try:
                self._current_activity_id = self._db.open_app_activity(
                    process_name=window.process_name,
                    window_title=window.window_title,
                    start_time=start_iso,
                    todo_id=self._current_todo_id,
                )
            except Exception:
                self._current_activity_id = None
            self._current_window = window

    # ── Todo auto pause / resume ────────────────────────────────

    def _pause_active_todo(self, reason: str, end_iso: str):
        try:
            active = self._db.get_active_todo_session()
        except Exception:
            active = None
        if not active:
            return
        try:
            self._db.stop_todo_timer(active["todo_id"], reason=reason, end_time=end_iso)
            self._auto_paused_todo_id = active["todo_id"]
            self._current_todo_id = None
        except Exception:
            pass

    def _resume_auto_paused_todo(self):
        todo_id = self._auto_paused_todo_id
        if not todo_id:
            # Memory might be empty (e.g. tracker restart). Ask the server.
            try:
                if hasattr(self._db, "get_recently_auto_paused_todo"):
                    row = self._db.get_recently_auto_paused_todo()
                    if row:
                        todo_id = row["todo_id"]
            except Exception:
                pass
        if not todo_id:
            return
        try:
            sid = self._db.start_todo_timer(todo_id)
            if sid:  # None means the todo was completed → never resurrect it
                self._current_todo_id = todo_id
        except Exception:
            pass
        self._auto_paused_todo_id = None

    # ── Public API ──────────────────────────────────────────────

    def close_active_session(self):
        """Force-close any in-flight activity. Called on shutdown."""
        if self._current_activity_id is not None or self._current_todo_id is not None:
            self._transition_to_paused(TrackerState.IDLE, "shutdown")

    def get_status(self) -> dict:
        with self._lock:
            state = self._state
            if self._pause.is_set():
                state = TrackerState.PAUSED
            date_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            try:
                total = self._db.get_today_todo_total_seconds(date_utc)
            except Exception:
                total = 0
            return {
                "state": state,
                "today_total_seconds": total,
                "excluded_app": self._excluded_app_name,
                "idle_seconds": self._idle_detector.get_idle_seconds(),
            }
