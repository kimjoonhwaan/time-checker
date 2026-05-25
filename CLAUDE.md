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
daemon thread →  TrackerLoop.run()      (10s poll)
daemon thread →  Flask run_flask()      (HTTP server)
```

**Core data flow — "tick accumulator" (the tracker is the single time authority):**
1. `tracker.py`: every poll (default 10s) `TrackerLoop._tick()` measures `delta = clamp(now - last_tick, 0, 2*poll)`, reads `idle` (`GetLastInputInfo`) and the foreground window, then computes `active = max(0, delta - idle)`. It pushes that **duration** to the backend tagged with the currently-selected todo (the one with `status='in_progress'`, which it only reads), the foreground process, an `excluded` flag, and a unique `event_id`.
2. `database.py`: `apply_tick()` accrues the duration into per-day buckets `todo_time`/`app_time`. `event_id` is recorded in `applied_ticks`, so a retried/replayed POST is applied **exactly once** (no loss, no double count). Durations are clock-agnostic → client/server clock skew is irrelevant. There is **no timestamp subtraction or live-cutoff** on the read side.
3. `app.py`: read endpoints return stored sums directly. The single write path is `POST /api/ingest/tick`.
4. `tray.py`: icon color from `tracker.get_status()` state string (`active`/`idle`/`excluded`/`paused`/`offline`).

Idle/excluded "auto-pause" is **emergent**: when idle or on an excluded app, `active ≈ 0`, so nothing accrues; returning to work resumes accrual on the next tick automatically. No state machine, no session intervals, no backdating.

**App exclusion logic** (`tracker.py` `WindowDetector._is_excluded`): foreground process name vs `excluded_processes` AND window title vs `excluded_title_keywords` (both from `config.json`, case-insensitive). When excluded, the tick still records `app_time` (flagged `is_excluded`) but credits **no** todo.

**Selection model**: `todos.status == 'in_progress'` is the single source of truth for which todo is active. The dashboard's start/stop/complete buttons set it (`set_active_todo`/`pause_todo`/`complete_todo`); the tracker only reads it via `get_active_todo_id` (`GET /api/active-todo` in REMOTE mode). A completed todo can never be restarted (`set_active_todo` refuses `status='done'`).

**Pause/resume**: `pause_event` shared between `TrackerLoop` and `TrayApp`. When set, ticks credit 0 and report `state='paused'`.

**Day boundary (KST)**: each tick accrues into the KST date bucket, so midnight splits naturally. `complete_day_crossed_todos()` marks any in_progress/paused todo whose most recent worked day is before today (KST) as `done`; triggered on each tick (LOCAL), `/api/todos` GET, and startup `cleanup()`.

**Offline resilience (REMOTE mode)**: `ingest_client.py` queues failed POSTs to a local SQLite file and replays them; idempotency makes replay safe.

**Port selection**: `find_free_port()` in `app.py` tries `flask_port` (default 5000) and increments up to 20 times, writing the choice into `config["_actual_port"]`.

## SQLite schema (`timetracker.db`, auto-created)

- `todos`: `title`, `status` (todo/in_progress/paused/done), `priority`, `total_seconds` (cache = `SUM(todo_time)`), `estimated_seconds`, `notes`, `created_at`, `completed_at`
- `todo_time`: `(todo_id, date)` → `seconds` — per-day accrued work time (KST date)
- `app_time`: `(process_name, date)` → `seconds`, `is_excluded` — per-day app usage (KST date)
- `applied_ticks`: `event_id` dedup keys (pruned after 2 days by `cleanup()`)
- `tracker_state`: last `(device_id, state, idle_seconds, time)` for liveness/tray
- Legacy `todo_sessions`/`app_activity`/`device_heartbeats` are retained read-only and folded into the new buckets once via `_backfill_once()` (guarded by `meta.backfilled_v2`).

## API endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | Dashboard HTML |
| `/api/ingest/tick` | POST | **The only write path** — accrue one measured duration (idempotent on `event_id`). Requires `X-API-Key`. |
| `/api/active-todo` | GET | The currently in_progress todo id (tracker polls this) |
| `/api/summary/today` | GET | Today's accrued todo seconds (KST) |
| `/api/apps/today` | GET | Per-process breakdown with `is_excluded` flag |
| `/api/tracker/status` | GET | Live state, idle_seconds, today total (from last tick) |
| `/api/config` | GET | Current `config.json` contents |
| `/api/stats/daily` | GET | 14-day daily totals (todo_time) |
| `/api/stats/weekly` | GET | 8-week weekly totals |
| `/api/stats/monthly` | GET | 6-month monthly totals |
| `/api/todos` | GET | All todos (optional `?status=` filter) |
| `/api/todos` | POST | Create todo (`title`, `priority`, `estimated_seconds`, `notes`) |
| `/api/todos/<id>` | GET/PUT/DELETE | Single todo CRUD |
| `/api/todos/<id>/start` | POST | Select as active (in_progress); 400 if already done |
| `/api/todos/<id>/stop` | POST | Pause (status=paused) |
| `/api/todos/<id>/complete` | POST | Mark done |
| `/api/admin/cleanup` | POST | Complete day-crossed todos, prune dedup keys |

## Key files

| File | Role |
|---|---|
| `main.py` | Entry point — wires threads, starts tray |
| `tracker.py` | `IdleDetector`, `WindowDetector`, `TrackerLoop` tick accumulator |
| `database.py` | `DatabaseManager` — all SQLite operations |
| `app.py` | Flask API + `find_free_port` (auto-increments if 5000 is taken) |
| `tray.py` | `pystray` icon, menu, 10s update loop |
| `config.json` | Runtime configuration |
| `templates/dashboard.html` | Chart.js dashboard — fetches all data from `/api/*` endpoints |

## Config

Edit `config.json` to customize behavior:
- `idle_threshold_seconds` (default 60) — idle threshold reported in tick state
- `poll_interval_seconds` (default 10) — tracker sampling/accrual interval
- `flask_port` (default 5000) — preferred dashboard port
- `excluded_processes` — exact process names (e.g. `"vlc.exe"`)
- `excluded_title_keywords` — substrings matched against window title (e.g. `"YouTube"`)
