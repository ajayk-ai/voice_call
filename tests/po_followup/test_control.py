"""Run control: starting / stopping the automatic follow-up from the dashboard, without a database."""

import pytest
from fastapi.testclient import TestClient

from backend.po_followup.api import admin
from backend.po_followup.db import store
from backend.po_followup.services import control, users
from backend.po_followup.services import followup as service
from backend.voice_agent.app import app

client = TestClient(app)


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
    monkeypatch.setattr(store, "table_rows", lambda title, limit: [
        {"Vendor": "V1 (1)", "Status": "INVALID_MOBILE", "Error": "Invalid/Missing Mobile Number"},
        {"Vendor": "V1 (1)", "Status": "SENT", "Error": ""}])
    out = {v["code"]: v for v in control.chosen_vendors()}
    assert set(out) == {"1", "3"}
    assert out["1"]["problem"] == "Invalid/Missing Mobile Number" and out["3"]["name"] == "No overdue" and not out["3"]["problem"]
