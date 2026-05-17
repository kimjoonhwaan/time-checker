"""SQLite-backed store for TimeChecker.

After the simplification we keep only two real measurement tables:

  app_activity  — per-window foreground intervals (drives "앱별 사용 시간")
  todo_sessions — start/stop intervals per todo  (drives "오늘 작업 시간")

`sessions` is preserved for older rows but no longer written or read; it
exists only so prior data isn't lost.

The tracker owns a single state machine and is the only writer of activity
and todo_session rows, so we don't need separate liveness/cap logic for
two parallel systems.
"""
import sqlite3
import threading
from datetime import datetime, timedelta, timezone


class DatabaseManager:
    # Active todo session elapsed time is capped relative to the most recent
    # heartbeat from any device. If the tracker dies, the displayed counter
    # freezes within this grace window instead of growing forever.
    _HEARTBEAT_GRACE_SECONDS = 60

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()
        self._migrate()

    def _create_tables(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS app_activity (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                process_name     TEXT NOT NULL,
                window_title     TEXT,
                start_time       TEXT NOT NULL,
                end_time         TEXT,
                duration_seconds INTEGER,
                todo_id          INTEGER,
                device_id        TEXT,
                client_event_id  TEXT
            );
            CREATE TABLE IF NOT EXISTS todos (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                title             TEXT NOT NULL,
                status            TEXT NOT NULL DEFAULT 'todo',
                priority          TEXT NOT NULL DEFAULT 'medium',
                total_seconds     INTEGER NOT NULL DEFAULT 0,
                estimated_seconds INTEGER,
                notes             TEXT,
                created_at        TEXT NOT NULL,
                completed_at      TEXT
            );
            CREATE TABLE IF NOT EXISTS todo_sessions (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                todo_id          INTEGER REFERENCES todos(id) ON DELETE CASCADE,
                start_time       TEXT NOT NULL,
                end_time         TEXT,
                duration_seconds INTEGER,
                pause_reason     TEXT,
                device_id        TEXT
            );
            CREATE TABLE IF NOT EXISTS device_heartbeats (
                device_id TEXT PRIMARY KEY,
                time      TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_app_activity_start ON app_activity(start_time);
            CREATE INDEX IF NOT EXISTS idx_todos_status       ON todos(status);
            CREATE INDEX IF NOT EXISTS idx_todo_sessions_todo ON todo_sessions(todo_id);
            CREATE INDEX IF NOT EXISTS idx_todo_sessions_start ON todo_sessions(start_time);
        """)
        self._conn.commit()

    def _migrate(self):
        # Add columns on pre-existing DBs from older versions. Each silently
        # no-ops if the column is already present.
        migrations = [
            "ALTER TABLE app_activity ADD COLUMN todo_id INTEGER",
            "ALTER TABLE app_activity ADD COLUMN device_id TEXT",
            "ALTER TABLE app_activity ADD COLUMN client_event_id TEXT",
            "ALTER TABLE todo_sessions ADD COLUMN pause_reason TEXT",
            "ALTER TABLE todo_sessions ADD COLUMN device_id TEXT",
        ]
        for sql in migrations:
            try:
                self._conn.execute(sql)
            except Exception:
                pass
        try:
            self._conn.executescript("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_activity_client_event
                    ON app_activity(client_event_id) WHERE client_event_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_app_activity_todo
                    ON app_activity(todo_id);
            """)
        except Exception:
            pass
        self._conn.commit()

    # ── App activity (per-window intervals) ─────────────────────

    def open_app_activity(self, process_name: str, window_title: str,
                          start_time: str, todo_id: int = None,
                          device_id: str = None,
                          client_event_id: str = None) -> int:
        with self._lock:
            if client_event_id:
                row = self._conn.execute(
                    "SELECT id FROM app_activity WHERE client_event_id = ?",
                    (client_event_id,)
                ).fetchone()
                if row:
                    return row["id"]
            cur = self._conn.execute(
                """INSERT INTO app_activity
                       (process_name, window_title, start_time,
                        todo_id, device_id, client_event_id)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (process_name, window_title, start_time,
                 todo_id, device_id, client_event_id)
            )
            self._conn.commit()
            return cur.lastrowid

    def close_app_activity(self, activity_id: int, end_time: str):
        with self._lock:
            self._conn.execute("""
                UPDATE app_activity
                SET end_time = ?,
                    duration_seconds = MAX(0, CAST(
                        ROUND((julianday(?) - julianday(start_time)) * 86400) AS INTEGER
                    ))
                WHERE id = ? AND end_time IS NULL
            """, (end_time, end_time, activity_id))
            self._conn.commit()

    def get_app_breakdown(self, date_utc: str) -> list:
        cur = self._conn.execute("""
            SELECT process_name, SUM(duration_seconds) AS total_seconds
            FROM app_activity
            WHERE substr(start_time, 1, 10) = ?
              AND duration_seconds IS NOT NULL
            GROUP BY process_name
            ORDER BY total_seconds DESC
        """, (date_utc,))
        return [dict(row) for row in cur.fetchall()]

    # ── Todos ───────────────────────────────────────────────────

    def create_todo(self, title: str, priority: str = "medium",
                    estimated_seconds: int = None, notes: str = None) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO todos (title, priority, estimated_seconds, notes, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (title, priority, estimated_seconds, notes, now)
            )
            self._conn.commit()
            return cur.lastrowid

    def get_todos(self, status_filter: str = None) -> list:
        if status_filter:
            cur = self._conn.execute(
                "SELECT * FROM todos WHERE status = ? ORDER BY created_at DESC",
                (status_filter,)
            )
        else:
            cur = self._conn.execute(
                "SELECT * FROM todos ORDER BY "
                "CASE status WHEN 'in_progress' THEN 0 WHEN 'todo' THEN 1 "
                "WHEN 'paused' THEN 2 ELSE 3 END, created_at DESC"
            )
        return [dict(row) for row in cur.fetchall()]

    def get_todo(self, todo_id: int) -> dict:
        cur = self._conn.execute("SELECT * FROM todos WHERE id = ?", (todo_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def update_todo(self, todo_id: int, **fields):
        allowed = {"title", "priority", "estimated_seconds", "notes", "status"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [todo_id]
        with self._lock:
            self._conn.execute(
                f"UPDATE todos SET {set_clause} WHERE id = ?", values
            )
            self._conn.commit()

    def delete_todo(self, todo_id: int):
        with self._lock:
            self._conn.execute("DELETE FROM todos WHERE id = ?", (todo_id,))
            self._conn.commit()

    def get_active_todo_session(self) -> dict:
        cur = self._conn.execute(
            """SELECT ts.*, t.title FROM todo_sessions ts
               JOIN todos t ON ts.todo_id = t.id
               WHERE ts.end_time IS NULL LIMIT 1"""
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def get_recently_auto_paused_todo(self, within_seconds: int = 3600) -> dict:
        """Most recently paused todo whose last todo_session was closed by
        the tracker (reason 'idle' or 'excluded:*'). Tracker queries this on
        TRACKING transition to know what to resume."""
        cur = self._conn.execute("""
            SELECT t.id AS todo_id, t.title, ts.end_time, ts.pause_reason
            FROM todos t
            JOIN todo_sessions ts ON ts.todo_id = t.id
            WHERE t.status = 'paused'
              AND ts.id = (SELECT MAX(id) FROM todo_sessions WHERE todo_id = t.id)
              AND ts.end_time IS NOT NULL
              AND (ts.pause_reason = 'idle' OR ts.pause_reason LIKE 'excluded:%')
              AND (julianday('now') - julianday(ts.end_time)) * 86400 <= ?
            ORDER BY ts.end_time DESC
            LIMIT 1
        """, (within_seconds,))
        row = cur.fetchone()
        return dict(row) if row else None

    def start_todo_timer(self, todo_id: int) -> int:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._lock:
            # Auto-pause any currently open todo using a heartbeat-aware end.
            active = self._conn.execute(
                "SELECT id, todo_id, start_time FROM todo_sessions "
                "WHERE end_time IS NULL LIMIT 1"
            ).fetchone()
            if active:
                cutoff = self._live_cutoff_iso_unlocked()
                end = max(active["start_time"], min(cutoff, now_iso))
                self._close_todo_session_locked(
                    active["id"], active["todo_id"], end, "interrupted"
                )
            self._conn.execute(
                "UPDATE todos SET status = 'in_progress' WHERE id = ?", (todo_id,)
            )
            cur = self._conn.execute(
                "INSERT INTO todo_sessions (todo_id, start_time) VALUES (?, ?)",
                (todo_id, now_iso)
            )
            self._conn.commit()
            return cur.lastrowid

    def stop_todo_timer(self, todo_id: int, reason: str = 'manual',
                        end_time: str = None):
        with self._lock:
            session = self._conn.execute(
                "SELECT id, start_time FROM todo_sessions "
                "WHERE todo_id = ? AND end_time IS NULL",
                (todo_id,)
            ).fetchone()
            now_iso = datetime.now(timezone.utc).isoformat()
            effective = end_time or now_iso
            if session:
                # Clamp: end >= start, end <= now
                if effective < session["start_time"]:
                    effective = session["start_time"]
                if effective > now_iso:
                    effective = now_iso
                self._close_todo_session_locked(
                    session["id"], todo_id, effective, reason
                )
            self._conn.execute(
                "UPDATE todos SET status = 'paused' "
                "WHERE id = ? AND status = 'in_progress'",
                (todo_id,)
            )
            self._conn.commit()

    def complete_todo(self, todo_id: int):
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            session = self._conn.execute(
                "SELECT id FROM todo_sessions "
                "WHERE todo_id = ? AND end_time IS NULL",
                (todo_id,)
            ).fetchone()
            if session:
                self._close_todo_session_locked(session["id"], todo_id, now, 'completed')
            self._conn.execute(
                "UPDATE todos SET status = 'done', completed_at = ? WHERE id = ?",
                (now, todo_id)
            )
            self._conn.commit()

    def _close_todo_session_locked(self, session_id: int, todo_id: int,
                                   end_time: str, reason: str = None):
        self._conn.execute("""
            UPDATE todo_sessions
            SET end_time = ?,
                duration_seconds = MAX(0, CAST(
                    ROUND((julianday(?) - julianday(start_time)) * 86400) AS INTEGER
                )),
                pause_reason = ?
            WHERE id = ?
        """, (end_time, end_time, reason, session_id))
        self._conn.execute("""
            UPDATE todos SET total_seconds = (
                SELECT COALESCE(SUM(duration_seconds), 0)
                FROM todo_sessions WHERE todo_id = ? AND end_time IS NOT NULL
            ) WHERE id = ?
        """, (todo_id, todo_id))

    def get_todo_history(self, todo_id: int) -> list:
        cur = self._conn.execute("""
            SELECT start_time, end_time, duration_seconds, pause_reason
            FROM todo_sessions WHERE todo_id = ?
            ORDER BY start_time ASC
        """, (todo_id,))
        return [dict(row) for row in cur.fetchall()]

    # ── Today / summary queries (all UTC, todo-driven) ──────────

    def get_today_todo_total_seconds(self, date_utc: str) -> int:
        """Sum of todo session time today, plus live elapsed of any active
        session whose start_time falls on today."""
        closed = self._conn.execute("""
            SELECT COALESCE(SUM(duration_seconds), 0)
            FROM todo_sessions
            WHERE end_time IS NOT NULL
              AND substr(start_time, 1, 10) = ?
        """, (date_utc,)).fetchone()[0]
        row = self._conn.execute("""
            SELECT start_time FROM todo_sessions
            WHERE end_time IS NULL
              AND substr(start_time, 1, 10) = ?
            ORDER BY id DESC LIMIT 1
        """, (date_utc,)).fetchone()
        active = 0
        if row:
            try:
                start = datetime.fromisoformat(row["start_time"])
                if start.tzinfo is None:
                    start = start.replace(tzinfo=timezone.utc)
                active = max(0, int(
                    (self._live_cutoff() - start).total_seconds()
                ))
            except Exception:
                pass
        return closed + active

    def get_completed_today_count(self, date_utc: str) -> int:
        cur = self._conn.execute(
            "SELECT COUNT(*) FROM todos "
            "WHERE status = 'done' AND substr(completed_at, 1, 10) = ?",
            (date_utc,)
        )
        return cur.fetchone()[0]

    def get_daily_todo_summary(self, days: int = 14) -> list:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        cur = self._conn.execute("""
            SELECT substr(start_time, 1, 10) AS date,
                   COALESCE(SUM(duration_seconds), 0) AS total_seconds
            FROM todo_sessions
            WHERE end_time IS NOT NULL
              AND substr(start_time, 1, 10) >= ?
            GROUP BY date ORDER BY date
        """, (cutoff,))
        return [dict(row) for row in cur.fetchall()]

    def get_weekly_todo_totals(self, weeks: int = 8) -> list:
        cutoff = (datetime.now(timezone.utc) - timedelta(weeks=weeks)).strftime("%Y-%m-%d")
        cur = self._conn.execute("""
            SELECT strftime('%Y-%W', substr(start_time, 1, 10)) AS week,
                   COALESCE(SUM(duration_seconds), 0) AS total_seconds
            FROM todo_sessions
            WHERE end_time IS NOT NULL
              AND substr(start_time, 1, 10) >= ?
            GROUP BY week ORDER BY week
        """, (cutoff,))
        return [dict(row) for row in cur.fetchall()]

    def get_monthly_todo_totals(self, months: int = 6) -> list:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=months * 31)).strftime("%Y-%m-%d")
        cur = self._conn.execute("""
            SELECT strftime('%Y-%m', substr(start_time, 1, 10)) AS month,
                   COALESCE(SUM(duration_seconds), 0) AS total_seconds
            FROM todo_sessions
            WHERE end_time IS NOT NULL
              AND substr(start_time, 1, 10) >= ?
            GROUP BY month ORDER BY month
        """, (cutoff,))
        return [dict(row) for row in cur.fetchall()]

    # ── Heartbeat / live cutoff ─────────────────────────────────

    def record_heartbeat(self, device_id: str, time_iso: str):
        if not device_id:
            return
        with self._lock:
            self._conn.execute(
                "INSERT INTO device_heartbeats(device_id, time) VALUES(?, ?) "
                "ON CONFLICT(device_id) DO UPDATE SET time = excluded.time",
                (device_id, time_iso)
            )
            self._conn.commit()

    def _live_cutoff(self) -> datetime:
        """Effective 'now' for active-session elapsed displays. If the most
        recent heartbeat is within the grace window, use real now; otherwise
        freeze at (last_heartbeat + grace) so abandoned sessions stop growing."""
        now = datetime.now(timezone.utc)
        row = self._conn.execute(
            "SELECT MAX(time) AS x FROM device_heartbeats"
        ).fetchone()
        last = row["x"] if row else None
        if not last:
            return now
        try:
            last_dt = datetime.fromisoformat(last)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            grace_end = last_dt + timedelta(seconds=self._HEARTBEAT_GRACE_SECONDS)
            return grace_end if grace_end < now else now
        except Exception:
            return now

    def _live_cutoff_iso_unlocked(self) -> str:
        """Same as _live_cutoff().isoformat() but safe to call while
        self._lock is already held."""
        return self._live_cutoff().isoformat()

    # ── Cleanup ─────────────────────────────────────────────────

    def cleanup_orphan_todo_sessions(self, stale_seconds: int = 14400) -> dict:
        """Close any todo_session that's been open longer than stale_seconds
        using the heartbeat cutoff as end_time. Mark its todo paused.
        Returns a small report."""
        with self._lock:
            cutoff_iso = self._live_cutoff_iso_unlocked()
            now_iso = datetime.now(timezone.utc).isoformat()
            stale = self._conn.execute(f"""
                SELECT id, todo_id, start_time FROM todo_sessions
                WHERE end_time IS NULL
                  AND (julianday('now') - julianday(start_time)) * 86400 >= {stale_seconds}
            """).fetchall()
            closed = 0
            for r in stale:
                end = max(r["start_time"], min(cutoff_iso, now_iso))
                self._close_todo_session_locked(
                    r["id"], r["todo_id"], end, "abandoned"
                )
                self._conn.execute(
                    "UPDATE todos SET status='paused' "
                    "WHERE id=? AND status='in_progress'",
                    (r["todo_id"],)
                )
                closed += 1
            # Also close any app_activity rows older than the cap.
            self._conn.execute(f"""
                UPDATE app_activity
                SET end_time = ?,
                    duration_seconds = MAX(0, CAST(
                        ROUND((julianday(?) - julianday(start_time)) * 86400) AS INTEGER
                    ))
                WHERE end_time IS NULL
                  AND (julianday('now') - julianday(start_time)) * 86400 >= {stale_seconds}
            """, (cutoff_iso, cutoff_iso))
            closed_activities = self._conn.total_changes
            self._conn.commit()
            return {
                "closed_todo_sessions": closed,
                "closed_activities_estimate": closed_activities,
            }

    def close(self):
        self._conn.close()
