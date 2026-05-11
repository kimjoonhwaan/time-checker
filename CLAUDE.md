# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run the application
python main.py
```

## Architecture

Windows-only time tracker that measures productive computer usage. Runs as a system tray app with a Flask web dashboard.

**Threading model** — `main.py` owns the main thread for `pystray` (Windows requirement). Two daemon threads run alongside: `TrackerLoop` (polling loop) and Flask server.

```
main thread  →  pystray TrayApp.run()  (blocks)
daemon thread →  TrackerLoop.run()      (30s poll)
daemon thread →  Flask run_flask()      (HTTP server)
```

**Core data flow:**
1. `tracker.py`: `IdleDetector` polls `GetLastInputInfo` every 30s. If idle < threshold and foreground window is not excluded → `TrackerLoop` opens/continues a session in SQLite via `DatabaseManager`.
2. `database.py`: All writes acquire `threading.Lock`. SQLite WAL mode allows concurrent reads from Flask.
3. `app.py`: Flask reads DB to serve REST endpoints. Dashboard auto-refreshes every 30s.
4. `tray.py`: Updates icon color (green=tracking, yellow=idle, red=paused) and tooltip every 10s from `tracker.get_status()`.

**App exclusion logic** (`tracker.py` `WindowDetector._is_excluded`): checks foreground window's process name against `excluded_processes` list AND window title against `excluded_title_keywords` list — both from `config.json`. Case-insensitive. Excluded window = treated same as idle (session ends).

**Pause/resume**: `main.py` creates a `pause_event` (`threading.Event`) shared between `TrackerLoop` and `TrayApp`. When set, `_tick()` ends any active session and stays in `PAUSED` state. The tray menu label toggles between "일시정지" / "재개" based on this event.

**Port selection**: `find_free_port()` in `app.py` tries `flask_port` (default 5000) and increments up to 20 times. The chosen port is written back into `config["_actual_port"]` so `TrayApp._open_dashboard()` opens the correct URL.

**Crash recovery**: `close_stale_sessions()` is called on startup and shutdown to close any sessions with `end_time IS NULL`.

**Schema migrations**: `DatabaseManager._migrate()` runs `ALTER TABLE` statements that silently fail if the column already exists — this is the pattern for adding new columns.

## SQLite schema (`timetracker.db`, auto-created)

- `sessions`: `start_time`, `end_time`, `total_seconds`, `date` (UTC ISO-8601)
- `app_activity`: per-window activity linked to a session via `session_id`
- `todos`: `title`, `status` (todo/in_progress/paused/done), `priority` (low/medium/high), `total_seconds`, `estimated_seconds`, `notes`, `created_at`, `completed_at`
- `todo_sessions`: time-tracking segments per todo, `pause_reason` (manual/idle/completed/excluded:\<app\>)

`todos.total_seconds` is kept in sync by `_close_todo_session_locked()` — it re-sums all closed `todo_sessions` rows each time a session closes.

## Todo feature

Todos are tracked independently from the general session tracker. Key behaviors:
- Only one todo can be `in_progress` at a time. Starting a new one auto-pauses the current one.
- When the system goes idle or an excluded app becomes foreground, `TrackerLoop._auto_pause_active_todo()` pauses the active todo and stores its ID in `_auto_paused_todo_id`. On resume, `_auto_resume_todo_if_needed()` restarts it.
- `pause_reason` on `todo_sessions` records why the timer stopped; `_reason_label()` in `app.py` converts it to Korean for the history API.

## API endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | Dashboard HTML |
| `/api/summary/today` | GET | Today's total seconds + session list |
| `/api/summary/week` | GET | Last 7 days, zero-filled |
| `/api/apps/today` | GET | Per-process breakdown with `is_excluded` flag |
| `/api/sessions/<date>` | GET | Sessions for a specific date |
| `/api/tracker/status` | GET | Live state, idle_seconds, today total |
| `/api/config` | GET | Current `config.json` contents |
| `/api/stats/daily` | GET | 14-day daily totals |
| `/api/stats/weekly` | GET | 8-week weekly totals |
| `/api/stats/monthly` | GET | 6-month monthly totals |
| `/api/todos` | GET | All todos (optional `?status=` filter) |
| `/api/todos` | POST | Create todo (`title`, `priority`, `estimated_seconds`, `notes`) |
| `/api/todos/<id>` | GET/PUT/DELETE | Single todo CRUD |
| `/api/todos/<id>/start` | POST | Start timer |
| `/api/todos/<id>/stop` | POST | Stop timer (reason=manual) |
| `/api/todos/<id>/complete` | POST | Mark done, stop timer |
| `/api/todos/<id>/history` | GET | Chronological start/stop events with Korean labels |

## Key files

| File | Role |
|---|---|
| `main.py` | Entry point — wires threads, starts tray |
| `tracker.py` | `IdleDetector`, `WindowDetector`, `TrackerLoop` state machine |
| `database.py` | `DatabaseManager` — all SQLite operations |
| `app.py` | Flask API + `find_free_port` (auto-increments if 5000 is taken) |
| `tray.py` | `pystray` icon, menu, 10s update loop |
| `config.json` | Runtime configuration |
| `templates/dashboard.html` | Chart.js dashboard — fetches all data from `/api/*` endpoints |

## Config

Edit `config.json` to customize behavior:
- `idle_threshold_seconds` (default 60) — inactivity before session ends
- `poll_interval_seconds` (default 30) — tracker sampling interval
- `flask_port` (default 5000) — preferred dashboard port
- `excluded_processes` — exact process names (e.g. `"vlc.exe"`)
- `excluded_title_keywords` — substrings matched against window title (e.g. `"YouTube"`)
