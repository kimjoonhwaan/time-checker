"""HTTP client for posting tracker measurements to a remote TimeChecker server.

Mirrors the subset of `DatabaseManager` that `TrackerLoop` calls, so it can be
substituted in `main.py` without changing tracker code.

Network failures don't lose data: failed POSTs are persisted to a local SQLite
queue and replayed by a background worker.

Session/activity handles returned by `open_*` methods are **client UUIDs**
(strings), not server-assigned integers. All subsequent calls reference these
UUIDs via `session_client_event_id` / `activity_client_event_id`, so the
tracker can keep working while offline.
"""
import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests


class IngestClient:
    def __init__(self, server_url: str, api_key: str, queue_db_path: str,
                 device_id: str, timeout: float = 5.0,
                 retry_interval: float = 30.0):
        self._base = server_url.rstrip("/")
        self._api_key = api_key
        self._device_id = device_id
        self._timeout = timeout
        self._retry_interval = retry_interval
        self._session = requests.Session()
        self._lock = threading.Lock()
        self._queue_lock = threading.Lock()
        self._cached_today_total = 0
        self._cached_today_date: Optional[str] = None
        self._cached_active_todo: Optional[dict] = None

        Path(queue_db_path).parent.mkdir(parents=True, exist_ok=True)
        self._queue = sqlite3.connect(queue_db_path, check_same_thread=False)
        self._queue.execute("""
            CREATE TABLE IF NOT EXISTS pending_posts (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                path         TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at   TEXT NOT NULL
            )
        """)
        self._queue.commit()

        self._shutdown = threading.Event()
        self._worker = threading.Thread(
            target=self._drain_loop, name="ingest-drain", daemon=True
        )
        self._worker.start()

    # ── HTTP plumbing ────────────────────────────────────────────

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self._api_key:
            h["X-API-Key"] = self._api_key
        return h

    def _post(self, path: str, payload: dict, queue_on_fail: bool = True) -> Optional[dict]:
        """Best-effort POST. Returns response JSON on success, None otherwise.
        Queues the payload for retry on any network/HTTP error if queue_on_fail."""
        url = self._base + path
        try:
            r = self._session.post(url, json=payload, headers=self._headers(),
                                   timeout=self._timeout)
            if r.status_code < 400:
                return r.json() if r.content else {}
            if r.status_code == 401:
                # Auth issue — don't queue, it'll never succeed.
                return None
        except requests.RequestException:
            pass
        if queue_on_fail:
            self._enqueue(path, payload)
        return None

    def _get(self, path: str, params: dict = None) -> Optional[dict]:
        url = self._base + path
        try:
            r = self._session.get(url, params=params, headers=self._headers(),
                                  timeout=self._timeout)
            if r.status_code < 400:
                return r.json() if r.content else {}
        except requests.RequestException:
            pass
        return None

    def _enqueue(self, path: str, payload: dict):
        with self._queue_lock:
            self._queue.execute(
                "INSERT INTO pending_posts (path, payload_json, created_at) VALUES (?, ?, ?)",
                (path, json.dumps(payload), datetime.now(timezone.utc).isoformat())
            )
            self._queue.commit()

    def _drain_loop(self):
        while not self._shutdown.is_set():
            self._drain_once()
            self._shutdown.wait(self._retry_interval)

    def _drain_once(self):
        with self._queue_lock:
            rows = self._queue.execute(
                "SELECT id, path, payload_json FROM pending_posts ORDER BY id LIMIT 100"
            ).fetchall()
        for row_id, path, payload_json in rows:
            payload = json.loads(payload_json)
            url = self._base + path
            try:
                r = self._session.post(url, json=payload, headers=self._headers(),
                                       timeout=self._timeout)
                if r.status_code < 400:
                    with self._queue_lock:
                        self._queue.execute("DELETE FROM pending_posts WHERE id = ?", (row_id,))
                        self._queue.commit()
                elif r.status_code == 401:
                    # Stop draining; auth needs to be fixed first.
                    return
                else:
                    # Other 4xx/5xx — keep in queue for next round.
                    return
            except requests.RequestException:
                return  # network still down, stop draining

    # ── DatabaseManager-compatible surface ──────────────────────

    def open_session(self, start_time: str, date: str) -> str:
        eid = str(uuid.uuid4())
        self._post("/api/ingest/session/open", {
            "client_event_id": eid,
            "start_time": start_time,
            "date": date,
            "device_id": self._device_id,
        })
        return eid

    def close_session(self, session_id, end_time: str):
        # session_id here is the client UUID we returned from open_session.
        self._post("/api/ingest/session/close", {
            "session_client_event_id": session_id,
            "end_time": end_time,
        })

    def open_app_activity(self, session_id, process_name: str,
                          window_title: str, start_time: str) -> str:
        eid = str(uuid.uuid4())
        self._post("/api/ingest/activity/open", {
            "client_event_id": eid,
            "session_client_event_id": session_id,
            "process_name": process_name,
            "window_title": window_title,
            "start_time": start_time,
            "device_id": self._device_id,
        })
        return eid

    def close_app_activity(self, activity_id, end_time: str):
        self._post("/api/ingest/activity/close", {
            "activity_client_event_id": activity_id,
            "end_time": end_time,
        })

    def close_stale_sessions(self):
        # Server-side concern — no-op for the client.
        return

    def get_active_todo_session(self) -> Optional[dict]:
        resp = self._get("/api/ingest/todo/active")
        if resp is None:
            return self._cached_active_todo
        self._cached_active_todo = resp.get("active")
        return self._cached_active_todo

    def get_recently_auto_paused_todo(self) -> Optional[dict]:
        resp = self._get("/api/ingest/todo/auto_paused")
        if resp is None:
            return None
        return resp.get("todo")

    def start_todo_timer(self, todo_id: int) -> Optional[int]:
        resp = self._post("/api/ingest/todo/start", {"todo_id": int(todo_id)})
        return resp.get("todo_session_id") if resp else None

    def stop_todo_timer(self, todo_id: int, reason: str = "manual", end_time: str = None):
        payload = {"todo_id": int(todo_id), "reason": reason}
        if end_time:
            payload["end_time"] = end_time
        self._post("/api/ingest/todo/stop", payload)

    def get_today_total_seconds(self, date: str) -> int:
        resp = self._get("/api/summary/today")
        if resp is None:
            return self._cached_today_total
        self._cached_today_total = resp.get("total_seconds", 0)
        self._cached_today_date = date
        return self._cached_today_total

    def heartbeat(self, state: str, idle_seconds: float, excluded_app: Optional[str]):
        self._post("/api/ingest/heartbeat", {
            "device_id": self._device_id,
            "state": state,
            "idle_seconds": idle_seconds,
            "excluded_app": excluded_app,
        }, queue_on_fail=False)  # heartbeats are ephemeral, don't queue stale ones

    def close(self):
        self._shutdown.set()
        try:
            self._queue.close()
        except Exception:
            pass
