"""Tracker — the single time authority.

Every poll it measures how much *active* time elapsed since the last poll and
pushes that duration to the backend, which accrues it. There is no session to
open/close and no timestamp subtraction on the read side:

    delta  = clamp(now - last_tick, 0, 2*poll)   # sleep-safe
    idle   = seconds since last keyboard/mouse input
    active = max(0, delta - idle)                 # idle is excluded intrinsically

`active` is attributed to the currently selected todo (the one the dashboard
marked in_progress) unless the foreground app is excluded. Idle/excluded
"auto-pause" is emergent: when idle or excluded, `active` is ~0 so nothing
accrues; returning to work resumes accrual on the next tick automatically.
"""
import ctypes
import ctypes.wintypes
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
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


class TrackerLoop:
    def __init__(self, backend, config: dict,
                 shutdown_event: threading.Event, pause_event: threading.Event):
        self._backend = backend
        self._config = config
        self._shutdown = shutdown_event
        self._pause = pause_event
        self._idle_detector = IdleDetector()
        self._window_detector = WindowDetector(
            config.get("excluded_processes", []),
            config.get("excluded_title_keywords", [])
        )
        self._poll = config.get("poll_interval_seconds", 10)
        self._threshold = config.get("idle_threshold_seconds", 60)
        self._device_id = config.get("device_id")
        self._last_tick = None  # monotonic-free: use wall clock, clamp protects us
        self._last_idle = 0.0
        self._last_state = "idle"
        self._last_excluded_app = None

    def run(self):
        while not self._shutdown.is_set():
            try:
                self._tick()
            except Exception:
                pass
            self._shutdown.wait(timeout=self._poll)

    def _tick(self):
        now = datetime.now(timezone.utc)
        if self._last_tick is None:
            # First tick after start: anchor, accrue nothing this round.
            self._last_tick = now
            return
        delta = (now - self._last_tick).total_seconds()
        self._last_tick = now
        # Sleep/suspend safety: never credit more than two polls of wall time.
        delta = max(0.0, min(delta, 2 * self._poll))

        idle = self._idle_detector.get_idle_seconds()
        self._last_idle = idle

        if self._pause.is_set():
            active = 0.0
            window = None
            excluded = False
            self._last_state = "paused"
        else:
            window = self._window_detector.get_active_window()
            excluded = bool(window and window.is_excluded)
            active = max(0.0, delta - idle)
            if idle >= self._threshold:
                self._last_state = "idle"
            elif excluded:
                self._last_state = "excluded"
            else:
                self._last_state = "active"
        self._last_excluded_app = window.process_name if excluded else None

        active_todo_id = None
        try:
            active_todo_id = self._backend.get_active_todo_id()
        except Exception:
            pass

        proc = window.process_name if window else "unknown"
        credited_todo = active_todo_id if (active > 0 and not excluded) else None
        try:
            self._backend.push_tick(
                event_id=str(uuid.uuid4()),
                kst_date=self._kst_date(now),
                active_seconds=int(round(active)),
                process_name=proc,
                excluded=excluded,
                todo_id=credited_todo,
                state=self._last_state,
                idle_seconds=idle,
                device_id=self._device_id,
            )
        except Exception:
            pass

        # Day rollover: in LOCAL mode the backend can complete crossed todos.
        if hasattr(self._backend, "complete_day_crossed_todos"):
            try:
                self._backend.complete_day_crossed_todos()
            except Exception:
                pass

    @staticmethod
    def _kst_date(dt_utc: datetime) -> str:
        from datetime import timedelta
        return (dt_utc.astimezone(timezone(timedelta(hours=9)))).strftime("%Y-%m-%d")

    # ── Public API (tray / shutdown) ────────────────────────────

    def close_active_session(self):
        # Nothing to flush: time is accrued per tick, so there is no open
        # interval to close. A final tick already credited elapsed active time.
        pass

    def get_status(self) -> dict:
        date_kst = self._kst_date(datetime.now(timezone.utc))
        try:
            total = self._backend.get_today_todo_total_seconds(date_kst)
        except Exception:
            total = 0
        return {
            "state": "paused" if self._pause.is_set() else self._last_state,
            "today_total_seconds": total,
            "excluded_app": self._last_excluded_app,
            "idle_seconds": self._last_idle,
        }
