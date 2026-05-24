"""Flask API + dashboard for TimeChecker.

All "today" boundaries are UTC; the dashboard displays everything in the
browser's local timezone. The simplified model removed OS-session endpoints
and the redundant total fields — only todo time + app breakdown are exposed.
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
_api_key: str | None = None
_last_heartbeat: dict = {"time": None, "device_id": None, "state": "unknown",
                        "idle_seconds": 0, "excluded_app": None}


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


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


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


@flask_app.route("/api/summary/today")
def summary_today():
    date = _today_utc()
    todo_total = _db.get_today_todo_total_seconds(date)
    return jsonify({
        "date": date,
        "todo_total_seconds": todo_total,
        "todo_total_formatted": format_duration(todo_total),
    })


@flask_app.route("/api/apps/today")
def apps_today():
    date = _today_utc()
    rows = _db.get_app_breakdown(date)
    total = sum(r["total_seconds"] for r in rows)
    excluded_processes = _get_excluded_processes()
    result = []
    for r in rows:
        pct = round(r["total_seconds"] / total * 100, 1) if total else 0
        result.append({
            "process_name": r["process_name"],
            "total_seconds": r["total_seconds"],
            "formatted": format_duration(r["total_seconds"]),
            "percentage": pct,
            "is_excluded": r["process_name"].lower() in excluded_processes,
        })
    return jsonify({"apps": result})


@flask_app.route("/api/tracker/status")
def tracker_status():
    # In remote-server mode the tracker lives on a different machine, so we
    # derive state from the last heartbeat. Local-mode (no remote) uses the
    # tracker instance directly.
    if _tracker is None:
        date = _today_utc()
        todo_total = _db.get_today_todo_total_seconds(date) if _db else 0
        last_hb = _last_heartbeat.get("time")
        stale = True
        if last_hb:
            try:
                dt = datetime.fromisoformat(last_hb)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                stale = (datetime.now(timezone.utc) - dt).total_seconds() > 120
            except Exception:
                stale = True
        state = "offline" if stale else _last_heartbeat.get("state", "unknown")
        return jsonify({
            "state": state,
            "today_total_seconds": todo_total,
            "excluded_app": _last_heartbeat.get("excluded_app"),
            "idle_seconds": _last_heartbeat.get("idle_seconds", 0),
            "last_heartbeat": last_hb,
            "device_id": _last_heartbeat.get("device_id"),
        })
    return jsonify(_tracker.get_status())


# ── Ingest API (write surface for remote clients) ──────────────

def _require_api_key():
    if not _api_key:
        return None  # auth disabled (dev mode)
    if request.headers.get("X-API-Key") != _api_key:
        return jsonify({"error": "unauthorized"}), 401
    return None


@flask_app.route("/api/ingest/activity/open", methods=["POST"])
def ingest_activity_open():
    err = _require_api_key()
    if err:
        return err
    d = request.get_json(force=True)
    aid = _db.open_app_activity(
        process_name=d["process_name"],
        window_title=d.get("window_title", ""),
        start_time=d["start_time"],
        todo_id=d.get("todo_id"),
        device_id=d.get("device_id"),
        client_event_id=d.get("client_event_id"),
    )
    return jsonify({"activity_id": aid})


@flask_app.route("/api/ingest/activity/close", methods=["POST"])
def ingest_activity_close():
    err = _require_api_key()
    if err:
        return err
    d = request.get_json(force=True)
    aid = d.get("activity_id")
    if aid is None:
        eid = d.get("activity_client_event_id")
        if eid:
            row = _db._conn.execute(
                "SELECT id FROM app_activity WHERE client_event_id = ?", (eid,)
            ).fetchone()
            if row:
                aid = row["id"]
    if aid is None:
        return jsonify({"error": "activity not found"}), 404
    _db.close_app_activity(activity_id=int(aid), end_time=d["end_time"])
    return jsonify({"ok": True})


@flask_app.route("/api/ingest/todo/start", methods=["POST"])
def ingest_todo_start():
    err = _require_api_key()
    if err:
        return err
    d = request.get_json(force=True)
    sid = _db.start_todo_timer(int(d["todo_id"]))
    return jsonify({"todo_session_id": sid})


@flask_app.route("/api/ingest/todo/stop", methods=["POST"])
def ingest_todo_stop():
    err = _require_api_key()
    if err:
        return err
    d = request.get_json(force=True)
    kwargs = {"reason": d.get("reason", "manual")}
    if d.get("end_time"):
        kwargs["end_time"] = d["end_time"]
    _db.stop_todo_timer(int(d["todo_id"]), **kwargs)
    return jsonify({"ok": True})


@flask_app.route("/api/ingest/todo/active", methods=["GET"])
def ingest_todo_active():
    err = _require_api_key()
    if err:
        return err
    row = _db.get_active_todo_session()
    return jsonify({"active": row})


@flask_app.route("/api/ingest/todo/auto_paused", methods=["GET"])
def ingest_todo_auto_paused():
    err = _require_api_key()
    if err:
        return err
    row = _db.get_recently_auto_paused_todo()
    return jsonify({"todo": row})


@flask_app.route("/api/ingest/heartbeat", methods=["POST"])
def ingest_heartbeat():
    err = _require_api_key()
    if err:
        return err
    d = request.get_json(force=True) or {}
    _last_heartbeat["time"] = datetime.now(timezone.utc).isoformat()
    _last_heartbeat["device_id"] = d.get("device_id")
    _last_heartbeat["state"] = d.get("state", "unknown")
    _last_heartbeat["idle_seconds"] = d.get("idle_seconds", 0)
    _last_heartbeat["excluded_app"] = d.get("excluded_app")
    _db.record_heartbeat(d.get("device_id"), _last_heartbeat["time"])
    # Periodic (≤30s) trigger for KST day-boundary auto-completion in REMOTE mode.
    try:
        _db.complete_day_crossed_todos()
    except Exception:
        pass
    return jsonify({"ok": True})


@flask_app.route("/api/admin/cleanup", methods=["POST"])
def admin_cleanup():
    err = _require_api_key()
    if err:
        return err
    return jsonify(_db.cleanup_orphan_todo_sessions())


def _get_excluded_processes() -> set:
    try:
        with open(_config_path) as f:
            cfg = json.load(f)
        return {p.lower() for p in cfg.get("excluded_processes", [])}
    except Exception:
        return set()


@flask_app.route("/api/config", methods=["GET"])
def get_config():
    with open(_config_path) as f:
        return jsonify(json.load(f))


# ── Todo API ──────────────────────────────────────────────────

def _serialize_todo(t: dict) -> dict:
    secs = t["total_seconds"] or 0
    active_secs = 0
    active_start = None
    if t["status"] == "in_progress":
        row = _db._conn.execute(
            "SELECT start_time FROM todo_sessions "
            "WHERE todo_id = ? AND end_time IS NULL "
            "ORDER BY id DESC LIMIT 1",
            (t["id"],)
        ).fetchone()
        if row:
            try:
                start = datetime.fromisoformat(row["start_time"])
                if start.tzinfo is None:
                    start = start.replace(tzinfo=timezone.utc)
                active_secs = max(0, int(
                    (_db._live_cutoff() - start).total_seconds()
                ))
                active_start = row["start_time"]
            except Exception:
                pass
    total_with_active = secs + active_secs
    est = t.get("estimated_seconds")
    pct = round(total_with_active / est * 100) if est and est > 0 else None
    return {
        "id": t["id"],
        "title": t["title"],
        "status": t["status"],
        "priority": t["priority"],
        "total_seconds": total_with_active,
        "total_formatted": format_duration(total_with_active),
        "active_session_start": active_start,
        "estimated_seconds": est,
        "progress_pct": pct,
        "notes": t.get("notes") or "",
        "created_at": t["created_at"],
        "completed_at": t.get("completed_at"),
    }


@flask_app.route("/api/todos", methods=["GET"])
def list_todos():
    # Self-heal on dashboard load: completes prior-day tasks even if no tracker
    # is running to send heartbeats (e.g. tracker was off overnight).
    try:
        _db.complete_day_crossed_todos()
    except Exception:
        pass
    status = request.args.get("status")
    todos = _db.get_todos(status_filter=status)
    return jsonify({
        "todos": [_serialize_todo(t) for t in todos],
        "completed_today": _db.get_completed_today_count(_today_utc()),
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
    if todo["status"] == "done":
        return jsonify({"error": "completed task cannot be restarted"}), 400
    _db.start_todo_timer(todo_id)
    return jsonify(_serialize_todo(_db.get_todo(todo_id)))


@flask_app.route("/api/todos/<int:todo_id>/stop", methods=["POST"])
def stop_todo(todo_id):
    _db.stop_todo_timer(todo_id, reason="manual")
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


def _format_local_time(iso_str: str) -> str:
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%H:%M")


def _reason_label(reason: str) -> str:
    if not reason:
        return "중단"
    if reason == "manual":
        return "수동 중단"
    if reason == "idle":
        return "유휴 중단 (키보드·마우스 없음)"
    if reason == "completed":
        return "작업 완료"
    if reason == "abandoned":
        return "비정상 종료로 자동 정리"
    if reason == "interrupted":
        return "다른 작업 시작으로 중단"
    if reason.startswith("excluded:"):
        app = reason[len("excluded:"):]
        return f"{app} 실행으로 중단"
    return "중단"


@flask_app.route("/api/todos/<int:todo_id>/history")
def todo_history(todo_id):
    rows = _db.get_todo_history(todo_id)
    events = []
    for r in rows:
        events.append({
            "type": "start",
            "time": _format_local_time(r["start_time"]),
            "label": "작업 시작",
        })
        if r["end_time"]:
            events.append({
                "type": "stop",
                "time": _format_local_time(r["end_time"]),
                "label": _reason_label(r["pause_reason"]),
                "duration": format_duration(r["duration_seconds"] or 0),
            })
    return jsonify({"history": events})


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
