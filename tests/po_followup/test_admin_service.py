from datetime import datetime, timedelta

import pytest

from backend.po_followup.services import followup as service
from backend.po_followup.services.run import ZONE

NOW = datetime(2026, 10, 8, 15, 0, tzinfo=ZONE)


def settings(**kw):
    return {"auto_enabled": True, "schedule_type": "daily", "daily_time": "08:30", "every_minutes": 5, **kw}


@pytest.fixture
def last_run(monkeypatch):
    """holder["t"]: start of the latest automatic run (ok); holder["runs"]: full history, newest first."""
    holder = {"t": None, "runs": None}

    def recent(env, limit=10):
        if holder["runs"] is not None:
            return holder["runs"]
        return [{"id": 1, "started_at": holder["t"], "status": "ok", "error": None}] if holder["t"] else []

    monkeypatch.setattr(service, "recent_auto_runs", recent)
    return holder


def auto(t, status):
    return {"id": 1, "started_at": t, "status": status, "error": None}


def test_auto_off(last_run):
    assert service.next_auto_run("test", settings(auto_enabled=False), NOW) is None


def test_daily_enabled_in_afternoon_waits_for_tomorrow(last_run):
    nxt = service.next_auto_run("prod", settings(), NOW)
    assert nxt == datetime(2026, 10, 9, 8, 30, tzinfo=ZONE)


def test_daily_catches_up_when_slightly_late(last_run):
    late = NOW.replace(hour=9, minute=45)
    assert service.is_due("prod", settings(), late)


def test_daily_runs_once_per_day(last_run):
    last_run["t"] = NOW.replace(hour=8, minute=30, second=3)
    assert not service.is_due("prod", settings(), NOW.replace(hour=9))
    assert service.next_auto_run("prod", settings(), NOW) == datetime(2026, 10, 9, 8, 30, tzinfo=ZONE)


def test_interval_schedule(last_run):
    s = settings(schedule_type="interval", every_minutes=5)
    assert service.is_due("test", s, NOW)  # never ran
    last_run["t"] = NOW - timedelta(minutes=3)
    assert not service.is_due("test", s, NOW)
    last_run["t"] = NOW - timedelta(minutes=5)
    assert service.is_due("test", s, NOW)


@pytest.mark.parametrize("bad", [
    {"intervals_minutes": [1, 2]}, {"intervals_minutes": [0, 5, 5]}, {"daily_time": "25:00"},
    {"auto_mode": "send"}, {"every_minutes": 0}, {"pilot_mobile": "abc"}, {"nope": 1},
])
def test_invalid_settings_rejected(bad):
    with pytest.raises(ValueError):
        service.validate_settings(bad)


def test_valid_settings_pass():
    ok = {"intervals_minutes": [30, 60, 1440], "daily_time": "08:30", "auto_mode": "live", "pilot_mobile": "+91 72000"}
    assert service.validate_settings(ok) == ok


def test_failed_daily_run_is_retried(last_run):
    first = NOW.replace(hour=8, minute=30, second=2)
    last_run["runs"] = [auto(first, "failed")]
    assert not service.is_due("prod", settings(), first + timedelta(minutes=5))
    assert service.is_due("prod", settings(), first + timedelta(minutes=10))


def test_daily_retries_stop_after_max_attempts(last_run):
    t = NOW.replace(hour=8, minute=30)
    last_run["runs"] = [auto(t + timedelta(minutes=20), "failed"), auto(t + timedelta(minutes=10), "failed"), auto(t, "failed")]
    assert not service.is_due("prod", settings(), t + timedelta(minutes=45))
    assert service.next_auto_run("prod", settings(), t + timedelta(minutes=45)) == datetime(2026, 10, 9, 8, 30, tzinfo=ZONE)


def test_daily_retry_succeeded_means_done_for_today(last_run):
    t = NOW.replace(hour=8, minute=30)
    last_run["runs"] = [auto(t + timedelta(minutes=10), "ok"), auto(t, "failed")]
    assert not service.is_due("prod", settings(), t + timedelta(hours=1))


def test_yesterdays_failures_do_not_count_today(last_run):
    t = NOW.replace(hour=8, minute=30) - timedelta(days=1)
    last_run["runs"] = [auto(t + timedelta(minutes=m), "failed") for m in (20, 10, 0)]
    assert service.is_due("prod", settings(), NOW.replace(hour=8, minute=31))


def test_stale_vendor_test_is_stopped_not_resumed():
    vt = {"started_at": (NOW - timedelta(hours=3)).isoformat(), "stopped_at": None, "minutes": 1}
    s = {"auto_enabled": True, "vendor_test": vt}
    assert service.stale_vendor_test(s, NOW - timedelta(hours=2), NOW)
    assert not service.stale_vendor_test(s, NOW - timedelta(minutes=2), NOW)  # running normally
    assert not service.stale_vendor_test({**s, "auto_enabled": False}, NOW - timedelta(hours=2), NOW)
    assert not service.stale_vendor_test(s | {"vendor_test": {**vt, "minutes": 60}}, NOW - timedelta(hours=2), NOW)
