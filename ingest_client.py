"""HTTP client the local tracker uses to send measurements to the server.

Mirrors the subset of `DatabaseManager` that `TrackerLoop` calls so the two are
interchangeable. The tracker sends *duration increments* (ticks), each with a
unique event_id; the server accrues them idempotently. Failed POSTs are queued
to a local SQLite file and replayed — because ticks are idempotent, replay can
never double-count, and nothing is lost while offline.
"""
import json
import sqlite3
import threading
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
        self._queue_lock = threading.Lock()
        self._cached_today_total = 0
        self._cached_active_todo_id = None

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
        try:
            r = self._session.post(self._base + path, json=payload,
                                   headers=self._headers(), timeout=self._timeout)
            if r.status_code < 400:
                return r.json() if r.content else {}
            if r.status_code < 500:
                return None  # auth/permanent client error — don't queue
        except requests.RequestException:
            pass
        if queue_on_fail:
            self._enqueue(path, payload)
        return None

    def _get(self, path: str, params: dict = None) -> Optional[dict]:
        try:
            r = self._session.get(self._base + path, params=params,
                                  headers=self._headers(), timeout=self._timeout)
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

    def _delete_queued(self, row_id: int):
        with self._queue_lock:
            self._queue.execute("DELETE FROM pending_posts WHERE id = ?", (row_id,))
            self._queue.commit()

    def _drain_loop(self):
        while not self._shutdown.is_set():
            self._drain_once()
            self._shutdown.wait(self._retry_interval)

    def _drain_once(self):
        with self._queue_lock:
            rows = self._queue.execute(
                "SELECT id, path, payload_json FROM pending_posts ORDER BY id LIMIT 200"
            ).fetchall()
        for row_id, path, payload_json in rows:
            payload = json.loads(payload_json)
            try:
                r = self._session.post(self._base + path, json=payload,
                                       headers=self._headers(), timeout=self._timeout)
            except requests.RequestException:
                return  # network down — retry whole queue next interval
            if r.status_code < 400:
                self._delete_queued(row_id)        # applied (idempotent) — safe
            elif r.status_code < 500:
                self._delete_queued(row_id)         # permanent client error — drop
            else:
                return                              # 5xx — server may recover

    # ── DatabaseManager-compatible surface (tracker uses these) ──

    def push_tick(self, event_id: str, kst_date: str, active_seconds,
                  process_name: str = None, excluded: bool = False,
                  todo_id: int = None, state: str = None,
                  idle_seconds: float = 0, device_id: str = None):
        self._post("/api/ingest/tick", {
            "event_id": event_id,
            "kst_date": kst_date,
            "active_seconds": int(active_seconds or 0),
            "process_name": process_name,
            "excluded": bool(excluded),
            "todo_id": todo_id,
            "state": state,
            "idle_seconds": idle_seconds,
            "device_id": device_id or self._device_id,
        })

    def get_active_todo_id(self) -> Optional[int]:
        resp = self._get("/api/active-todo")
        if resp is None:
            return self._cached_active_todo_id
        self._cached_active_todo_id = resp.get("todo_id")
        return self._cached_active_todo_id

    def get_today_todo_total_seconds(self, date_kst: str) -> int:
        resp = self._get("/api/summary/today")
        if resp is None:
            return self._cached_today_total
        self._cached_today_total = resp.get("todo_total_seconds", 0)
        return self._cached_today_total

    def close(self):
        self._shutdown.set()
        try:
            self._queue.close()
        except Exception:
            pass
