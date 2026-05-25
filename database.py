"""SQLite-backed store for TimeChecker — tick-accumulator model.

Time is never derived by subtracting a stored timestamp from "now". Instead the
tracker (the single time authority) measures *active duration* every poll and
sends it as an increment; the server accrues it into per-day buckets:

  todo_time(todo_id, date, seconds)        — "오늘/작업 시간" (the user-visible total)
  app_time(process_name, date, seconds)    — "앱별 사용 시간"

Increments carry an `event_id` and are recorded in `applied_ticks` so a retried
or replayed POST is applied exactly once (no loss, no double count). Durations
are clock-agnostic, so client/server clock skew is irrelevant.

`todos.status == 'in_progress'` is the single source of truth for *which* todo
is selected; the dashboard sets it, the tracker only reads it.

Legacy tables (`todo_sessions`, `app_activity`, `device_heartbeats`) are kept
for one-time backfill and are no longer written.
"""
import sqlite3
import threading
from datetime import datetime, timedelta, timezone, time


class DatabaseManager:
    _KST = timezone(timedelta(hours=9))

    def __init__(self, db_path: str, idle_threshold_seconds: int = 60):
        self._db_path = db_path
        self._idle_threshold = idle_threshold_seconds
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()
        self._migrate()
        self._backfill_once()

    # ── KST helpers ─────────────────────────────────────────────

    @classmethod
    def kst_today(cls) -> str:
        return datetime.now(timezone.utc).astimezone(cls._KST).strftime("%Y-%m-%d")

    @classmethod
    def _kst_midnight_after(cls, kst_date_str: str) -> str:
        """UTC ISO of the KST midnight that follows the given KST date."""
        d = datetime.strptime(kst_date_str, "%Y-%m-%d").date()
        boundary = datetime.combine(d + timedelta(days=1), time(0, 0), tzinfo=cls._KST)
        return boundary.astimezone(timezone.utc).isoformat()

    # ── Schema ──────────────────────────────────────────────────

    def _create_tables(self):
        self._conn.executescript("""
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
            CREATE TABLE IF NOT EXISTS todo_time (
                todo_id INTEGER NOT NULL,
                date    TEXT NOT NULL,
                seconds INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (todo_id, date)
            );
            CREATE TABLE IF NOT EXISTS app_time (
                process_name TEXT NOT NULL,
                date         TEXT NOT NULL,
                seconds      INTEGER NOT NULL DEFAULT 0,
                is_excluded  INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (process_name, date)
            );
            CREATE TABLE IF NOT EXISTS applied_ticks (
                event_id   TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tracker_state (
                device_id    TEXT PRIMARY KEY,
                state        TEXT,
                idle_seconds REAL DEFAULT 0,
                time         TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            -- Legacy (read-only for backfill).
            CREATE TABLE IF NOT EXISTS todo_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, todo_id INTEGER,
                start_time TEXT, end_time TEXT, duration_seconds INTEGER,
                pause_reason TEXT, device_id TEXT
            );
            CREATE TABLE IF NOT EXISTS app_activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT, process_name TEXT,
                window_title TEXT, start_time TEXT, end_time TEXT,
                duration_seconds INTEGER, todo_id INTEGER, device_id TEXT,
                client_event_id TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_todos_status   ON todos(status);
            CREATE INDEX IF NOT EXISTS idx_todo_time_date ON todo_time(date);
            CREATE INDEX IF NOT EXISTS idx_app_time_date  ON app_time(date);
        """)
        self._conn.commit()

    def _migrate(self):
        # Older app_time DBs may lack is_excluded.
        try:
            self._conn.execute("ALTER TABLE app_time ADD COLUMN is_excluded INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        self._conn.commit()

    def _backfill_once(self):
        """Fold legacy interval data into the new accumulator buckets, once."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key='backfilled_v2'"
            ).fetchone()
            if row:
                return
            try:
                self._conn.execute("""
                    INSERT INTO todo_time(todo_id, date, seconds)
                    SELECT todo_id, date(start_time, '+9 hours'),
                           CAST(SUM(duration_seconds) AS INTEGER)
                    FROM todo_sessions
                    WHERE end_time IS NOT NULL AND duration_seconds IS NOT NULL
                    GROUP BY todo_id, date(start_time, '+9 hours')
                    ON CONFLICT(todo_id, date)
                        DO UPDATE SET seconds = todo_time.seconds + excluded.seconds
                """)
                self._conn.execute("""
                    INSERT INTO app_time(process_name, date, seconds, is_excluded)
                    SELECT process_name, date(start_time, '+9 hours'),
                           CAST(SUM(duration_seconds) AS INTEGER), 0
                    FROM app_activity
                    WHERE duration_seconds IS NOT NULL
                    GROUP BY process_name, date(start_time, '+9 hours')
                    ON CONFLICT(process_name, date)
                        DO UPDATE SET seconds = app_time.seconds + excluded.seconds
                """)
                self._conn.execute("""
                    UPDATE todos SET total_seconds = COALESCE(
                        (SELECT SUM(seconds) FROM todo_time WHERE todo_id = todos.id), 0)
                """)
            except Exception:
                pass
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('backfilled_v2', ?)",
                (datetime.now(timezone.utc).isoformat(),)
            )
            self._conn.commit()

    # ── Tick accumulation (the only time writer) ────────────────

    def apply_tick(self, event_id: str, kst_date: str, active_seconds,
                   process_name: str = None, excluded: bool = False,
                   todo_id: int = None) -> bool:
        """Accrue one measured interval. Idempotent on event_id."""
        s = max(0, int(active_seconds or 0))
        with self._lock:
            if s > 0 and event_id:
                seen = self._conn.execute(
                    "SELECT 1 FROM applied_ticks WHERE event_id = ?", (event_id,)
                ).fetchone()
                if seen:
                    return False
            if s > 0:
                if process_name:
                    self._conn.execute("""
                        INSERT INTO app_time(process_name, date, seconds, is_excluded)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(process_name, date) DO UPDATE SET
                            seconds = app_time.seconds + excluded.seconds,
                            is_excluded = excluded.is_excluded
                    """, (process_name, kst_date, s, 1 if excluded else 0))
                if todo_id:
                    self._conn.execute("""
                        INSERT INTO todo_time(todo_id, date, seconds)
                        VALUES (?, ?, ?)
                        ON CONFLICT(todo_id, date) DO UPDATE SET
                            seconds = todo_time.seconds + excluded.seconds
                    """, (todo_id, kst_date, s))
                    self._conn.execute("""
                        UPDATE todos SET total_seconds = COALESCE(
                            (SELECT SUM(seconds) FROM todo_time WHERE todo_id = ?), 0)
                        WHERE id = ?
                    """, (todo_id, todo_id))
                if event_id:
                    self._conn.execute(
                        "INSERT OR IGNORE INTO applied_ticks(event_id, applied_at) "
                        "VALUES (?, ?)",
                        (event_id, datetime.now(timezone.utc).isoformat())
                    )
            self._conn.commit()
            return True

    def push_tick(self, event_id: str, kst_date: str, active_seconds,
                  process_name: str = None, excluded: bool = False,
                  todo_id: int = None, state: str = None,
                  idle_seconds: float = 0, device_id: str = None) -> bool:
        """Backend-uniform entry used by the tracker (LOCAL mode). Mirrors the
        server's /api/ingest/tick: accrue time and record liveness state."""
        applied = self.apply_tick(event_id, kst_date, active_seconds,
                                  process_name, excluded, todo_id)
        if device_id:
            self.record_tracker_state(
                device_id, state, idle_seconds,
                datetime.now(timezone.utc).isoformat())
        return applied

    def record_tracker_state(self, device_id: str, state: str,
                             idle_seconds: float, time_iso: str):
        if not device_id:
            return
        with self._lock:
            self._conn.execute("""
                INSERT INTO tracker_state(device_id, state, idle_seconds, time)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                    state = excluded.state,
                    idle_seconds = excluded.idle_seconds,
                    time = excluded.time
            """, (device_id, state, idle_seconds, time_iso))
            self._conn.commit()

    def get_latest_tracker_state(self) -> dict:
        row = self._conn.execute(
            "SELECT device_id, state, idle_seconds, time FROM tracker_state "
            "ORDER BY time DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    # ── Selection (which todo is active) ────────────────────────

    def get_active_todo_id(self) -> int:
        row = self._conn.execute(
            "SELECT id FROM todos WHERE status='in_progress' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row["id"] if row else None

    def set_active_todo(self, todo_id: int) -> bool:
        """Make todo_id the single in_progress todo. Refuses completed/missing
        todos so a finished task can never be restarted. Returns success."""
        with self._lock:
            row = self._conn.execute(
                "SELECT status FROM todos WHERE id = ?", (todo_id,)
            ).fetchone()
            if row is None or row["status"] == "done":
                return False
            self._conn.execute(
                "UPDATE todos SET status='paused' WHERE status='in_progress'"
            )
            self._conn.execute(
                "UPDATE todos SET status='in_progress' WHERE id = ?", (todo_id,)
            )
            self._conn.commit()
            return True

    def pause_todo(self, todo_id: int):
        with self._lock:
            self._conn.execute(
                "UPDATE todos SET status='paused' "
                "WHERE id=? AND status='in_progress'",
                (todo_id,)
            )
            self._conn.commit()

    def complete_todo(self, todo_id: int):
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                "UPDATE todos SET status='done', completed_at=? WHERE id=?",
                (now, todo_id)
            )
            self._conn.commit()

    # ── Todo CRUD ───────────────────────────────────────────────

    def create_todo(self, title: str, priority: str = "medium",
                    estimated_seconds: int = None, notes: str = None) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO todos (title, priority, estimated_seconds, notes, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
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
            self._conn.execute(f"UPDATE todos SET {set_clause} WHERE id = ?", values)
            self._conn.commit()

    def delete_todo(self, todo_id: int):
        with self._lock:
            self._conn.execute("DELETE FROM todos WHERE id = ?", (todo_id,))
            self._conn.execute("DELETE FROM todo_time WHERE todo_id = ?", (todo_id,))
            self._conn.commit()

    def get_todo_today_seconds(self, todo_id: int, date_kst: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(seconds),0) FROM todo_time "
            "WHERE todo_id=? AND date=?", (todo_id, date_kst)
        ).fetchone()
        return int(row[0])

    # ── Read aggregations (stored totals, no cutoff math) ───────

    def get_today_todo_total_seconds(self, date_kst: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(seconds),0) FROM todo_time WHERE date=?",
            (date_kst,)
        ).fetchone()
        return int(row[0])

    def get_completed_today_count(self, date_kst: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM todos "
            "WHERE status='done' AND date(completed_at, '+9 hours') = ?",
            (date_kst,)
        ).fetchone()
        return int(row[0])

    def get_app_breakdown(self, date_kst: str) -> list:
        cur = self._conn.execute(
            "SELECT process_name, seconds AS total_seconds, is_excluded "
            "FROM app_time WHERE date=? AND seconds > 0 "
            "ORDER BY seconds DESC", (date_kst,)
        )
        return [dict(row) for row in cur.fetchall()]

    def get_daily_todo_summary(self, days: int = 14) -> list:
        cutoff = (datetime.now(timezone.utc).astimezone(self._KST)
                  - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        cur = self._conn.execute("""
            SELECT date, COALESCE(SUM(seconds),0) AS total_seconds
            FROM todo_time WHERE date >= ?
            GROUP BY date ORDER BY date
        """, (cutoff,))
        return [dict(row) for row in cur.fetchall()]

    def get_weekly_todo_totals(self, weeks: int = 8) -> list:
        cutoff = (datetime.now(timezone.utc).astimezone(self._KST)
                  - timedelta(weeks=weeks)).strftime("%Y-%m-%d")
        cur = self._conn.execute("""
            SELECT strftime('%Y-%W', date) AS week,
                   COALESCE(SUM(seconds),0) AS total_seconds
            FROM todo_time WHERE date >= ?
            GROUP BY week ORDER BY week
        """, (cutoff,))
        return [dict(row) for row in cur.fetchall()]

    def get_monthly_todo_totals(self, months: int = 6) -> list:
        cutoff = (datetime.now(timezone.utc).astimezone(self._KST)
                  - timedelta(days=months * 31)).strftime("%Y-%m-%d")
        cur = self._conn.execute("""
            SELECT strftime('%Y-%m', date) AS month,
                   COALESCE(SUM(seconds),0) AS total_seconds
            FROM todo_time WHERE date >= ?
            GROUP BY month ORDER BY month
        """, (cutoff,))
        return [dict(row) for row in cur.fetchall()]

    # ── Day-boundary auto-completion ────────────────────────────

    def complete_day_crossed_todos(self) -> int:
        """Complete any in_progress/paused todo whose most recent worked day
        (KST) is before today and has no time today — it crossed midnight."""
        with self._lock:
            today_kst = self.kst_today()
            rows = self._conn.execute("""
                SELECT t.id AS todo_id, MAX(tt.date) AS last_date
                FROM todos t
                JOIN todo_time tt ON tt.todo_id = t.id
                WHERE t.status IN ('in_progress', 'paused')
                GROUP BY t.id
            """).fetchall()
            completed = 0
            for r in rows:
                if not r["last_date"] or r["last_date"] >= today_kst:
                    continue
                self._conn.execute(
                    "UPDATE todos SET status='done', completed_at=? "
                    "WHERE id=? AND status!='done'",
                    (self._kst_midnight_after(r["last_date"]), r["todo_id"])
                )
                completed += 1
            self._conn.commit()
            return completed

    # ── Maintenance ─────────────────────────────────────────────

    def cleanup(self) -> dict:
        """Complete day-crossed todos and prune old dedup keys."""
        day_completed = self.complete_day_crossed_todos()
        with self._lock:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
            self._conn.execute(
                "DELETE FROM applied_ticks WHERE applied_at < ?", (cutoff,)
            )
            self._conn.commit()
        return {"day_completed_todos": day_completed}

    def close(self):
        self._conn.close()
