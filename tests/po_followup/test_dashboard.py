"""Business dashboard: overdue PO lines and their Scenario 1 / 2 / 3 status, without a database."""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from backend.po_followup.api import admin
from backend.po_followup.services import dashboard
from backend.po_followup.services.run import ZONE
from backend.voice_agent.app import app

client = TestClient(app)
NOW = datetime(2026, 10, 13, 10, 0, tzinfo=ZONE)
DAY = 1440
SETTINGS = {"intervals_minutes": [DAY, DAY, DAY], "pilot_mode": False, "pilot_vendor_codes": ""}


def master(po, item, od, code="400186", name="ACME"):
    return {"PO No": po, "PO Item No": item, "Del.Sch Line": "1", "Vendor Code": code, "Vendor Name": name,
            "Material": "M" + item, "Mat. Desc": "Plate " + item, "Pending Qty": "10", "UOM": "NOS", "OD Days": str(od),
            "Buyer Mail ID": "ravi@example.com"}


def tracked(key, level, at, first, status="OPEN", code="400186"):
    return {"Line Key": key, "Vendor Code": code, "Escalation Level": str(level), "Status": status,
            "Last Alert At": at.strftime("%Y-%m-%d %H:%M:%S"), "First Alert Date": first.isoformat()}


def sent(level, at, code="400186", simulated=False):
    return {"vendor": f"ACME ({code})", "level": str(level), "alert_date": at.date().isoformat(),
            "first_at": at.strftime("%Y-%m-%d %H:%M:%S"), "simulated": simulated}


def states(line):
    return [s["state"] for s in line["scenarios"]]


def test_lines_grouped_by_po_with_scenario_status():
    d0, d1 = NOW - timedelta(days=2), NOW - timedelta(hours=3)
    rows = [master("PO1", "10", 40), master("PO1", "20", 5), master("PO2", "10", 0), master("PO3", "10", 12)]
    tracker = [
        tracked("PO1/10/1", 1, d1, d0.date()),             # Scenario 1 + 2 sent, Scenario 3 waits for its interval
        tracked("PO3/10/1", 0, d0, d0.date()),             # Scenario 1 sent two days ago: Scenario 2 due now
        tracked("PO9/10/1", 2, d0, d0.date(), "CLEARED") | {"Cleared Date": NOW.date().isoformat()},
    ]
    out = dashboard.build(rows, tracker, [sent(0, d0), sent(1, d1)], SETTINGS, NOW)

    assert [p["po"] for p in out["pos"]] == ["PO1", "PO3"]  # PO2 is not overdue; most days late first
    po1 = out["pos"][0]
    assert po1["buyer"] == "ravi" and po1["max_od"] == 40 and po1["stage"] == 2
    l10, l20 = po1["lines"]
    assert states(l10) == ["done", "done", "scheduled"]
    assert l10["scenarios"][0]["at"] == d0.isoformat() and l10["scenarios"][1]["at"] == d1.isoformat()
    assert l10["scenarios"][2]["at"] == (d1 + timedelta(days=1)).isoformat()
    assert l10["stage_name"] == "With vendor management" and l10["due"] == "03-Sep-2026"
    assert states(l20) == ["due", "upcoming", "upcoming"] and l20["stage_name"] == "Not yet notified"
    assert states(out["pos"][1]["lines"][0]) == ["done", "due", "upcoming"]

    s = out["summary"]
    assert (s["lines"], s["pos"], s["vendors"], s["max_od"], s["over_30"]) == (3, 2, 1, 40, 1)
    assert s["by_stage"] == [1, 1, 1, 0, 0] and s["cleared_recent"] == 1


def test_earlier_messages_for_other_lines_are_not_shown_as_this_lines():
    old, first = NOW - timedelta(days=30), NOW - timedelta(hours=1)
    out = dashboard.build([master("PO1", "10", 3)], [tracked("PO1/10/1", 0, first, first.date())],
                          [sent(0, old), sent(0, first, simulated=True)], SETTINGS, NOW)
    s1 = out["pos"][0]["lines"][0]["scenarios"][0]
    assert s1 == {"state": "done", "at": first.isoformat(), "simulated": True}


def test_meeting_reached_and_pilot_hold():
    at = NOW - timedelta(days=1)
    rows = [master("PO1", "10", 9), master("PO2", "10", 9, code="500001", name="OTHER")]
    tracker = [tracked("PO1/10/1", 3, at, at.date())]
    out = dashboard.build(rows, tracker, [], {**SETTINGS, "pilot_mode": True, "pilot_vendor_codes": "400186"}, NOW)
    by_po = {p["po"]: p["lines"][0] for p in out["pos"]}
    assert by_po["PO1"]["meeting"] and states(by_po["PO1"]) == ["done", "done", "done"]
    assert by_po["PO1"]["stage_name"] == "Management meeting"
    assert by_po["PO2"]["on_hold"] and states(by_po["PO2"]) == ["on_hold", "upcoming", "upcoming"]


@pytest.fixture
def ready(monkeypatch):
    monkeypatch.setattr(admin, "_ready", True)


def test_page_and_access(monkeypatch, ready):
    assert "Overdue Purchase Orders" in client.get("/dashboard").text
    css = client.get("/assets/ui.css")  # shared fonts + top bar, linked by every page
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css") and ".gnav" in css.text
    monkeypatch.setenv("DASHBOARD_PASSWORD", "biz")
    monkeypatch.setenv("ADMIN_PASSWORD", "adm")
    # the response is validated against models.dashboard.DashboardData, so return a real (empty) report
    monkeypatch.setattr(dashboard, "overview", lambda env: {"env": env, "synced_at": None, **dashboard.build([], [], [], SETTINGS, NOW)})
    assert client.get("/dashboard/api/prod").status_code == 401
    assert client.get("/dashboard/api/prod", headers={"X-Access-Code": "nope"}).status_code == 401
    assert client.get("/dashboard/api/prod", headers={"X-Access-Code": "biz"}).json()["env"] == "prod"
    assert client.get("/dashboard/api/test", headers={"X-Access-Code": "adm"}).json()["env"] == "test"
    assert client.get("/dashboard/api/staging", headers={"X-Access-Code": "biz"}).status_code == 422
    # the business code does not open the admin API
    assert client.get("/admin/api/session", headers={"X-Admin-Code": "biz"}).status_code == 401
