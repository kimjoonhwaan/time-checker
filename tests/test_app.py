import socket
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

import app as app_module
from app import flask_app, format_duration, find_free_port, init_app


# ── format_duration ───────────────────────────────────────────

class TestFormatDuration:
    def test_seconds_only(self):
        assert format_duration(59) == "59s"

    def test_zero(self):
        assert format_duration(0) == "0s"

    def test_minutes_and_seconds(self):
        assert format_duration(90) == "1m 30s"

    def test_hours_minutes_seconds(self):
        assert format_duration(3661) == "1h 01m 01s"

    def test_exact_hour(self):
        assert format_duration(3600) == "1h 00m 00s"

    def test_large_value(self):
        assert format_duration(7322) == "2h 02m 02s"


# ── find_free_port ────────────────────────────────────────────

class TestFindFreePort:
    def test_returns_start_when_free(self):
        # Find an actually free port and confirm it's returned
        with socket.socket() as s:
            s.bind(("localhost", 0))
            free_port = s.getsockname()[1]
        result = find_free_port(free_port)
        assert result == free_port

    def test_skips_occupied_port(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
            srv.bind(("localhost", 0))
            srv.listen(1)
            occupied = srv.getsockname()[1]
            result = find_free_port(occupied)
        assert result != occupied


# ── Flask API (real in-memory DB, tick-accumulator model) ─────

from database import DatabaseManager


@pytest.fixture
def client(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text('{"idle_threshold_seconds": 60}')
    db = DatabaseManager(":memory:")
    init_app(db, tracker=None, config_path=cfg_path, api_key=None)
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        c._db = db
        yield c
    db.close()


def _today():
    return DatabaseManager.kst_today()


class TestSummaryToday:
    def test_json_shape(self, client):
        res = client.get("/api/summary/today")
        assert res.status_code == 200
        data = res.get_json()
        assert {"date", "todo_total_seconds", "todo_total_formatted"} <= data.keys()

    def test_reflects_accrued_ticks(self, client):
        tid = client.post("/api/todos", json={"title": "t"}).get_json()["id"]
        client.post(f"/api/todos/{tid}/start")
        client.post("/api/ingest/tick", json={
            "event_id": "a", "kst_date": _today(), "active_seconds": 3661,
            "process_name": "code.exe", "todo_id": tid, "state": "active"})
        data = client.get("/api/summary/today").get_json()
        assert data["todo_total_seconds"] == 3661
        assert data["todo_total_formatted"] == "1h 01m 01s"


class TestTodoLifecycle:
    def test_start_sets_active(self, client):
        tid = client.post("/api/todos", json={"title": "t"}).get_json()["id"]
        client.post(f"/api/todos/{tid}/start")
        assert client.get("/api/active-todo").get_json()["todo_id"] == tid

    def test_completed_cannot_restart(self, client):
        tid = client.post("/api/todos", json={"title": "t"}).get_json()["id"]
        client.post(f"/api/todos/{tid}/complete")
        res = client.post(f"/api/todos/{tid}/start")
        assert res.status_code == 400


class TestIngestTickIdempotent:
    def test_replay_does_not_double_count(self, client):
        tid = client.post("/api/todos", json={"title": "t"}).get_json()["id"]
        client.post(f"/api/todos/{tid}/start")
        payload = {"event_id": "dup", "kst_date": _today(), "active_seconds": 10,
                   "process_name": "code.exe", "todo_id": tid, "state": "active"}
        client.post("/api/ingest/tick", json=payload)
        client.post("/api/ingest/tick", json=payload)  # replay
        assert client.get("/api/summary/today").get_json()["todo_total_seconds"] == 10


class TestIngestTickTriggersDayCross:
    def test_first_tick_completes_prior_day_todo(self, client):
        # Yesterday's in_progress todo (no todo_time rows).
        tid = client.post("/api/todos", json={"title": "old"}).get_json()["id"]
        client.post(f"/api/todos/{tid}/start")
        client._db._conn.execute(
            "UPDATE todos SET created_at=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", tid),
        )
        client._db._conn.commit()
        # Reset the module-level throttle so this test's tick actually fires
        # the day-cross check (other tests may have already set it).
        app_module._last_day_cross_check = None
        client.post("/api/ingest/tick", json={
            "event_id": "x", "kst_date": _today(), "active_seconds": 1,
            "process_name": "code.exe", "state": "active"})
        assert client.get(f"/api/todos/{tid}").get_json()["status"] == "done"


class TestAppsToday:
    def test_keys_and_percentage(self, client):
        client.post("/api/ingest/tick", json={
            "event_id": "p", "kst_date": _today(), "active_seconds": 100,
            "process_name": "code.exe", "excluded": False, "state": "active"})
        data = client.get("/api/apps/today").get_json()
        assert data["apps"]
        for app in data["apps"]:
            assert {"process_name", "total_seconds", "formatted",
                    "percentage", "is_excluded"} <= app.keys()
        assert abs(sum(a["percentage"] for a in data["apps"]) - 100.0) < 0.1
