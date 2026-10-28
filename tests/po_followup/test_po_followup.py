from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest

from backend.po_followup.config import Settings
from backend.po_followup.domain.escalation import (
    apply_phone_master, build_groups, detect_cleared, normalize_mobile, select_alerts,
)
from backend.po_followup.api.replies import extract_po_number, reply_record
from backend.po_followup.domain.overdue import overdue_now
from backend.po_followup.services.run import ZONE, run_daily

RUN = date(2026, 10, 8)
NOW = datetime(2026, 10, 8, 8, 30, tzinfo=ZONE)


def master_row(po, item="10", od="5", vendor="400186", **extra):
    row = {"PO No": po, "PO Item No": item, "Del.Sch Line": "1", "Vendor Code": vendor, "Vendor Name": "SUPER SPRINGS",
           "Material": "M1", "Mat. Desc": "Bonnet Assembly 76 HP", "Pending Qty": "3", "UOM": "EA", "OD Days\r": od,
           "vendor planner number": "9876543210", "vendor coordinator number": "9876543211",
           "vendor management number": "9876543212", "buyer number": "9876543213", "Buyer Mail ID": "ravi@bull.com"}
    row.update(extra)
    return row


def tracker_row(key, level, last_date="2026-10-07", status="OPEN"):
    return {"Line Key": key, "Escalation Level": str(level), "Last Alert Date": last_date, "Status": status,
            "First Alert Date": "2026-10-05", "Last Alert Type": "X"}


SETTINGS = Settings(
    sheet_id="x", test_mode=False, update_tracker_in_test_mode=False, pilot_mode=False,
    pilot_vendor_codes=frozenset({"400186"}), pilot_mobile="7200081289", ultramsg_base_url="u", ultramsg_token="t",
    scm_hod_name="SCM Head", scm_hod_mobile="9999999999", scm_head_email="", max_lines_per_message=15,
    send_interval_seconds=0, run_at="08:30", vendor_scope="all",
)


def test_runs_follow_up_only_the_selected_vendors_unless_scope_is_all():
    assert SETTINGS.vendor_filter is None
    assert replace(SETTINGS, vendor_scope="selected").vendor_filter == {"400186"}
    assert replace(SETTINGS, pilot_mode=True).vendor_filter == {"400186"}  # pilot always means selected only


# ---------- rules ----------

def test_only_overdue_lines_are_pending():
    groups = build_groups([master_row("1", od="5"), master_row("2", od="0"), master_row("3", od="")], [], NOW)
    assert len(groups) == 1 and groups[0]["overdueLines"] == 1 and groups[0]["poNumber"] == "1"


def test_streak_confirms_the_next_level_one_step_at_a_time():
    rows = [master_row(str(i)) for i in range(1, 6)]
    tracker = [
        tracker_row("1/10/1", 0),                    # streak 2 confirms Scenario 2 -> level 1
        tracker_row("2/10/1", 1),                    # Scenario 2 already sent; streak 2 confirms nothing more yet
        tracker_row("3/10/1", 3),                    # meeting reached -> nothing
        tracker_row("4/10/1", 2, status="CLEARED"),  # cleared earlier -> starts again at Scenario 1
        tracker_row("5/10/1", 0),                    # streak 4 confirms the meeting, but levels are never skipped
    ]
    line_streaks = {"1/10/1": 2, "2/10/1": 2, "3/10/1": 9, "4/10/1": 5, "5/10/1": 4}
    levels = {g["nextLevel"]: [l["poNumber"] for l in g["lines"]] for g in build_groups(rows, tracker, NOW, streaks=line_streaks)}
    assert levels == {1: ["1", "5"], 0: ["4"]}


def test_unconfirmed_line_waits_for_the_next_check():
    tracker = [tracker_row("1/10/1", 0)]
    assert build_groups([master_row("1")], tracker, NOW, streaks={"1/10/1": 1}) == []
    assert build_groups([master_row("1")], tracker, NOW) == []  # no history: overdue in this check only


def test_group_summary_and_due_date():
    g = build_groups([master_row("4500567101", od="16"), master_row("4500568052", od="12")], [], NOW)[0]
    assert g["daysOverdue"] == 16 and g["poCount"] == 2
    assert g["lineSummary"].splitlines()[0] == (
        "1. PO 4500567101/10 | Bonnet Assembly 76 HP | Due 22-Sep-2026 | Pend 3 EA | 16d late")
    assert g["buyerName"] == "ravi"


def test_detect_cleared_and_empty_master_safety():
    tracker = [tracker_row("1/10/1", 0), tracker_row("2/10/1", 1), tracker_row("3/10/1", 1, status="CLEARED")]
    cleared = detect_cleared([master_row("1"), master_row("2", od="0")], tracker, RUN, "ts")
    assert [c["Line Key"] for c in cleared] == ["2/10/1"]
    assert detect_cleared([], tracker, RUN, "ts") == []


def test_phone_master_overrides_and_falls_back():
    g = build_groups([master_row("1")], [], NOW)
    out = apply_phone_master(g, [{"Vendor Code": "400186", "Scenario 1 No": "", "Scenario 2 No": "9000000002"}])[0]
    assert out["scenario1Raw"] == "9876543210,9876543211"  # blank -> Master planner + coordinator
    assert out["scenario2Raw"] == "9000000002"
    assert out["scenario3Raw"] == "9000000002"  # blank -> Scenario 2
    assert out["phoneSource"] == "Phone_Master"


def test_phone_master_live_sheet_headers():
    g = build_groups([master_row("1")], [], NOW)
    row = {"Vendor Code": "400186", "Scenario 1 mobile No": "9111111111", "Scenario 2 mobile No": "",
           "Scenario 3 mobile No": "9333333333"}
    out = apply_phone_master(g, [row])[0]
    assert (out["scenario1Raw"], out["scenario2Raw"], out["scenario3Raw"]) == ("9111111111", "9876543212", "9333333333")


def test_select_alerts_dedupe_and_pilot():
    g = apply_phone_master(build_groups([master_row("1"), master_row("9", vendor="111")], [], NOW), [])
    sent_today = {"400186|L0|VENDOR_ALERT_DAY0|2026-10-08"}
    assert [x["vendorCode"] for x in select_alerts(g, sent_today, False, frozenset())] == ["111"]
    assert [x["vendorCode"] for x in select_alerts(g, set(), True, frozenset({"400186"}))] == ["400186"]


@pytest.mark.parametrize("raw,expected", [
    ("09876543210", "919876543210"), ("+91 98765 43210", "919876543210"), ("98765-43210 / 9000000000", "919876543210"),
    ("", ""), ("12345", "12345"),
])
def test_normalize_mobile(raw, expected):
    assert normalize_mobile(raw, False, "") == expected


def test_pilot_mode_redirects_every_message():
    assert normalize_mobile("9876543210", True, "7200081289") == "917200081289"


# ---------- full run with fakes ----------

class FakeSheet:
    def __init__(self, tabs, overdue_days=0):
        self.tabs = tabs
        self.appended: dict[str, list] = {}
        self.upserted: dict[str, list] = {}
        # Earlier daily checks: Master's overdue lines were overdue on each of the `overdue_days` days before NOW
        midnight = NOW.replace(hour=0, minute=0)
        self.checks = [(midnight - timedelta(days=d), None) for d in range(1, overdue_days + 1)]
        self.recorded = None

    def record_check(self, period, lines, master_rows):
        self.checks = [c for c in self.checks if c[0] != period] + [(period, set(lines))]
        self.recorded = (period, set(lines), master_rows)

    def recent_checks(self, upto, limit=7):
        everything = set(overdue_now(self.tabs.get("Master", [])))
        out = sorted((c for c in self.checks if c[0] <= upto), key=lambda c: c[0], reverse=True)[:limit]
        return [(p, everything if keys is None else keys) for p, keys in out]

    def read_records(self, title):
        if isinstance(self.tabs.get(title), Exception):
            raise self.tabs[title]
        return self.tabs.get(title, [])

    def append_rows(self, title, rows):
        self.appended.setdefault(title, []).extend(rows)

    def upsert_rows(self, title, rows, key):
        self.upserted.setdefault(title, []).extend(rows)


class FakeSender:
    def __init__(self, ok=True):
        self.ok = ok
        self.sent = []

    def send_text(self, mobile, body):
        self.sent.append((mobile, body))
        return {"sent": "true", "message": "ok", "id": 1} if self.ok else {"sent": "false", "error": "bad token=abc"}


def run(sheet, sender=None, **overrides):
    return run_daily(replace(SETTINGS, **overrides), sheet, sender, now=NOW, sleep=lambda _: None)


def test_live_day0_sends_logs_and_tracks():
    sheet = FakeSheet({"Master": [master_row("1")]})
    sender = FakeSender()
    run(sheet, sender)
    assert [m for m, _ in sender.sent] == ["919876543210", "919876543211"]  # planner + coordinator
    logs = sheet.appended["Alert_Log"]
    assert {r["Status"] for r in logs} == {"SENT"}
    assert logs[0]["Dedupe Key"] == "400186|L0|VENDOR_ALERT_DAY0|2026-10-08"
    assert sheet.upserted["Line_Tracker"][0]["Escalation Level"] == "0"


def test_dry_run_sends_nothing_and_keeps_tracker():
    sheet = FakeSheet({"Master": [master_row("1")]})
    sender = FakeSender()
    run(sheet, sender, test_mode=True)
    assert sender.sent == []
    assert {r["Status"] for r in sheet.appended["Alert_Log"]} == {"DRY_RUN"}
    assert {r["Dedupe Key"] for r in sheet.appended["Alert_Log"]} == {""}
    assert "Line_Tracker" not in sheet.upserted


def test_preview_writes_nothing():
    sheet = FakeSheet({"Master": [master_row("1")]})
    report = run_daily(SETTINGS, sheet, None, preview=True, now=NOW)
    assert sheet.appended == {} and sheet.upserted == {}
    assert len(report.alerts) == 2 and all(a["status"] == "DRY_RUN" for a in report.alerts)


def test_failed_send_is_logged_with_masked_token_and_not_tracked():
    sheet = FakeSheet({"Master": [master_row("1")]})
    run(sheet, FakeSender(ok=False))
    logs = sheet.appended["Alert_Log"]
    assert {r["Status"] for r in logs} == {"FAILED"} and "token=***" in logs[0]["Error"]
    assert "Line_Tracker" not in sheet.upserted


def test_invalid_number_is_logged():
    row = master_row("1", **{"vendor planner number": "#N/A", "vendor coordinator number": ""})
    sheet = FakeSheet({"Master": [row]})
    run(sheet, FakeSender())
    assert sheet.appended["Alert_Log"][0]["Status"] == "INVALID_MOBILE"


def test_run_records_the_check_and_escalates_after_consecutive_overdue_days():
    sheet = FakeSheet({"Master": [master_row("1"), master_row("2", od="0")]})
    report = run(sheet, FakeSender())
    assert sheet.recorded == (datetime(2026, 10, 8, tzinfo=ZONE), {"1/10/1"}, 2)
    assert report.check["by_scenario"] == [1, 0, 0, 0] and report.alerts[0]["level"] == 0
    sheet.tabs["Line_Tracker"] = list(sheet.upserted["Line_Tracker"])
    # same day again: the streak is still 1, so nothing new goes out
    assert run(sheet, FakeSender()).alerts == []
    # next day, still overdue: Scenario 2 to vendor management
    sender = FakeSender()
    report = run_daily(SETTINGS, sheet, sender, now=NOW + timedelta(days=1), sleep=lambda _: None)
    assert report.check["by_scenario"] == [0, 1, 0, 0] and report.check["earlier_checks"] == 1
    assert [m for m, _ in sender.sent] == ["919876543212"]


def test_a_day_not_overdue_restarts_the_streak():
    sheet = FakeSheet({"Master": [master_row("1")], "Line_Tracker": [tracker_row("1/10/1", 0)]})
    sheet.checks = [(NOW.replace(hour=0, minute=0) - timedelta(days=1), set())]  # not overdue yesterday
    assert run(sheet, FakeSender()).alerts == []  # streak 1: Scenario 2 not confirmed


def test_preview_does_not_record_the_check():
    sheet = FakeSheet({"Master": [master_row("1")]})
    run_daily(SETTINGS, sheet, None, preview=True, now=NOW)
    assert sheet.recorded is None


def test_day2_creates_call_task_and_messages_buyer():
    sheet = FakeSheet({"Master": [master_row("1")], "Line_Tracker": [tracker_row("1/10/1", 1)]}, overdue_days=2)
    sender = FakeSender()
    run(sheet, sender)
    assert sheet.appended["Call_Tasks"][0]["Task ID"] == "400186-CALL-2026-10-08"
    assert [m for m, _ in sender.sent] == ["919876543213"]  # buyer


def test_day3_creates_meeting_task_and_notifies_buyer_and_hod():
    sheet = FakeSheet({"Master": [master_row("1")], "Line_Tracker": [tracker_row("1/10/1", 2)]}, overdue_days=3)
    sender = FakeSender()
    run(sheet, sender)
    meeting = sheet.appended["Meeting_Tasks"][0]
    assert meeting["Proposed Date"] == "2026-10-09 11:00"
    assert [m for m, _ in sender.sent] == ["919876543213", "919999999999"]
    assert sheet.upserted["Line_Tracker"][0]["Status"] == "MEETING_SCHEDULED"


class FakeCaller:
    def __init__(self, fail=False):
        self.fail = fail
        self.called = []

    def call(self, mobile, script):
        self.called.append(mobile)
        self.script = script
        if self.fail:
            raise RuntimeError("FreJun down")
        return "call-1"


class FakeMeet:
    def __init__(self):
        self.invites = []

    def schedule(self, title, description, start, attendees):
        self.invites.append((start, attendees))
        return {"event_link": "https://calendar/e", "meet_link": "https://meet.google.com/abc-defg-hij"}


def test_day2_agent_calls_scenario3_number_in_pilot():
    sheet = FakeSheet({"Master": [master_row("1")], "Line_Tracker": [tracker_row("1/10/1", 1)]}, overdue_days=2)
    caller = FakeCaller()
    run_daily(replace(SETTINGS, pilot_mode=True), sheet, FakeSender(), now=NOW, sleep=lambda _: None, caller=caller)
    assert caller.called == ["917200081289"]
    from backend.voice_agent import call_context

    script = call_context.decode(caller.script)
    assert "SUPER SPRINGS" in script["greeting"]
    assert "PO 1, line 10 - Bonnet Assembly 76 HP" in script["prompt"]
    assert "4 0 0 1 8 6" in script["prompt"]
    call_log = next(r for r in sheet.appended["Alert_Log"] if r["Alert Type"] == "AGENT_CALL_DAY2")
    assert call_log["Status"] == "CALL_PLACED" and "call-1" in call_log["UltraMsg Response"]


def test_day2_agent_call_rings_the_pilot_call_number_while_whatsapp_goes_to_the_pilot_mobile():
    sheet = FakeSheet({"Master": [master_row("1")], "Line_Tracker": [tracker_row("1/10/1", 1)]}, overdue_days=2)
    sender, caller = FakeSender(), FakeCaller()
    run_daily(replace(SETTINGS, pilot_mode=True, pilot_call_mobile="9123456789"), sheet, sender, now=NOW,
              sleep=lambda _: None, caller=caller)
    assert caller.called == ["919123456789"]
    assert {m for m, _ in sender.sent} == {"917200081289"}


def test_day2_failed_agent_call_is_logged_and_whatsapp_still_goes():
    sheet = FakeSheet({"Master": [master_row("1")], "Line_Tracker": [tracker_row("1/10/1", 1)]}, overdue_days=2)
    sender = FakeSender()
    run_daily(SETTINGS, sheet, sender, now=NOW, sleep=lambda _: None, caller=FakeCaller(fail=True))
    assert {r["Status"] for r in sheet.appended["Alert_Log"]} == {"CALL_FAILED", "SENT"}
    assert [m for m, _ in sender.sent] == ["919876543213"]


def test_dry_run_places_no_agent_call_or_meet():
    sheet = FakeSheet({"Master": [master_row("1")], "Line_Tracker": [tracker_row("1/10/1", 1)]}, overdue_days=2)
    caller, meet = FakeCaller(), FakeMeet()
    run_daily(replace(SETTINGS, test_mode=True), sheet, None, now=NOW, caller=caller, meet=meet)
    assert caller.called == [] and meet.invites == []


def test_day3_meet_invite_goes_to_pilot_email_and_link_is_shared():
    sheet = FakeSheet({"Master": [master_row("1")], "Line_Tracker": [tracker_row("1/10/1", 2)]}, overdue_days=3)
    sender, meet = FakeSender(), FakeMeet()
    run_daily(replace(SETTINGS, pilot_mode=True, pilot_email="ajay.k@bullmachine.com"), sheet, sender, now=NOW,
              sleep=lambda _: None, meet=meet)
    assert meet.invites == [(datetime(2026, 10, 9, 11, 0, tzinfo=ZONE), ["ajay.k@bullmachine.com"])]
    assert sheet.appended["Meeting_Tasks"][0]["Meeting Status"].startswith("SCHEDULED – https://meet.google.com/")
    assert all("https://meet.google.com/abc-defg-hij" in body for _, body in sender.sent)


def test_master_read_failure_is_logged():
    sheet = FakeSheet({"Master": RuntimeError("quota")})
    run(sheet, FakeSender())
    assert sheet.appended["Alert_Log"][0]["Status"] == "SHEETS_READ_FAILED"


# ---------- vendor replies ----------

def test_po_number_only_when_explicit():
    assert extract_po_number("PO No: 4500567101 will dispatch Friday") == "4500567101"
    assert extract_po_number("will dispatch friday") == ""
    assert extract_po_number("PO pending") == ""


def test_reply_record_filters_non_vendor_messages():
    msg = {"event_type": "message_received", "data": {"from": "919876543210@c.us", "body": "PO 4500567101 sent", "id": "m1"}}
    rec = reply_record(msg)
    assert rec["Sender Mobile"] == "919876543210" and rec["PO Number"] == "4500567101"
    assert reply_record({**msg, "data": {**msg["data"], "from": "123@g.us"}}) is None
    assert reply_record({**msg, "data": {**msg["data"], "fromMe": True}}) is None


class Killed(BaseException):
    """Stands in for the process dying mid-run (not caught by the run's error handling)."""


def test_alert_log_is_written_per_vendor_so_a_crash_keeps_what_was_sent():
    sheet = FakeSheet({"Master": [master_row("1"), master_row("9", vendor="111")]})

    class DiesOnSecondVendor(FakeSender):
        def send_text(self, mobile, body):
            if "(111)" in body:
                raise Killed()
            return super().send_text(mobile, body)

    with pytest.raises(Killed):
        run(sheet, DiesOnSecondVendor())
    logs = sheet.appended["Alert_Log"]
    assert {r["Status"] for r in logs} == {"SENT"} and all("400186" in r["Dedupe Key"] for r in logs)


def test_agent_call_refuses_a_stream_server_running_old_code(monkeypatch):
    import httpx

    from backend.po_followup.integrations.agent_call import AgentCallNotConfigured, check_stream_server
    from backend.voice_agent import call_context

    monkeypatch.setattr(httpx, "get", lambda url, timeout: httpx.Response(200, headers={"X-Call-Script": call_context.VERSION}))
    check_stream_server("https://new")
    monkeypatch.setattr(httpx, "get", lambda url, timeout: httpx.Response(200))
    with pytest.raises(AgentCallNotConfigured, match="older code"):
        check_stream_server("https://old")
