import json
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template, request

flask_app = Flask(__name__)
_db = None
_tracker = None
_config_path = Path(__file__).parent / "config.json"


def init_app(db, tracker=None, config_path: Path = None):
    global _db, _tracker, _config_path
    _db = db
    if tracker is not None:
        _tracker = tracker
    if config_path:
        _config_path = config_path


def format_duration(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
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
    date = datetime.now().strftime("%Y-%m-%d")
    total = _db.get_today_total_seconds(date)
    sessions = _db.get_sessions_for_date(date)
    return jsonify({
        "date": date,
        "total_seconds": total,
        "total_formatted": format_duration(total),
        "sessions": sessions
    })


@flask_app.route("/api/summary/week")
def summary_week():
    today = datetime.now()
    start = (today - timedelta(days=6)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")
    rows = _db.get_weekly_summary(start, end)
    # Fill in days with 0 if missing
    all_days = {}
    for i in range(7):
        d = (today - timedelta(days=6 - i)).strftime("%Y-%m-%d")
        all_days[d] = 0
    for row in rows:
        all_days[row["date"]] = row["total_seconds"]
    return jsonify({
        "days": [{"date": d, "total_seconds": s, "formatted": format_duration(s)}
                 for d, s in all_days.items()]
    })


@flask_app.route("/api/apps/today")
def apps_today():
    date = datetime.now().strftime("%Y-%m-%d")
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
    if _tracker is None:
        return jsonify({"state": "unknown", "today_total_seconds": 0,
                        "excluded_app": None, "idle_seconds": 0})
    return jsonify(_tracker.get_status())


def _get_excluded_processes() -> set:
    try:
        with open(_config_path) as f:
            cfg = json.load(f)
        return {p.lower() for p in cfg.get("excluded_processes", [])}
    except Exception:
        return set()


@flask_app.route("/api/sessions/<date>")
def sessions_for_date(date):
    sessions = _db.get_sessions_for_date(date)
    return jsonify({"sessions": sessions})


@flask_app.route("/api/config", methods=["GET"])
def get_config():
    with open(_config_path) as f:
        return jsonify(json.load(f))


# ── Todo API ──────────────────────────────────────────────────

def _serialize_todo(t: dict) -> dict:
    secs = t["total_seconds"] or 0
    est = t.get("estimated_seconds")
    pct = round(secs / est * 100) if est and est > 0 else None
    return {
        "id": t["id"],
        "title": t["title"],
        "status": t["status"],
        "priority": t["priority"],
        "total_seconds": secs,
        "total_formatted": format_duration(secs),
        "estimated_seconds": est,
        "progress_pct": pct,
        "notes": t.get("notes") or "",
        "created_at": t["created_at"],
        "completed_at": t.get("completed_at"),
    }


@flask_app.route("/api/todos", methods=["GET"])
def list_todos():
    status = request.args.get("status")
    todos = _db.get_todos(status_filter=status)
    date = datetime.now().strftime("%Y-%m-%d")
    return jsonify({
        "todos": [_serialize_todo(t) for t in todos],
        "completed_today": _db.get_completed_today_count(date),
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
    rows = _db.get_daily_summary(days=14)
    return jsonify({"rows": [
        {"label": r["date"], "total_seconds": r["total_seconds"],
         "formatted": format_duration(r["total_seconds"])} for r in rows
    ]})


@flask_app.route("/api/stats/weekly")
def stats_weekly():
    rows = _db.get_weekly_totals(weeks=8)
    return jsonify({"rows": [
        {"label": r["week"], "total_seconds": r["total_seconds"],
         "formatted": format_duration(r["total_seconds"])} for r in rows
    ]})


@flask_app.route("/api/stats/monthly")
def stats_monthly():
    rows = _db.get_monthly_totals(months=6)
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
