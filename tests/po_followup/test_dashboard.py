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
SETTINGS = {"check_period_minutes": 1440, "pilot_mode": False, "pilot_vendor_codes": ""}


def day(n):
    """Start of the daily check n days before NOW."""
    return NOW.replace(hour=0, minute=0) - timedelta(days=n)


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


def test_tabs_follow_the_overdue_streak_and_cells_show_what_was_sent():
    d2, d1 = day(2) + timedelta(hours=8), day(1) + timedelta(hours=8)
    rows = [master("PO1", "10", 40), master("PO1", "20", 5), master("PO2", "10", 0), master("PO3", "10", 12)]
    # PO1/10 overdue 3 checks in a row (2 earlier + now), PO3/10 2 in a row, PO1/20 only now
    earlier = [(day(1), {"PO1/10/1", "PO3/10/1"}), (day(2), {"PO1/10/1"}), (day(3), set())]
    tracker = [
        tracked("PO1/10/1", 1, d1, d2.date()),  # Scenario 1 + 2 sent; streak 3 confirms Scenario 3 -> due
        tracked("PO3/10/1", 0, d1, d1.date()),  # Scenario 1 sent; streak 2 confirms Scenario 2 -> due
        tracked("PO9/10/1", 2, d2, d2.date(), "CLEARED") | {"Cleared Date": NOW.date().isoformat()},
    ]
    out = dashboard.build(rows, tracker, [sent(0, d2), sent(1, d1)], earlier, SETTINGS, NOW)

    assert [p["po"] for p in out["pos"]] == ["PO1", "PO3"]  # PO2 is not overdue; most days late first
    po1 = out["pos"][0]
    assert po1["buyer"] == "ravi" and po1["max_od"] == 40 and po1["stage"] == 2
    l10, l20 = po1["lines"]
    assert l10["streak"] == 3 and l10["stage_name"] == "Scenario 3" and l10["due"] == "03-Sep-2026"
    assert states(l10) == ["done", "done", "due"]
    assert l10["scenarios"][0]["at"] == d2.isoformat() and l10["scenarios"][1]["at"] == d1.isoformat()
    assert [h["overdue"] for h in l10["history"]] == [False, True, True, True]  # oldest first, now last
    # overdue in this check only: Scenario 1 due; Scenario 2 is confirmed by tomorrow's check if still overdue
    assert l20["streak"] == 1 and states(l20) == ["due", "upcoming", "upcoming"]
    assert l20["scenarios"][1]["at"] == day(-1).isoformat() and l20["scenarios"][2]["at"] == day(-2).isoformat()
    assert states(out["pos"][1]["lines"][0]) == ["done", "due", "upcoming"]

    s = out["summary"]
    assert (s["lines"], s["pos"], s["vendors"], s["max_od"], s["over_30"]) == (3, 2, 1, 40, 1)
    assert s["by_stage"] == [1, 1, 1, 0] and s["cleared_recent"] == 1 and s["earlier_checks"] == 3
    assert out["last_check"] == day(1).isoformat()


def test_confirmed_scenarios_wait_their_turn():
    # overdue 4 checks in a row (meeting confirmed) but only Scenario 1 went out: Scenario 2 is next, 3 queues
    earlier = [(day(n), {"PO1/10/1"}) for n in (1, 2, 3)]
    at = day(1) + timedelta(hours=8)
    out = dashboard.build([master("PO1", "10", 9)], [tracked("PO1/10/1", 0, at, at.date())], [], earlier, SETTINGS, NOW)
    line = out["pos"][0]["lines"][0]
    assert line["stage_name"] == "Management meeting" and states(line) == ["done", "due", "queued"]


def test_earlier_messages_for_other_lines_are_not_shown_as_this_lines():
    old, first = NOW - timedelta(days=30), NOW - timedelta(hours=1)
    out = dashboard.build([master("PO1", "10", 3)], [tracked("PO1/10/1", 0, first, first.date())],
                          [sent(0, old), sent(0, first, simulated=True)], [], SETTINGS, NOW)
    s1 = out["pos"][0]["lines"][0]["scenarios"][0]
    assert s1 == {"state": "done", "at": first.isoformat(), "simulated": True}


def test_meeting_reached_and_pilot_hold():
    at = day(1) + timedelta(hours=8)
    rows = [master("PO1", "10", 9), master("PO2", "10", 9, code="500001", name="OTHER")]
    earlier = [(day(n), {"PO1/10/1", "PO2/10/1"}) for n in (1, 2, 3)]
    tracker = [tracked("PO1/10/1", 3, at, at.date())]
    out = dashboard.build(rows, tracker, [], earlier, {**SETTINGS, "pilot_mode": True, "pilot_vendor_codes": "400186"}, NOW)
    by_po = {p["po"]: p["lines"][0] for p in out["pos"]}
    assert by_po["PO1"]["meeting"] and states(by_po["PO1"]) == ["done", "done", "done"]
    assert by_po["PO1"]["stage_name"] == "Management meeting"
    assert by_po["PO2"]["on_hold"] and states(by_po["PO2"]) == ["on_hold", "queued", "queued"]


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
    monkeypatch.setattr(dashboard, "overview", lambda env: {"env": env, "synced_at": None, **dashboard.build([], [], [], [], SETTINGS, NOW)})
    assert client.get("/dashboard/api/prod").status_code == 401
    assert client.get("/dashboard/api/prod", headers={"X-Access-Code": "nope"}).status_code == 401
    assert client.get("/dashboard/api/prod", headers={"X-Access-Code": "biz"}).json()["env"] == "prod"
    assert client.get("/dashboard/api/test", headers={"X-Access-Code": "adm"}).json()["env"] == "test"
    assert client.get("/dashboard/api/staging", headers={"X-Access-Code": "biz"}).status_code == 422
    # the business code does not open the admin API
    assert client.get("/admin/api/session", headers={"X-Admin-Code": "biz"}).status_code == 401


def test_refresh_with_nothing_new_answers_304(monkeypatch, ready):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "biz")
    stamp = iter(["2026-10-20T08:30:00+05:30", "2026-10-20T08:32:00+05:30", "2026-10-20T08:34:00+05:30"])
    report = {"env": "prod", "synced_at": None, **dashboard.build([], [], [], [], SETTINGS, NOW)}
    monkeypatch.setattr(dashboard, "overview", lambda env: {**report, "now": next(stamp)})
    first = client.get("/dashboard/api/prod", headers={"X-Access-Code": "biz"})
    tag = first.headers["ETag"]
    # only `now` changed: same tag, empty 304
    again = client.get("/dashboard/api/prod", headers={"X-Access-Code": "biz", "If-None-Match": tag})
    assert again.status_code == 304 and again.content == b"" and again.headers["ETag"] == tag
    # an old tag gets the full report
    stale = client.get("/dashboard/api/prod", headers={"X-Access-Code": "biz", "If-None-Match": '"old"'})
    assert stale.status_code == 200 and stale.json()["env"] == "prod"
