"""Tests for the tick-accumulator DatabaseManager."""
from database import DatabaseManager


def _today():
    return DatabaseManager.kst_today()


class TestAccumulation:
    def test_apply_tick_accrues_todo_and_app(self, mem_db):
        d = _today()
        tid = mem_db.create_todo("t")
        mem_db.set_active_todo(tid)
        for i in range(3):
            mem_db.apply_tick(f"e{i}", d, 10, "code.exe", False, tid)
        assert mem_db.get_today_todo_total_seconds(d) == 30
        apps = {a["process_name"]: a for a in mem_db.get_app_breakdown(d)}
        assert apps["code.exe"]["total_seconds"] == 30
        assert mem_db.get_todo(tid)["total_seconds"] == 30

    def test_apply_tick_idempotent(self, mem_db):
        d = _today()
        tid = mem_db.create_todo("t")
        mem_db.apply_tick("dup", d, 10, "code.exe", False, tid)
        mem_db.apply_tick("dup", d, 10, "code.exe", False, tid)  # replay
        assert mem_db.get_today_todo_total_seconds(d) == 10

    def test_zero_seconds_returns_true(self, mem_db):
        d = _today()
        assert mem_db.apply_tick("z", d, 0, "code.exe", False, None) is True

    def test_excluded_app_not_credited_to_todo(self, mem_db):
        d = _today()
        mem_db.apply_tick("x", d, 20, "chrome.exe", True, None)
        assert mem_db.get_today_todo_total_seconds(d) == 0
        apps = {a["process_name"]: a for a in mem_db.get_app_breakdown(d)}
        assert apps["chrome.exe"]["total_seconds"] == 20
        assert bool(apps["chrome.exe"]["is_excluded"]) is True


class TestSelection:
    def test_set_and_get_active(self, mem_db):
        tid = mem_db.create_todo("t")
        assert mem_db.set_active_todo(tid) is True
        assert mem_db.get_active_todo_id() == tid
        assert mem_db.get_todo(tid)["status"] == "in_progress"

    def test_starting_one_pauses_others(self, mem_db):
        a = mem_db.create_todo("a")
        b = mem_db.create_todo("b")
        mem_db.set_active_todo(a)
        mem_db.set_active_todo(b)
        assert mem_db.get_todo(a)["status"] == "paused"
        assert mem_db.get_active_todo_id() == b

    def test_completed_cannot_restart(self, mem_db):
        tid = mem_db.create_todo("t")
        mem_db.complete_todo(tid)
        assert mem_db.set_active_todo(tid) is False
        assert mem_db.get_todo(tid)["status"] == "done"


class TestDayBoundary:
    def test_prior_day_todo_auto_completes(self, mem_db):
        tid = mem_db.create_todo("t")
        mem_db.set_active_todo(tid)
        mem_db.apply_tick("y", "2000-01-01", 100, "code.exe", False, tid)
        assert mem_db.complete_day_crossed_todos() == 1
        assert mem_db.get_todo(tid)["status"] == "done"

    def test_today_todo_not_completed(self, mem_db):
        d = _today()
        tid = mem_db.create_todo("t")
        mem_db.set_active_todo(tid)
        mem_db.apply_tick("t1", d, 30, "code.exe", False, tid)
        assert mem_db.complete_day_crossed_todos() == 0
        assert mem_db.get_todo(tid)["status"] == "in_progress"


class TestStats:
    def test_daily_summary_groups_by_date(self, mem_db):
        d = _today()
        tid = mem_db.create_todo("t")
        mem_db.apply_tick("a", d, 60, "code.exe", False, tid)
        rows = {r["date"]: r["total_seconds"] for r in mem_db.get_daily_todo_summary(14)}
        assert rows.get(d) == 60
