"""Flask API + dashboard for TimeChecker (tick-accumulator model).

Day buckets are KST; the server only stores and reads accumulated totals — no
timestamp subtraction, no live-cutoff. The tracker is the sole time writer via
POST /api/ingest/tick (idempotent on event_id).
"""
import json
import os
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template, request

import paths

flask_app = Flask(__name__)
_db = None
_tracker = None
_config_path: Path = paths.config_path()
_api_key = None
_KST = timezone(timedelta(hours=9))


def init_app(db, tracker=None, config_path: Path = None, api_key: str = None):
    global _db, _tracker, _config_path, _api_key
    _db = db
    if tracker is not None:
        _tracker = tracker
    if config_path:
        _config_path = config_path
    if api_key is not None:
        _api_key = api_key
    else:
        _api_key = os.environ.get("TIMECHECKER_API_KEY")


def _today_kst() -> str:
    return datetime.now(timezone.utc).astimezone(_KST).strftime("%Y-%m-%d")


def format_duration(seconds: int) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


@flask_app.route("/")
def dashboard():
    return render_template("dashboard.html")


# ── Read endpoints (stored totals) ─────────────────────────────

@flask_app.route("/api/summary/today")
def summary_today():
    date = _today_kst()
    total = _db.get_today_todo_total_seconds(date)
    return jsonify({
        "date": date,
        "todo_total_seconds": total,
        "todo_total_formatted": format_duration(total),
    })


@flask_app.route("/api/apps/today")
def apps_today():
    date = _today_kst()
    rows = _db.get_app_breakdown(date)
    total = sum(r["total_seconds"] for r in rows)
    result = []
    for r in rows:
        pct = round(r["total_seconds"] / total * 100, 1) if total else 0
        result.append({
            "process_name": r["process_name"],
            "total_seconds": r["total_seconds"],
            "formatted": format_duration(r["total_seconds"]),
            "percentage": pct,
            "is_excluded": bool(r["is_excluded"]),
        })
    return jsonify({"apps": result})


@flask_app.route("/api/tracker/status")
def tracker_status():
    date = _today_kst()
    if _tracker is not None:
        st = _tracker.get_status()
        st["today_total_seconds"] = _db.get_today_todo_total_seconds(date)
        return jsonify(st)
    # Remote mode: derive from the last tick's recorded state.
    total = _db.get_today_todo_total_seconds(date) if _db else 0
    last = _db.get_latest_tracker_state() if _db else None
    state, idle, last_time = "offline", 0, None
    if last and last.get("time"):
        last_time = last["time"]
        try:
            dt = datetime.fromisoformat(last_time)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            fresh = (datetime.now(timezone.utc) - dt).total_seconds() <= 120
        except Exception:
            fresh = False
        if fresh:
            state = last.get("state") or "unknown"
            idle = last.get("idle_seconds") or 0
    return jsonify({
        "state": state,
        "today_total_seconds": total,
        "idle_seconds": idle,
        "last_tick": last_time,
    })


# ── Ingest (the single write path) ─────────────────────────────

def _require_api_key():
    if not _api_key:
        return None  # auth disabled (dev mode)
    if request.headers.get("X-API-Key") != _api_key:
        return jsonify({"error": "unauthorized"}), 401
    return None


@flask_app.route("/api/ingest/tick", methods=["POST"])
def ingest_tick():
    err = _require_api_key()
    if err:
        return err
    d = request.get_json(force=True) or {}
    _db.push_tick(
        event_id=d.get("event_id"),
        kst_date=d.get("kst_date") or _today_kst(),
        active_seconds=d.get("active_seconds", 0),
        process_name=d.get("process_name"),
        excluded=bool(d.get("excluded")),
        todo_id=d.get("todo_id"),
        state=d.get("state"),
        idle_seconds=d.get("idle_seconds", 0) or 0,
        device_id=d.get("device_id"),
    )
    return jsonify({"ok": True})


@flask_app.route("/api/active-todo", methods=["GET"])
def active_todo():
    err = _require_api_key()
    if err:
        return err
    return jsonify({"todo_id": _db.get_active_todo_id()})


@flask_app.route("/api/admin/cleanup", methods=["POST"])
def admin_cleanup():
    err = _require_api_key()
    if err:
        return err
    return jsonify(_db.cleanup())


@flask_app.route("/api/config", methods=["GET"])
def get_config():
    with open(_config_path) as f:
        return jsonify(json.load(f))


# ── Todo API ───────────────────────────────────────────────────

def _serialize_todo(t: dict) -> dict:
    total = t["total_seconds"] or 0
    est = t.get("estimated_seconds")
    pct = round(total / est * 100) if est and est > 0 else None
    return {
        "id": t["id"],
        "title": t["title"],
        "status": t["status"],
        "priority": t["priority"],
        "total_seconds": total,
        "total_formatted": format_duration(total),
        "today_seconds": _db.get_todo_today_seconds(t["id"], _today_kst()),
        "estimated_seconds": est,
        "progress_pct": pct,
        "notes": t.get("notes") or "",
        "created_at": t["created_at"],
        "completed_at": t.get("completed_at"),
    }


@flask_app.route("/api/todos", methods=["GET"])
def list_todos():
    # Self-heal: complete prior-day tasks on dashboard load too.
    try:
        _db.complete_day_crossed_todos()
    except Exception:
        pass
    status = request.args.get("status")
    todos = _db.get_todos(status_filter=status)
    return jsonify({
        "todos": [_serialize_todo(t) for t in todos],
        "completed_today": _db.get_completed_today_count(_today_kst()),
    })


@flask_app.route("/api/todos", methods=["POST"])
def create_todo():
    data = request.get_json(force=True)
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "title required"}), 400
    est_raw = data.get("estimated_seconds")
    est = int(est_raw) if est_raw else None
    todo_id = _db.create_todo(
        title=title,
        priority=data.get("priority", "medium"),
        estimated_seconds=est,
        notes=data.get("notes"),
    )
    return jsonify(_serialize_todo(_db.get_todo(todo_id))), 201


@flask_app.route("/api/todos/<int:todo_id>", methods=["GET"])
def get_todo(todo_id):
    todo = _db.get_todo(todo_id)
    if not todo:
        return jsonify({"error": "not found"}), 404
    return jsonify(_serialize_todo(todo))


@flask_app.route("/api/todos/<int:todo_id>", methods=["PUT"])
def update_todo(todo_id):
    data = request.get_json(force=True)
    _db.update_todo(todo_id, **data)
    todo = _db.get_todo(todo_id)
    if not todo:
        return jsonify({"error": "not found"}), 404
    return jsonify(_serialize_todo(todo))


@flask_app.route("/api/todos/<int:todo_id>", methods=["DELETE"])
def delete_todo(todo_id):
    _db.delete_todo(todo_id)
    return jsonify({"ok": True})


@flask_app.route("/api/todos/<int:todo_id>/start", methods=["POST"])
def start_todo(todo_id):
    todo = _db.get_todo(todo_id)
    if not todo:
        return jsonify({"error": "not found"}), 404
    if not _db.set_active_todo(todo_id):
        return jsonify({"error": "completed task cannot be restarted"}), 400
    return jsonify(_serialize_todo(_db.get_todo(todo_id)))


@flask_app.route("/api/todos/<int:todo_id>/stop", methods=["POST"])
def stop_todo(todo_id):
    _db.pause_todo(todo_id)
    todo = _db.get_todo(todo_id)
    if not todo:
        return jsonify({"error": "not found"}), 404
    return jsonify(_serialize_todo(todo))


@flask_app.route("/api/todos/<int:todo_id>/complete", methods=["POST"])
def complete_todo(todo_id):
    todo = _db.get_todo(todo_id)
    if not todo:
        return jsonify({"error": "not found"}), 404
    _db.complete_todo(todo_id)
    return jsonify(_serialize_todo(_db.get_todo(todo_id)))


# ── Stats ──────────────────────────────────────────────────────

@flask_app.route("/api/stats/daily")
def stats_daily():
    rows = _db.get_daily_todo_summary(days=14)
    return jsonify({"rows": [
        {"label": r["date"], "total_seconds": r["total_seconds"],
         "formatted": format_duration(r["total_seconds"])} for r in rows
    ]})


@flask_app.route("/api/stats/weekly")
def stats_weekly():
    rows = _db.get_weekly_todo_totals(weeks=8)
    return jsonify({"rows": [
        {"label": r["week"], "total_seconds": r["total_seconds"],
         "formatted": format_duration(r["total_seconds"])} for r in rows
    ]})


@flask_app.route("/api/stats/monthly")
def stats_monthly():
    rows = _db.get_monthly_todo_totals(months=6)
    return jsonify({"rows": [
        {"label": r["month"], "total_seconds": r["total_seconds"],
         "formatted": format_duration(r["total_seconds"])} for r in rows
    ]})


def find_free_port(start: int) -> int:
    port = start
    while port < start + 20:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("localhost", port)) != 0:
                return port
        port += 1
    return start


def run_flask(db, tracker, config: dict, shutdown_event=None):
    init_app(db, tracker=tracker)
    port = find_free_port(config.get("flask_port", 5000))
    config["_actual_port"] = port
    flask_app.run(host="127.0.0.1", port=port, use_reloader=False, threaded=True)
