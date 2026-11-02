"""Run control: starting / stopping the automatic follow-up from the dashboard, without a database."""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from backend.core.env import TIMEZONE

from backend.po_followup.api import admin
from backend.po_followup.db import store
from backend.po_followup.services import control, users
from backend.po_followup.services import followup as service
from backend.voice_agent.app import app

client = TestClient(app)
REAL_NEXT_AUTO_RUN = service.next_auto_run  # the `state` fixture replaces it


@pytest.fixture
def state(monkeypatch):
    settings = {**service.default_settings(), "vendor_scope": "selected", "vendor_codes": "400186"}
    monkeypatch.setattr(store, "save_settings", settings.update)
    monkeypatch.setattr(store, "master_synced_at", lambda: None)
    monkeypatch.setattr(service, "get_settings", lambda: dict(settings))
    monkeypatch.setattr(service, "next_auto_run", lambda s=None, now=None: None)
    monkeypatch.setattr(service, "list_runs", lambda limit=25: [])
    monkeypatch.setattr(service, "is_busy", lambda: False)
    monkeypatch.setattr(service, "whatsapp_ready", lambda: True)
    monkeypatch.setattr(admin, "_ready", True)
    return settings


def test_start_defaults_to_one_day_and_stop_switches_it_off(state):
    r = control.start("interval", control.default_interval_minutes(), "08:30", "dry", None)
    assert r["running"] and r["every_minutes"] == 1440 and r["mode"] == "dry" and r["schedule_type"] == "interval"
    assert state["check_period_minutes"] == 1440  # production days stay IST days
    assert not control.stop()["running"]


def test_a_repeat_shorter_than_a_day_is_one_check_per_run(state):
    control.start("interval", 3, "08:30", "dry", None)
    assert state["check_period_minutes"] == 3
    control.start("interval", 90, "08:30", "dry", None)
    assert state["check_period_minutes"] == 90
    control.start("interval", 3 * 1440, "08:30", "dry", None)
    assert state["check_period_minutes"] == 1440


def test_intervals_up_to_thirty_days_are_allowed_and_nothing_else(state):
    control.start("interval", 30 * 1440, "08:30", "dry", None)
    for bad in (0, 0.5, 30 * 1440 + 1):
        with pytest.raises(ValueError, match="(?i)interval"):
            control.start("interval", bad, "08:30", "dry", None)


def test_live_in_production_needs_the_word_live_and_vendors_must_be_chosen(state):
    with pytest.raises(ValueError, match="confirm = LIVE"):
        control.start("interval", 1440, "08:30", "live", None)
    assert control.start("interval", 1440, "08:30", "live", "LIVE")["running"]
    state["vendor_codes"] = ""
    with pytest.raises(ValueError, match="No vendor is selected"):
        control.start("interval", 5, "08:30", "dry", None)
    state["vendor_scope"] = "all"  # every vendor: nothing to choose
    assert control.start("interval", 5, "08:30", "dry", None)["running"]


def test_a_signed_in_user_starts_and_stops_and_the_audit_names_them(state, monkeypatch):
    seen = []
    monkeypatch.setattr(users, "current", lambda token: {"username": "ravi", "role": "scm"} if token else None)
    monkeypatch.setattr(users, "audit", lambda actor, action, detail=None: seen.append((actor, action)))
    h = {"X-Session": "signed-in"}
    assert client.get("/dashboard/api/control").status_code == 401
    r = client.post("/dashboard/api/control/start", json={"every_minutes": 1440, "mode": "dry"}, headers=h)
    assert r.status_code == 200 and r.json()["running"] is True
    assert client.post("/dashboard/api/control/stop", headers=h).json()["running"] is False
    assert seen == [("ravi", "follow_up_started"), ("ravi", "follow_up_stopped")]


def test_the_page_rejects_nonsense_and_a_live_run_now_needs_confirmation(state, monkeypatch):
    monkeypatch.setattr(users, "current", lambda token: {"username": "ravi", "role": "scm"})
    h = {"X-Session": "signed-in"}
    assert client.post("/dashboard/api/control/start", json={"every_minutes": 0}, headers=h).status_code == 422
    assert client.post("/dashboard/api/control/start", json={"mode": "yolo"}, headers=h).status_code == 422
    r = client.post("/dashboard/api/control/run-now", json={"mode": "live"}, headers=h)
    assert r.status_code == 400 and "LIVE" in r.json()["error"]


def test_start_chooses_the_vendors_in_the_same_step(state, monkeypatch):
    from backend.po_followup.services import vendor_numbers

    seen = []
    monkeypatch.setattr(vendor_numbers, "set_selection", lambda codes, scope, confirm: seen.append((codes, scope, confirm)))
    control.start("interval", 1440, "08:30", "dry", None, scope="selected", codes=["1", "2"])
    assert seen == [(["1", "2"], "selected", None)]
    with pytest.raises(ValueError, match="at least one vendor"):
        control.start("interval", 1440, "08:30", "dry", None, scope="selected", codes=[])
    control.start("interval", 1440, "08:30", "dry", None, scope="all", confirm_scope="ALL")
    assert seen[-1] == ([], "all", "ALL")


def test_trace_lists_only_the_chosen_vendors_and_failed_deliveries(state, monkeypatch):
    from backend.po_followup.services import trace, vendor_numbers

    state.update(vendor_scope="selected", vendor_codes="1,3")
    def row(code):
        return {"code": code, "name": "V" + code, "pending_lines": 2, "max_od": 5, "level": 0, "step": "Scenario 1", "last_alert_at": None, "in_pilot": code != "2"}

    monkeypatch.setattr(trace, "vendor_list", lambda: [row("1"), row("2")])
    monkeypatch.setattr(vendor_numbers, "list_numbers", lambda: {"vendors": [{"code": "3", "name": "No overdue"}]})
    monkeypatch.setattr(trace, "source_records", lambda title: [
        {"PO No": "9", "Vendor Code": "3", "Vendor Name": "No overdue", "OD Days": "0", "Purchasing Group": "210", "Buyer Mail ID": "ravi@bull.com"}])
    monkeypatch.setattr(control.calls, "latest_calls", lambda: {"1": {"status": "completed"}})
    monkeypatch.setattr(store, "table_rows", lambda title, limit: [
        {"Vendor": "V1 (1)", "Status": "INVALID_MOBILE", "Error": "Invalid/Missing Mobile Number"},
        {"Vendor": "V1 (1)", "Status": "SENT", "Error": ""}])
    out = {v["code"]: v for v in control.chosen_vendors()}
    assert set(out) == {"1", "3"}
    assert out["1"]["problem"] == "Invalid/Missing Mobile Number" and out["3"]["name"] == "No overdue" and not out["3"]["problem"]
    assert out["1"]["call"] == {"status": "completed"} and out["3"]["call"] is None
    assert out["3"]["by_pg"] == {"210": {"lines": 0, "max_od": 0, "pos": 0}} and out["3"]["buyers"] == ["ravi"]  # from Master, nothing overdue


def test_emergency_stop_switches_everything_off_until_cleared(state, monkeypatch):
    seen, hung, failed = [], [], []
    monkeypatch.setattr(users, "audit", lambda actor, action, detail=None: seen.append((actor, action, detail)))
    monkeypatch.setattr(control.calls, "live_calls", lambda since: [
        {"key": "a", "frejun_call_id": "f1", "status": "placed"}, {"key": "b", "frejun_call_id": "f2", "status": "in_call"},
        {"key": "c", "frejun_call_id": "gone", "status": "in_call"}])
    monkeypatch.setattr(control.calls, "fail_call", lambda key, error: failed.append(key))

    def fake_hang_up(call_id):
        if call_id == "gone":
            raise RuntimeError("no such call")
        hung.append(call_id)

    monkeypatch.setattr(control, "hang_up", fake_hang_up)
    state["auto_enabled"] = True
    r = control.emergency_stop("ravi")
    assert r["emergency_stop"] and not r["running"] and r["emergency_stop_by"] == "ravi"
    assert (r["calls_ended"], r["calls_not_ended"]) == (2, 1) and hung == ["f1", "f2"]
    assert failed == ["a"]  # the connected call saves its own transcript
    assert seen[-1][1] == "emergency_stop"
    with pytest.raises(ValueError, match="emergency stop"):
        control.start("interval", 1440, "08:30", "dry", None)
    with pytest.raises(ValueError, match="emergency stop"):
        control.run_now("dry", None, "ravi")
    r = control.emergency_clear("meena")
    assert not r["emergency_stop"] and not r["running"]  # clearing does not start anything
    assert control.start("interval", 1440, "08:30", "dry", None)["running"]


def test_emergency_stop_holds_even_if_hanging_up_fails(state, monkeypatch):
    monkeypatch.setattr(control.calls, "live_calls", lambda since: [{"key": "a", "frejun_call_id": "f", "status": "placed"}])
    monkeypatch.setattr(control, "hang_up", lambda call_id: (_ for _ in ()).throw(RuntimeError("FreJun down")))
    r = control.emergency_stop("ravi")
    assert r["emergency_stop"] and r["calls_not_ended"] == 1


def test_the_scheduler_never_runs_during_an_emergency_stop(state, monkeypatch):
    monkeypatch.setattr(service, "recent_auto_runs", lambda limit=10: [])
    state.update(auto_enabled=True, schedule_type="interval")
    assert REAL_NEXT_AUTO_RUN({**state}) is not None
    assert REAL_NEXT_AUTO_RUN({**state, "emergency_stop": True}) is None


def test_a_daily_run_stopped_by_the_emergency_stop_can_run_again_today(state, monkeypatch):
    now = datetime(2026, 11, 2, 9, 0, tzinfo=TIMEZONE)
    state.update(auto_enabled=True, schedule_type="daily", daily_time="08:30")
    runs = [{"id": 1, "started_at": now - timedelta(minutes=20), "status": "stopped", "error": None}]
    monkeypatch.setattr(service, "recent_auto_runs", lambda limit=10: runs)
    assert REAL_NEXT_AUTO_RUN({**state}, now).date() == now.date()
    runs[0]["status"] = "ok"
    assert REAL_NEXT_AUTO_RUN({**state}, now).date() > now.date()


def test_start_run_refuses_during_an_emergency_stop(monkeypatch):
    monkeypatch.setattr(service.store, "emergency_on", lambda: True)
    monkeypatch.setattr(service.store, "RunLock", lambda: type("L", (), {"acquire": lambda s: True, "release": lambda s: None})())
    with pytest.raises(ValueError, match="emergency stop"):
        service.start_run("dry", "manual")
    assert not service.is_busy()  # the lock is released


def test_the_page_button_needs_a_session_and_names_who_pressed_it(state, monkeypatch):
    monkeypatch.setattr(control.calls, "live_calls", lambda since: [])
    monkeypatch.setattr(users, "current", lambda token: {"username": "ravi", "role": "scm"} if token else None)
    assert client.post("/dashboard/api/control/emergency-stop").status_code == 401
    r = client.post("/dashboard/api/control/emergency-stop", headers={"X-Session": "s"}).json()
    assert r["emergency_stop"] and r["emergency_stop_by"] == "ravi"
    assert client.post("/dashboard/api/control/emergency-clear", headers={"X-Session": "s"}).json()["emergency_stop"] is False


@pytest.fixture
def restarting(state, monkeypatch):
    done = []
    monkeypatch.setattr(store, "fresh_start", lambda at: done.append(at) or 164)
    monkeypatch.setattr(service, "start_run", lambda mode, trigger: done.append((mode, trigger)) or 7)
    monkeypatch.setattr(service.store, "RunLock", lambda: type("L", (), {"acquire": lambda s: True, "release": lambda s: None})())
    return done


def do_restart(mode="dry", confirm=None, restart="RESTART", every=1440, scope=None, codes=None):
    return control.restart("interval", every, "08:30", mode, confirm, scope, codes, None, restart, "ravi")


def test_restart_resets_every_vendor_then_runs_at_once(state, restarting):
    r = do_restart()
    assert r["running"] and r["lines_reset"] == 164 and r["run_id"] == 7
    assert restarting[-1] == ("dry", "manual") and len(restarting) == 2


def test_restart_wipes_nothing_unless_it_is_confirmed_and_valid(state, restarting):
    for kwargs, match in [({"restart": None}, "RESTART"), ({"mode": "live"}, "LIVE"), ({"every": 0}, "(?i)interval"),
                          ({"scope": "selected", "codes": []}, "at least one vendor")]:
        with pytest.raises(ValueError, match=match):
            do_restart(**kwargs)
    state["emergency_stop"] = True
    with pytest.raises(ValueError, match="emergency stop"):
        do_restart()
    assert restarting == []


def test_restart_waits_for_a_run_in_progress(state, restarting, monkeypatch):
    def busy():
        raise service.Busy("A run is already in progress")

    monkeypatch.setattr(service, "_acquire", busy)
    with pytest.raises(service.Busy):
        do_restart()
    assert restarting == []


def test_restart_needs_a_session_and_the_word_restart(state, restarting, monkeypatch):
    monkeypatch.setattr(users, "current", lambda token: {"username": "ravi", "role": "scm"} if token else None)
    body = {"every_minutes": 1440, "mode": "dry", "confirm_restart": "RESTART"}
    assert client.post("/dashboard/api/control/restart", json=body).status_code == 401
    assert client.post("/dashboard/api/control/restart", json={**body, "confirm_restart": None}, headers={"X-Session": "s"}).status_code == 400
    assert client.post("/dashboard/api/control/restart", json=body, headers={"X-Session": "s"}).status_code == 202
