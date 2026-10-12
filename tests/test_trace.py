"""Vendor trace: Scenario 1 -> 2 -> 3 -> meeting state, without a database."""

from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from po_followup import admin, trace
from po_followup.run import ZONE
from voice_agent.app import app

NOW = datetime(2026, 10, 9, 10, 0, tzinfo=ZONE)
IV = [1, 1, 1]


def tracker(level, at, status="OPEN", key="PO1/10/1"):
    return {"Line Key": key, "Vendor Code": "400186", "Escalation Level": str(level), "Status": status,
            "Last Alert At": at.strftime("%Y-%m-%d %H:%M:%S")}


def log(level, at, status="SENT"):
    return {"Timestamp": at.strftime("%Y-%m-%d %H:%M:%S"), "Escalation Level": str(level), "Status": status,
            "Recipient Role": "r", "Message": "m"}


def test_not_started_scenario1_due_now():
    level, last = trace.position([])
    steps = trace.journey(level, last, [], IV, NOW)
    assert [s["state"] for s in steps] == ["next", "later", "later", "later"]
    assert steps[0]["due_now"] and steps[0]["wait_minutes"] is None and steps[1]["wait_minutes"] == 1


def test_scenario2_waits_for_interval():
    sent = NOW - timedelta(seconds=20)
    level, last = trace.position([tracker(0, sent)])
    steps = trace.journey(level, last, [log(0, sent)], IV, NOW)
    assert [s["state"] for s in steps] == ["done", "next", "later", "later"]
    assert steps[0]["done_at"] == sent.isoformat() and steps[0]["messages"][0]["status"] == "SENT"
    assert not steps[1]["due_now"] and steps[1]["due_at"] == (sent + timedelta(minutes=1)).isoformat()
    assert trace.journey(level, last, [], IV, NOW + timedelta(minutes=1))[1]["due_now"]


def test_position_uses_highest_open_level_and_ignores_cleared():
    rows = [tracker(1, NOW - timedelta(minutes=3), key="a"), tracker(2, NOW - timedelta(minutes=1), key="b"),
            tracker(3, NOW, status="CLEARED", key="c")]
    assert trace.position(rows) == (2, (NOW - timedelta(minutes=1)).replace(microsecond=0))


def test_all_done_and_failed_messages_stay_on_their_step():
    steps = trace.journey(3, NOW, [log(1, NOW, "FAILED"), log(1, NOW, "SENT")], IV, NOW)
    assert all(s["state"] == "done" for s in steps)
    assert [m["status"] for m in steps[1]["messages"]] == ["SENT", "FAILED"]  # newest first
    assert "All steps done" in trace.next_step_note(steps, 4, True, {"pilot_mode": True}, None)


def test_next_note_explains_pilot_skip_and_no_lines():
    steps = trace.journey(None, None, [], IV, NOW)
    assert "not in the pilot" in trace.next_step_note(steps, 3, False, {"pilot_mode": True}, None)
    assert "No pending lines" in trace.next_step_note(steps, 0, True, {"pilot_mode": True}, None)
    assert "due now" in trace.next_step_note(steps, 3, True, {"pilot_mode": True, "auto_enabled": True}, NOW)


def test_index_vendors_and_contacts_fallback():
    master = [
        {"PO No": "1", "PO Item No": "10", "Vendor Code": "400186", "Vendor Name": "Super", "OD Days": "5",
         "vendor planner number": "9000000001", "vendor management number": "9000000002"},
        {"PO No": "2", "Vendor Code": "400186", "OD Days": "0"},
        {"PO No": "3", "Vendor Code": "500", "Vendor Name": "Other", "OD Days": "-2"},
    ]
    v = trace.index_vendors(master)
    assert list(v["400186"]["lines"]) == ["1/10/1"] and v["500"]["lines"] == {}
    c = trace.contacts(v["400186"], [{"Vendor Code": "400186", "Scenario 2 No": "9111111111"}], "9222222222")
    assert (c["scenario1"], c["scenario2"], c["scenario3"]) == ("9000000001", "9111111111", "9111111111")
    assert c["source"] == "Phone_Master" and c["scm_hod_mobile"] == "9222222222"


def test_vendor_test_request_validation(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "adm")
    monkeypatch.setattr(admin, "_ready", True)
    client = TestClient(app)
    h = {"X-Admin-Code": "adm"}
    assert client.post("/admin/api/test/vendor-test", json={"vendor_code": "1", "mobile": "9876543210", "minutes": 0},
                       headers=h).status_code == 422
    assert client.post("/admin/api/test/vendor-test", json={"vendor_code": "1", "mobile": "123"},
                       headers=h).status_code == 422
