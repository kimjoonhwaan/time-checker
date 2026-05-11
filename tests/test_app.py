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


# ── Flask API ─────────────────────────────────────────────────

@pytest.fixture
def mock_db():
    db = MagicMock()
    db.get_today_total_seconds.return_value = 3661
    db.get_sessions_for_date.return_value = [
        {"id": 1, "start_time": "2025-01-01T09:00:00+00:00",
         "end_time": "2025-01-01T10:01:01+00:00", "total_seconds": 3661}
    ]
    db.get_app_breakdown.return_value = [
        {"process_name": "code.exe", "total_seconds": 1800},
        {"process_name": "chrome.exe", "total_seconds": 1800},
    ]
    db.get_weekly_summary.return_value = []
    return db


@pytest.fixture
def client(mock_db, tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text('{"idle_threshold_seconds": 300}')
    init_app(mock_db, cfg_path)
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


class TestSummaryToday:
    def test_json_shape(self, client):
        res = client.get("/api/summary/today")
        assert res.status_code == 200
        data = res.get_json()
        assert "date" in data
        assert "total_seconds" in data
        assert "total_formatted" in data
        assert "sessions" in data

    def test_total_formatted_value(self, client):
        res = client.get("/api/summary/today")
        data = res.get_json()
        assert data["total_seconds"] == 3661
        assert data["total_formatted"] == "1h 01m 01s"


class TestSummaryWeek:
    def test_always_returns_seven_days(self, client, mock_db):
        mock_db.get_weekly_summary.return_value = []
        res = client.get("/api/summary/week")
        data = res.get_json()
        assert len(data["days"]) == 7

    def test_missing_days_filled_with_zero(self, client, mock_db):
        mock_db.get_weekly_summary.return_value = []
        res = client.get("/api/summary/week")
        data = res.get_json()
        assert all(d["total_seconds"] == 0 for d in data["days"])

    def test_known_day_has_correct_total(self, client, mock_db):
        today = datetime.now().strftime("%Y-%m-%d")
        mock_db.get_weekly_summary.return_value = [{"date": today, "total_seconds": 7200}]
        res = client.get("/api/summary/week")
        data = res.get_json()
        today_entry = next(d for d in data["days"] if d["date"] == today)
        assert today_entry["total_seconds"] == 7200


class TestAppsToday:
    def test_percentage_sums_to_100(self, client):
        res = client.get("/api/apps/today")
        data = res.get_json()
        total_pct = sum(a["percentage"] for a in data["apps"])
        assert abs(total_pct - 100.0) < 0.1

    def test_each_app_has_required_keys(self, client):
        res = client.get("/api/apps/today")
        data = res.get_json()
        for app in data["apps"]:
            assert "process_name" in app
            assert "total_seconds" in app
            assert "formatted" in app
            assert "percentage" in app


class TestSessionsForDate:
    def test_passes_date_to_db(self, client, mock_db):
        mock_db.get_sessions_for_date.return_value = []
        client.get("/api/sessions/2025-06-01")
        mock_db.get_sessions_for_date.assert_called_once_with("2025-06-01")
