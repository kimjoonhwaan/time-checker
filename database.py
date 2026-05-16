import sqlite3
import threading
from datetime import datetime, timezone


class DatabaseManager:
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
            CREATE TABLE IF NOT EXISTS sessions (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                start_time     TEXT NOT NULL,
                end_time       TEXT,
                total_seconds  INTEGER,
                date           TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS app_activity (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id       INTEGER REFERENCES sessions(id) ON DELETE CASCADE,
                process_name     TEXT NOT NULL,
                window_title     TEXT,
                start_time       TEXT NOT NULL,
                end_time         TEXT,
                duration_seconds INTEGER
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
                duration_seconds INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_date ON sessions(date);
            CREATE INDEX IF NOT EXISTS idx_app_activity_session ON app_activity(session_id);
            CREATE INDEX IF NOT EXISTS idx_todos_status ON todos(status);
            CREATE INDEX IF NOT EXISTS idx_todo_sessions_todo ON todo_sessions(todo_id);
        """)
        self._conn.commit()

    def _migrate(self):
        migrations = [
            "ALTER TABLE todo_sessions ADD COLUMN pause_reason TEXT",
            "ALTER TABLE sessions ADD COLUMN device_id TEXT",
            "ALTER TABLE sessions ADD COLUMN client_event_id TEXT",
            "ALTER TABLE app_activity ADD COLUMN device_id TEXT",
            "ALTER TABLE app_activity ADD COLUMN client_event_id TEXT",
            "ALTER TABLE todos ADD COLUMN device_id TEXT",
            "ALTER TABLE todo_sessions ADD COLUMN device_id TEXT",
            "ALTER TABLE todo_sessions ADD COLUMN client_event_id TEXT",
        ]
        for sql in migrations:
            try:
                self._conn.execute(sql)
            except Exception:
                pass
        try:
            self._conn.executescript("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_client_event
                    ON sessions(client_event_id) WHERE client_event_id IS NOT NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS idx_activity_client_event
                    ON app_activity(client_event_id) WHERE client_event_id IS NOT NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS idx_todo_sessions_client_event
                    ON todo_sessions(client_event_id) WHERE client_event_id IS NOT NULL;
            """)
        except Exception:
            pass
        self._conn.commit()

    def open_session(self, start_time: str, date: str,
                     device_id: str = None, client_event_id: str = None) -> int:
        with self._lock:
            if client_event_id:
                row = self._conn.execute(
                    "SELECT id FROM sessions WHERE client_event_id = ?", (client_event_id,)
                ).fetchone()
                if row:
                    return row["id"]
            cur = self._conn.execute(
                "INSERT INTO sessions (start_time, date, device_id, client_event_id) "
                "VALUES (?, ?, ?, ?)",
                (start_time, date, device_id, client_event_id)
            )
            self._conn.commit()
            return cur.lastrowid

    def close_session(self, session_id: int, end_time: str):
        with self._lock:
            self._conn.execute("""
                UPDATE sessions
                SET end_time = ?,
                    total_seconds = CAST(
                        ROUND((julianday(?) - julianday(start_time)) * 86400) AS INTEGER
                    )
                WHERE id = ?
            """, (end_time, end_time, session_id))
            self._conn.commit()

    def open_app_activity(self, session_id: int, process_name: str,
                          window_title: str, start_time: str,
                          device_id: str = None, client_event_id: str = None) -> int:
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
                       (session_id, process_name, window_title, start_time,
                        device_id, client_event_id)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (session_id, process_name, window_title, start_time,
                 device_id, client_event_id)
            )
            self._conn.commit()
            return cur.lastrowid

    def close_app_activity(self, activity_id: int, end_time: str):
        with self._lock:
            self._conn.execute("""
                UPDATE app_activity
                SET end_time = ?,
                    duration_seconds = CAST(
                        ROUND((julianday(?) - julianday(start_time)) * 86400) AS INTEGER
                    )
                WHERE id = ?
            """, (end_time, end_time, activity_id))
            self._conn.commit()

    def close_stale_sessions(self):
        with self._lock:
            now = datetime.now(timezone.utc).isoformat()
            self._conn.execute("""
                UPDATE sessions SET end_time = start_time, total_seconds = 0
                WHERE end_time IS NULL
            """)
            self._conn.execute("""
                UPDATE app_activity SET end_time = start_time, duration_seconds = 0
                WHERE end_time IS NULL
            """)
            self._conn.commit()

    # An open session is treated as "live" only if (a) it is the most recently
    # opened session for the day (older open sessions are crash leftovers,
    # contribute 0) and (b) it hasn't been open for more than this many seconds
    # (safety cap if tracker is dead but stays open). Tracker normally closes
    # the session on idle/excluded transitions; long uninterrupted work is OK.
    _STALE_OPEN_SECONDS = 14400  # 4 hours

    def get_today_total_seconds(self, date: str) -> int:
        cur = self._conn.execute(f"""
            WITH live AS (
                SELECT MAX(id) AS id FROM sessions
                WHERE end_time IS NULL AND date = ?
            )
            SELECT COALESCE(SUM(
                CASE
                    WHEN s.end_time IS NOT NULL THEN s.total_seconds
                    WHEN s.id = (SELECT id FROM live)
                         AND (julianday('now') - julianday(s.start_time)) * 86400 < {self._STALE_OPEN_SECONDS}
                        THEN CAST(ROUND((julianday('now') - julianday(s.start_time)) * 86400) AS INTEGER)
                    ELSE 0
                END
            ), 0)
            FROM sessions s WHERE s.date = ?
        """, (date, date))
        return cur.fetchone()[0]

    def cleanup_stale_open_sessions(self) -> dict:
        """Close sessions that have been open longer than _STALE_OPEN_SECONDS.

        For each stale open session, sets end_time to MAX(start_time, last
        child app_activity end_time) so any genuinely measured time isn't lost.
        Also closes any orphan app_activity rows that lost their parent.

        Returns a small report.
        """
        with self._lock:
            stale = self._conn.execute(f"""
                SELECT id, start_time FROM sessions
                WHERE end_time IS NULL
                  AND (julianday('now') - julianday(start_time)) * 86400 >= {self._STALE_OPEN_SECONDS}
            """).fetchall()

            closed_sessions = 0
            closed_activities = 0
            for row in stale:
                sid = row["id"]
                start_time = row["start_time"]
                # Find latest activity end_time for this session as best-guess end.
                last_end = self._conn.execute(
                    "SELECT MAX(end_time) AS x FROM app_activity "
                    "WHERE session_id = ? AND end_time IS NOT NULL",
                    (sid,)
                ).fetchone()["x"]
                end_time = last_end if last_end else start_time
                # Close any still-open activities under this session first.
                orphan_acts = self._conn.execute(
                    "SELECT id FROM app_activity "
                    "WHERE session_id = ? AND end_time IS NULL", (sid,)
                ).fetchall()
                for a in orphan_acts:
                    self._conn.execute("""
                        UPDATE app_activity
                        SET end_time = ?,
                            duration_seconds = CAST(
                                ROUND((julianday(?) - julianday(start_time)) * 86400) AS INTEGER
                            )
                        WHERE id = ?
                    """, (end_time, end_time, a["id"]))
                    closed_activities += 1
                self._conn.execute("""
                    UPDATE sessions
                    SET end_time = ?,
                        total_seconds = CAST(
                            ROUND((julianday(?) - julianday(start_time)) * 86400) AS INTEGER
                        )
                    WHERE id = ?
                """, (end_time, end_time, sid))
                closed_sessions += 1
            self._conn.commit()
            return {
                "closed_sessions": closed_sessions,
                "closed_activities": closed_activities,
            }

    def get_sessions_for_date(self, date: str) -> list:
        cur = self._conn.execute(
            "SELECT id, start_time, end_time, total_seconds FROM sessions WHERE date = ? AND end_time IS NOT NULL ORDER BY start_time",
            (date,)
        )
        return [dict(row) for row in cur.fetchall()]

    def get_app_breakdown(self, date: str) -> list:
        cur = self._conn.execute("""
            SELECT a.process_name, SUM(a.duration_seconds) as total_seconds
            FROM app_activity a
            JOIN sessions s ON a.session_id = s.id
            WHERE s.date = ? AND a.duration_seconds IS NOT NULL
            GROUP BY a.process_name
            ORDER BY total_seconds DESC
        """, (date,))
        return [dict(row) for row in cur.fetchall()]

    def get_weekly_summary(self, start_date: str, end_date: str) -> list:
        cur = self._conn.execute("""
            SELECT date, COALESCE(SUM(total_seconds), 0) as total_seconds
            FROM sessions
            WHERE date BETWEEN ? AND ? AND end_time IS NOT NULL
            GROUP BY date
            ORDER BY date
        """, (start_date, end_date))
        return [dict(row) for row in cur.fetchall()]

    # ── Todo CRUD ────────────────────────────────────────────────

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
                "SELECT * FROM todos ORDER BY CASE status WHEN 'in_progress' THEN 0 WHEN 'todo' THEN 1 WHEN 'paused' THEN 2 ELSE 3 END, created_at DESC"
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

    def start_todo_timer(self, todo_id: int) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            # Auto-pause any currently running todo
            active = self._conn.execute(
                "SELECT ts.id, ts.todo_id FROM todo_sessions ts WHERE ts.end_time IS NULL LIMIT 1"
            ).fetchone()
            if active:
                self._close_todo_session_locked(active["id"], active["todo_id"], now)

            self._conn.execute(
                "UPDATE todos SET status = 'in_progress' WHERE id = ?", (todo_id,)
            )
            cur = self._conn.execute(
                "INSERT INTO todo_sessions (todo_id, start_time) VALUES (?, ?)",
                (todo_id, now)
            )
            self._conn.commit()
            return cur.lastrowid

    def stop_todo_timer(self, todo_id: int, reason: str = 'manual'):
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            session = self._conn.execute(
                "SELECT id FROM todo_sessions WHERE todo_id = ? AND end_time IS NULL",
                (todo_id,)
            ).fetchone()
            if session:
                self._close_todo_session_locked(session["id"], todo_id, now, reason)
            self._conn.execute(
                "UPDATE todos SET status = 'paused' WHERE id = ? AND status = 'in_progress'",
                (todo_id,)
            )
            self._conn.commit()

    def complete_todo(self, todo_id: int):
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            session = self._conn.execute(
                "SELECT id FROM todo_sessions WHERE todo_id = ? AND end_time IS NULL",
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
                duration_seconds = CAST(
                    ROUND((julianday(?) - julianday(start_time)) * 86400) AS INTEGER
                ),
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

    def get_daily_summary(self, days: int = 14) -> list:
        cur = self._conn.execute(f"""
            WITH live_per_day AS (
                SELECT date, MAX(id) AS id FROM sessions
                WHERE end_time IS NULL GROUP BY date
            )
            SELECT s.date, COALESCE(SUM(
                CASE
                    WHEN s.end_time IS NOT NULL THEN s.total_seconds
                    WHEN s.id = (SELECT id FROM live_per_day WHERE date = s.date)
                         AND (julianday('now') - julianday(s.start_time)) * 86400 < {self._STALE_OPEN_SECONDS}
                        THEN CAST(ROUND((julianday('now') - julianday(s.start_time)) * 86400) AS INTEGER)
                    ELSE 0
                END
            ), 0) AS total_seconds
            FROM sessions s
            WHERE s.date >= date('now', ?)
            GROUP BY s.date ORDER BY s.date
        """, (f'-{days - 1} days',))
        return [dict(row) for row in cur.fetchall()]

    def get_weekly_totals(self, weeks: int = 8) -> list:
        cur = self._conn.execute("""
            SELECT strftime('%Y-%W', date) as week,
                   COALESCE(SUM(total_seconds), 0) as total_seconds
            FROM sessions
            WHERE date >= date('now', ?) AND end_time IS NOT NULL
            GROUP BY week ORDER BY week
        """, (f'-{weeks * 7} days',))
        return [dict(row) for row in cur.fetchall()]

    def get_monthly_totals(self, months: int = 6) -> list:
        cur = self._conn.execute("""
            SELECT strftime('%Y-%m', date) as month,
                   COALESCE(SUM(total_seconds), 0) as total_seconds
            FROM sessions
            WHERE date >= date('now', ?) AND end_time IS NOT NULL
            GROUP BY month ORDER BY month
        """, (f'-{months} months',))
        return [dict(row) for row in cur.fetchall()]

    def get_today_todo_total_seconds(self, date: str) -> int:
        """Sum of all time spent on todos today (UTC date).

        Includes any currently in-progress todo's elapsed time since
        its session started, so the dashboard counter ticks live.
        """
        closed = self._conn.execute("""
            SELECT COALESCE(SUM(duration_seconds), 0)
            FROM todo_sessions
            WHERE end_time IS NOT NULL
              AND substr(start_time, 1, 10) = ?
        """, (date,)).fetchone()[0]
        row = self._conn.execute("""
            SELECT start_time FROM todo_sessions
            WHERE end_time IS NULL
            ORDER BY id DESC LIMIT 1
        """).fetchone()
        active = 0
        if row:
            try:
                start = datetime.fromisoformat(row["start_time"])
                if start.tzinfo is None:
                    start = start.replace(tzinfo=timezone.utc)
                active = max(0, int(
                    (datetime.now(timezone.utc) - start).total_seconds()
                ))
            except Exception:
                pass
        return closed + active

    def get_completed_today_count(self, date: str) -> int:
        cur = self._conn.execute(
            "SELECT COUNT(*) FROM todos WHERE status = 'done' AND DATE(completed_at) = ?",
            (date,)
        )
        return cur.fetchone()[0]

    def close(self):
        self._conn.close()
