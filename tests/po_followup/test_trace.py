"""Vendor trace: Scenario 1 -> 2 -> 3 -> meeting state, without a database."""

from datetime import datetime, timedelta


from backend.po_followup.services import trace
from backend.core.env import TIMEZONE

NOW = datetime(2026, 10, 9, 10, 0, tzinfo=TIMEZONE)
PERIOD = 1  # check period in minutes (test environment style)


def tracker(level, at, status="OPEN", key="PO1/10/1"):
    return {"Line Key": key, "Vendor Code": "400186", "Escalation Level": str(level), "Status": status,
            "Last Alert At": at.strftime("%Y-%m-%d %H:%M:%S")}


def log(level, at, status="SENT"):
    return {"Timestamp": at.strftime("%Y-%m-%d %H:%M:%S"), "Escalation Level": str(level), "Status": status,
            "Recipient Role": "r", "Message": "m"}


def test_not_started_scenario1_due_now():
    level, last = trace.position([])
    steps = trace.journey(level, last, [], PERIOD, NOW)
    assert [s["state"] for s in steps] == ["next", "later", "later", "later"]
    assert steps[0]["due_now"] and steps[0]["wait_minutes"] is None and steps[1]["wait_minutes"] == 1


def test_scenario2_waits_for_the_next_check():
    sent = NOW + timedelta(seconds=20)
    level, last = trace.position([tracker(0, sent)])
    steps = trace.journey(level, last, [log(0, sent)], PERIOD, sent)
    assert [s["state"] for s in steps] == ["done", "next", "later", "later"]
    assert steps[0]["done_at"] == sent.isoformat() and steps[0]["messages"][0]["status"] == "SENT"
    # due from the start of the next check period
    assert not steps[1]["due_now"] and steps[1]["due_at"] == (NOW + timedelta(minutes=1)).isoformat()
    assert trace.journey(level, last, [], PERIOD, NOW + timedelta(minutes=1))[1]["due_now"]


def test_position_uses_highest_open_level_and_ignores_cleared():
    rows = [tracker(1, NOW - timedelta(minutes=3), key="a"), tracker(2, NOW - timedelta(minutes=1), key="b"),
            tracker(3, NOW, status="CLEARED", key="c")]
    assert trace.position(rows) == (2, (NOW - timedelta(minutes=1)).replace(microsecond=0))


def test_all_done_and_failed_messages_stay_on_their_step():
    steps = trace.journey(3, NOW, [log(1, NOW, "FAILED"), log(1, NOW, "SENT")], PERIOD, NOW)
    assert all(s["state"] == "done" for s in steps)
    assert [m["status"] for m in steps[1]["messages"]] == ["SENT", "FAILED"]  # newest first
    assert "All steps done" in trace.next_step_note(steps, 4, True, {"vendor_scope": "selected"}, None)


def test_next_note_explains_unchosen_vendor_and_no_lines():
    steps = trace.journey(None, None, [], PERIOD, NOW)
    assert "not selected" in trace.next_step_note(steps, 3, False, {"vendor_scope": "selected"}, None)
    assert "not selected" in trace.next_step_note(steps, 3, False, {"vendor_scope": "selected"}, None)
    assert "not selected" not in trace.next_step_note(steps, 3, False, {"vendor_scope": "all"}, None)
    assert "No pending lines" in trace.next_step_note(steps, 0, True, {"vendor_scope": "selected"}, None)
    assert "due now" in trace.next_step_note(steps, 3, True, {"vendor_scope": "selected", "auto_enabled": True}, NOW)


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


def test_a_vendor_trace_opens_with_the_real_settings(monkeypatch):
    """The whole trace with default settings, so it cannot depend on a setting that no longer exists."""
    master = [{"Vendor Code": "100019", "Vendor Name": "Bull", "PO No": "1", "PO Item No": "10", "Del.Sch Line": "1", "OD Days": "5"}]
    monkeypatch.setattr(trace.store, "get_settings", lambda: {})
    monkeypatch.setattr(trace.store, "vendor_rows", lambda title, code: [])
    monkeypatch.setattr(trace, "source_records", lambda title: master if title == "Master" else [])
    monkeypatch.setattr(trace.store, "PgStore", lambda: type("S", (), {"read_records": lambda self, title: []})())
    monkeypatch.setattr(trace.service, "recent_auto_runs", lambda limit=10: [])
    t = trace.vendor_trace("100019")
    assert t["pending_lines"] == 1 and t["steps"][0]["state"] == "next"


def test_a_vendor_in_several_purchasing_groups_counts_lines_per_group():
    master = [
        {"PO No": "1", "PO Item No": "10", "Vendor Code": "7", "OD Days": "5", "Purchasing Group": "256", "Buyer Mail ID": "ravi@bull.com"},
        {"PO No": "1", "PO Item No": "20", "Vendor Code": "7", "OD Days": "9", "Purchasing Group": "256", "Buyer Mail ID": "ravi@bull.com"},
        {"PO No": "2", "PO Item No": "10", "Vendor Code": "7", "OD Days": "3", "Purchasing Group": "210", "Buyer Mail ID": "#N/A"},
    ]
    g = trace.line_groups(trace.index_vendors(master)["7"])
    assert g["by_pg"] == {"256": {"lines": 2, "max_od": 9, "pos": 1}, "210": {"lines": 1, "max_od": 3, "pos": 1}}
    assert g["buyers"] == ["ravi"] and g["po_count"] == 2


def test_cycle_day_and_restart():
    start = NOW.date() - timedelta(days=2)
    c = trace.cycle_info(start, None, 7, NOW.date())
    assert c["cycle_day"] == 3 and c["restart_on"] is None  # not known before Scenario 4
    assert trace.cycle_info(start, NOW.date(), 7, NOW.date())["restart_on"] == (NOW.date() + timedelta(days=7)).isoformat()
    assert trace.cycle_info(None, None, 7, NOW.date())["cycle_day"] is None


def test_history_before_a_restart_is_left_out():
    rows = [{"Timestamp": "2026-11-02 09:00:00"}, {"Timestamp": "2026-11-02 11:00:00"}]
    assert trace.since_restart(rows, "2026-11-02 10:00:00") == rows[1:] and trace.since_restart(rows, "") == rows


def test_the_timeline_shows_only_the_current_cycle():
    old = log(1, NOW, "FAILED")
    rows = [old, {"Status": "CYCLE_RESET", "Escalation Level": "-"}, log(0, NOW)]
    assert trace.current_cycle(rows) == [rows[2]] and trace.current_cycle([old]) == [old]


def test_the_note_says_when_a_finished_vendor_starts_again():
    steps = trace.journey(3, NOW, [], PERIOD, NOW)
    note = trace.next_step_note(steps, 4, True, {"vendor_scope": "selected"}, None, NOW.date())
    assert "09-Oct-2026" in note and "start again from Scenario 1" in note
