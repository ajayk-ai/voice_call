"""Daily overdue checks: periods, streaks and the scenario they confirm."""

from datetime import datetime, timedelta

from backend.po_followup.domain.overdue import confirmed_level, history, overdue_now, period_start, streaks
from backend.po_followup.services.run import ZONE

NOW = datetime(2026, 10, 15, 8, 30, tzinfo=ZONE)
DAY = datetime(2026, 10, 15, tzinfo=ZONE)


def day(n):
    return DAY - timedelta(days=n)


def test_period_is_the_ist_day_or_a_minute_slot():
    assert period_start(NOW, 1440) == DAY
    assert period_start(NOW.replace(hour=23, minute=59), 1440) == DAY
    assert period_start(NOW.replace(minute=34), 5) == NOW.replace(minute=30)


def test_overdue_now_uses_the_pending_rule():
    rows = [{"PO No": "1", "PO Item No": "10", "Vendor Code": "400186", "OD Days": "3"},
            {"PO No": "2", "Vendor Code": "400186", "OD Days": "0"},
            {"PO No": "3", "Vendor Code": "400186", "OD Days": ""}]
    assert overdue_now(rows) == {"1/10/1": {"vendor_code": "400186", "od": 3.0}}


def test_streak_counts_checks_in_a_row_and_a_gap_restarts_it():
    checks = [(day(0), {"a", "b", "c"}), (day(1), {"a", "b"}), (day(2), {"a"}), (day(3), {"a", "c"})]
    assert streaks(checks, ["a", "b", "c"]) == {"a": 4, "b": 2, "c": 1}


def test_missing_day_is_skipped_not_a_reset():
    # no check ran on day 1 (server off): days 0 and 2 are still "in a row"
    assert streaks([(day(0), {"a"}), (day(2), {"a"})], ["a"]) == {"a": 2}


def test_streak_confirms_scenarios():
    assert [confirmed_level(n) for n in (1, 2, 3, 4, 9)] == [0, 1, 2, 3, 3]


def test_history_is_oldest_first():
    checks = [(day(0), {"a"}), (day(1), set()), (day(2), {"a"})]
    assert [h["overdue"] for h in history(checks, "a")] == [True, False, True]
    assert history(checks, "a")[0]["at"] == day(2).isoformat()
