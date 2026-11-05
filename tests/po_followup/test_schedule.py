"""When the next automatic run is due, and which settings are accepted."""

from datetime import datetime, timedelta

import pytest

from backend.core.env import TIMEZONE
from backend.po_followup.services import followup as service

NOW = datetime(2026, 10, 8, 15, 0, tzinfo=TIMEZONE)


def settings(**kw):
    return {"auto_enabled": True, "schedule_type": "daily", "daily_time": "08:30", "every_minutes": 5, **kw}


@pytest.fixture
def last_run(monkeypatch):
    """holder["t"]: start of the latest automatic run (ok); holder["runs"]: full history, newest first."""
    holder = {"t": None, "runs": None}

    def recent(limit=10):
        if holder["runs"] is not None:
            return holder["runs"]
        return [{"id": 1, "started_at": holder["t"], "status": "ok", "error": None}] if holder["t"] else []

    monkeypatch.setattr(service, "recent_auto_runs", recent)
    return holder


def auto(t, status):
    return {"id": 1, "started_at": t, "status": status, "error": None}


def test_auto_off(last_run):
    assert service.next_auto_run(settings(auto_enabled=False), NOW) is None


def test_daily_enabled_in_afternoon_waits_for_tomorrow(last_run):
    nxt = service.next_auto_run(settings(), NOW)
    assert nxt == datetime(2026, 10, 9, 8, 30, tzinfo=TIMEZONE)


def test_daily_catches_up_when_slightly_late(last_run):
    late = NOW.replace(hour=9, minute=45)
    assert service.is_due(settings(), late)


def test_daily_runs_once_per_day(last_run):
    last_run["t"] = NOW.replace(hour=8, minute=30, second=3)
    assert not service.is_due(settings(), NOW.replace(hour=9))
    assert service.next_auto_run(settings(), NOW) == datetime(2026, 10, 9, 8, 30, tzinfo=TIMEZONE)


def test_interval_schedule(last_run):
    s = settings(schedule_type="interval", every_minutes=5)
    assert service.is_due(s, NOW)  # never ran
    last_run["t"] = NOW - timedelta(minutes=3)
    assert not service.is_due(s, NOW)
    last_run["t"] = NOW - timedelta(minutes=5)
    assert service.is_due(s, NOW)


@pytest.mark.parametrize("bad", [
    {"check_period_minutes": 0}, {"check_period_minutes": 2000}, {"check_period_minutes": True}, {"daily_time": "25:00"},
    {"auto_mode": "send"}, {"every_minutes": 0}, {"nope": 1},
])
def test_invalid_settings_rejected(bad):
    with pytest.raises(ValueError):
        service.validate_settings(bad)


def test_valid_settings_pass():
    ok = {"check_period_minutes": 30, "daily_time": "08:30", "auto_mode": "live"}
    assert service.validate_settings(ok) == ok


def test_failed_daily_run_is_retried(last_run):
    first = NOW.replace(hour=8, minute=30, second=2)
    last_run["runs"] = [auto(first, "failed")]
    assert not service.is_due(settings(), first + timedelta(minutes=5))
    assert service.is_due(settings(), first + timedelta(minutes=10))


def test_daily_retries_stop_after_max_attempts(last_run):
    t = NOW.replace(hour=8, minute=30)
    last_run["runs"] = [auto(t + timedelta(minutes=20), "failed"), auto(t + timedelta(minutes=10), "failed"), auto(t, "failed")]
    assert not service.is_due(settings(), t + timedelta(minutes=45))
    assert service.next_auto_run(settings(), t + timedelta(minutes=45)) == datetime(2026, 10, 9, 8, 30, tzinfo=TIMEZONE)


def test_daily_retry_succeeded_means_done_for_today(last_run):
    t = NOW.replace(hour=8, minute=30)
    last_run["runs"] = [auto(t + timedelta(minutes=10), "ok"), auto(t, "failed")]
    assert not service.is_due(settings(), t + timedelta(hours=1))


def test_yesterdays_failures_do_not_count_today(last_run):
    t = NOW.replace(hour=8, minute=30) - timedelta(days=1)
    last_run["runs"] = [auto(t + timedelta(minutes=m), "failed") for m in (20, 10, 0)]
    assert service.is_due(settings(), NOW.replace(hour=8, minute=31))
