from datetime import datetime, timezone, timedelta


def _iso(dt): return dt.isoformat()
def _now(): return datetime.now(timezone.utc)


def test_open_session_returns_int_id(mem_db):
    sid = mem_db.open_session(_iso(_now()), "2025-01-01")
    assert isinstance(sid, int) and sid >= 1


def test_open_and_close_session_total_seconds(mem_db):
    start = _now()
    end = start + timedelta(seconds=120)
    sid = mem_db.open_session(_iso(start), "2025-01-01")
    mem_db.close_session(sid, _iso(end))
    rows = mem_db.get_sessions_for_date("2025-01-01")
    assert len(rows) == 1
    assert rows[0]["total_seconds"] == 120


def test_get_today_total_seconds_sums_multiple(mem_db):
    start1 = _now()
    mem_db.close_session(
        mem_db.open_session(_iso(start1), "2025-01-01"),
        _iso(start1 + timedelta(seconds=60))
    )
    start2 = start1 + timedelta(seconds=120)
    mem_db.close_session(
        mem_db.open_session(_iso(start2), "2025-01-01"),
        _iso(start2 + timedelta(seconds=90))
    )
    assert mem_db.get_today_total_seconds("2025-01-01") == 150


def test_get_today_excludes_other_dates(mem_db):
    start = _now()
    mem_db.close_session(
        mem_db.open_session(_iso(start), "2025-01-02"),
        _iso(start + timedelta(seconds=300))
    )
    assert mem_db.get_today_total_seconds("2025-01-01") == 0


def test_get_sessions_for_date_ordered(mem_db):
    t1 = datetime(2025, 1, 1, 8, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2025, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    mem_db.close_session(mem_db.open_session(_iso(t2), "2025-01-01"), _iso(t2 + timedelta(seconds=60)))
    mem_db.close_session(mem_db.open_session(_iso(t1), "2025-01-01"), _iso(t1 + timedelta(seconds=60)))
    rows = mem_db.get_sessions_for_date("2025-01-01")
    assert rows[0]["start_time"] < rows[1]["start_time"]


def test_get_sessions_excludes_open_sessions(mem_db):
    mem_db.open_session(_iso(_now()), "2025-01-01")  # not closed
    rows = mem_db.get_sessions_for_date("2025-01-01")
    assert rows == []


def test_app_breakdown_groups_by_process(mem_db):
    start = _now()
    sid = mem_db.open_session(_iso(start), "2025-01-01")
    mem_db.close_session(sid, _iso(start + timedelta(seconds=200)))

    a1 = mem_db.open_app_activity(sid, "code.exe", "editor", _iso(start))
    mem_db.close_app_activity(a1, _iso(start + timedelta(seconds=100)))
    a2 = mem_db.open_app_activity(sid, "code.exe", "editor2", _iso(start + timedelta(seconds=100)))
    mem_db.close_app_activity(a2, _iso(start + timedelta(seconds=180)))
    a3 = mem_db.open_app_activity(sid, "chrome.exe", "browser", _iso(start + timedelta(seconds=180)))
    mem_db.close_app_activity(a3, _iso(start + timedelta(seconds=200)))

    breakdown = mem_db.get_app_breakdown("2025-01-01")
    by_name = {r["process_name"]: r["total_seconds"] for r in breakdown}
    assert by_name["code.exe"] == 180
    assert by_name["chrome.exe"] == 20


def test_weekly_summary_range(mem_db):
    for day in ["2025-01-01", "2025-01-03", "2025-01-05"]:
        start = datetime.fromisoformat(day + "T09:00:00+00:00")
        mem_db.close_session(
            mem_db.open_session(_iso(start), day),
            _iso(start + timedelta(seconds=3600))
        )
    rows = mem_db.get_weekly_summary("2025-01-01", "2025-01-05")
    dates = [r["date"] for r in rows]
    assert "2025-01-01" in dates
    assert "2025-01-03" in dates
    assert "2025-01-05" in dates
    assert all(r["total_seconds"] == 3600 for r in rows)


def test_close_stale_sessions(mem_db):
    start = _now()
    sid = mem_db.open_session(_iso(start), "2025-01-01")
    aid = mem_db.open_app_activity(sid, "code.exe", "editor", _iso(start))

    mem_db.close_stale_sessions()

    # After cleanup, sessions and activities with NULL end_time should be closed
    rows = mem_db.get_sessions_for_date("2025-01-01")
    # total_seconds is 0 when closed stale
    assert rows[0]["total_seconds"] == 0
