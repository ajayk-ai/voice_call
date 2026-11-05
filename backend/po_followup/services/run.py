"""One daily run of the PO follow-up workflow (n8n: '01 Daily PO Trigger' through '09 Append Alert Log')."""

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from backend.core.env import TIMEZONE
from backend.core.tracing import mask_phones, traceable

from backend.po_followup.config import Settings
from backend.po_followup.domain.escalation import (
    apply_phone_master, build_groups, cycle_resets, cycle_starts, detect_cleared, is_valid_mobile, normalize_mobile,
    parse_date, select_alerts, txt,
)
from backend.po_followup.integrations.agent_call import call_script, script_lines
from backend.po_followup.domain.messages import call_task_row, log_row, meeting_task_row, message_for, recipients_for, tracker_rows
from backend.po_followup.domain.overdue import LEVELS, confirmed_level, overdue_now, period_start, streaks
from backend.po_followup.integrations.whatsapp import mask_token
from backend.po_followup.services import calls

log = logging.getLogger("po_followup")

DRY_RUN_RESPONSE = {"sent": "true", "message": "DRY RUN – WhatsApp NOT sent", "id": "DRY-RUN"}


@dataclass
class RunContext:
    now: datetime
    triggered_by: str

    @property
    def run_date(self):
        return self.now.date()

    @property
    def run_iso(self) -> str:
        return self.now.strftime("%Y-%m-%d")

    @property
    def run_ts(self) -> str:
        return self.now.strftime("%Y-%m-%d %H:%M:%S")

    def now_text(self) -> str:
        return datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class RunReport:
    run_date: str
    alerts: list[dict] = field(default_factory=list)  # one entry per recipient message
    cleared_lines: int = 0
    cycle_resets: int = 0  # vendors sent back to Scenario 1 by this run
    vendor_groups: int = 0
    sheet_writes: list[str] = field(default_factory=list)
    stopped: bool = False  # the emergency stop ended this run early
    log_rows: list[dict] = field(default_factory=list)
    check: dict = field(default_factory=dict)  # this run's overdue check: period, lines per confirmed scenario


class DailyRun:
    def __init__(self, settings: Settings, sheet, sender, ctx: RunContext, sleep=time.sleep,
                 caller=None, meet=None, should_stop=None):
        self.s = settings
        self.sheet = sheet
        self.sender = sender
        self.caller = caller  # Scenario 3 agent call (agent_call.AgentCaller); None = no call
        self.meet = meet  # Day 3 Google Meet invite (meet.MeetScheduler); None = task row only
        self.should_stop = should_stop  # asked before every send; True ends the run there
        self.ctx = ctx
        self.dry = settings.dry_run
        self.sleep = sleep
        self.report = RunReport(ctx.run_iso)
        self.logs: list[dict] = []  # Alert_Log rows not written yet
        self.cycle_starts: dict = {}  # vendor code -> the day its current cycle began
        self._sent_any_real = False

    # ----- reads and writes -----

    @traceable(name="sheet_write", run_type="tool",
               process_inputs=lambda i: {"op": i["op"], "tab": i["title"], "rows": len(i["rows"])})
    def _write(self, op: str, title: str, rows: list[dict], **kwargs) -> None:
        self.report.sheet_writes.append(f"{op} {title}: {len(rows)} row(s)")
        getattr(self.sheet, op)(title, rows, **kwargs)

    def _read(self, title: str, required: bool = True) -> list[dict]:
        try:
            return self.sheet.read_records(title)
        except Exception as e:
            if required:
                raise
            log.warning("Could not read %s, continuing without it: %s", title, e)
            return []

    # ----- the run -----

    def execute(self) -> RunReport:
        try:
            self._execute()
        except Exception as e:  # '10 Error Handler'
            log.exception("PO follow-up run failed")
            self.logs.append(log_row(
                self.ctx, **{"Alert Type": "WORKFLOW_ERROR", "Message": "PO follow-up run failed",
                             "Status": "WORKFLOW_ERROR", "Error": mask_token(str(e))[:500],
                             "Retry Status": "CHECK_LOGS"},
            ))
            self._flush_logs()
            raise
        self._flush_logs()
        return self.report

    def _execute(self) -> None:
        ctx, s = self.ctx, self.s
        today_log = [r for r in self._read("Alert_Log", required=False)
                     if txt(r.get("Alert Date")) == ctx.run_iso and txt(r.get("Timestamp")) >= s.fresh_start]
        tracker = self._read("Line_Tracker", required=False)
        phone_master = self._read("Phone_Master")
        try:
            master = self._read("Master")
        except Exception as e:  # '02e Log Sheets Read Error'
            log.error("Could not read Master: %s", e)
            self.logs.append(log_row(
                ctx, **{"Alert Type": "SHEETS_READ", "Message": "Could not read PO_Master – no alerts processed this run",
                        "UltraMsg Response": "Not attempted", "Status": "SHEETS_READ_FAILED", "Error": str(e)[:500],
                        "Retry Status": "RETRY_NEXT_RUN"},
            ))
            return

        cleared = detect_cleared(master, tracker, ctx.run_date, ctx.run_ts)
        self.report.cleared_lines = len(cleared)
        if cleared and not self.dry:
            try:
                self._write("upsert_rows", "Line_Tracker", cleared, key="Line Key")
            except Exception as e:
                log.error("Could not mark %d line(s) CLEARED: %s", len(cleared), e)
        tracker = self._reset_cycles(_merged(tracker, cleared))
        self.cycle_starts = cycle_starts(tracker)
        line_streaks = self._check_overdue(master, tracker)

        groups = build_groups(master, tracker, ctx.now, s.max_lines_per_message, line_streaks)
        groups = apply_phone_master(groups, phone_master)
        vendor_emails = self.sheet.vendor_emails() if hasattr(self.sheet, "vendor_emails") else {}
        for g in groups:  # Scenario 4: the vendor's own e-mail ids join the management meeting
            g["vendorEmails"] = vendor_emails.get(g["vendorCode"], "")
        sent_keys = {txt(r.get("Dedupe Key")) for r in today_log if txt(r.get("Dedupe Key"))}
        groups = select_alerts(groups, sent_keys, s.vendor_filter)
        self.report.vendor_groups = len(groups)
        log.info("%d vendor/level group(s) to alert today (%s)", len(groups), "trial" if self.dry else "live")

        for g in groups:
            if self._stop_requested():
                break
            self._handle_group(g)
            # Log each vendor as soon as it is handled: if the process dies mid-run, what was already
            # sent is still in Alert_Log (audit trail + today's dedupe keys), not lost with the run
            self._flush_logs()

    def _stop_requested(self) -> bool:
        if self.report.stopped:
            return True
        if self.should_stop is None or not self.should_stop():
            return False
        self.report.stopped = True
        log.warning("Emergency stop: run ended early")
        self.logs.append(log_row(self.ctx, **{"Alert Type": "RUN_STOPPED", "UltraMsg Response": "N/A", "Status": "RUN_STOPPED",
                                              "Message": "Emergency stop: this run ended early"}))
        return True

    def _reset_cycles(self, tracker: list[dict]) -> list[dict]:
        """Vendors whose cycle is over go back to Scenario 1. Returns the tracker as the rest of the run sees it
        (a trial run works on that copy only). If the reset cannot be saved the vendor keeps its scenario today."""
        ctx, s = self.ctx, self.s
        rows = cycle_resets(tracker, ctx.run_date, s.cycle_days, s.vendor_filter, ctx.run_ts)
        if not rows:
            return tracker
        if not self.dry:
            try:
                self._write("upsert_rows", "Line_Tracker", rows, key="Line Key")
            except Exception as e:
                log.error("Could not reset the cycle of %d line(s): %s", len(rows), e)
                return tracker
        vendors: dict[tuple, int] = {}
        for r in rows:
            vendor = (r["Vendor Code"], r["Vendor Name"])
            vendors[vendor] = vendors.get(vendor, 0) + 1
        for (code, name), lines in vendors.items():
            self.logs.append(log_row(
                ctx, **{"Vendor": f"{name} ({code})", "Alert Type": "CYCLE_RESET", "UltraMsg Response": "N/A",
                        "Message": f"{s.cycle_days} days after Scenario 4: {lines} open line(s) back to Scenario 1",
                        "Status": "DRY_RUN" if self.dry else "CYCLE_RESET"}))
        self.report.cycle_resets = len(vendors)
        log.info("%d vendor(s) back to Scenario 1, %d days after Scenario 4", len(vendors), s.cycle_days)
        return _merged(tracker, rows)

    def _check_overdue(self, master: list[dict], tracker: list[dict]) -> dict[str, int]:
        """Record this period's overdue check and return each overdue line's streak (checks in a row overdue)."""
        current = overdue_now(master)
        period = period_start(self.ctx.now, self.s.check_period_minutes)
        if hasattr(self.sheet, "record_check"):
            self.sheet.record_check(period, current, len(master))
        earlier = (self.sheet.recent_checks(period - timedelta(microseconds=1), LEVELS - 1)
                   if hasattr(self.sheet, "recent_checks") else [])
        line_streaks = streaks([(period, set(current))] + earlier, current, _cycle_periods(tracker, period))
        by_scenario = [0] * LEVELS
        for n in line_streaks.values():
            by_scenario[confirmed_level(n)] += 1
        self.report.check = {"period": period.isoformat(), "overdue_lines": len(current), "by_scenario": by_scenario,
                             "earlier_checks": len(earlier)}
        log.info("Overdue check %s: %d line(s); confirmed Scenario 1/2/3/meeting: %s", period.isoformat(),
                 len(current), by_scenario)
        return line_streaks

    @traceable(name="vendor_alert", run_type="chain", process_inputs=lambda i: _group_summary(i["g"]))
    def _handle_group(self, g: dict) -> None:
        ctx, s = self.ctx, self.s
        level = g["nextLevel"]
        tracker_updated = False
        any_success = False

        message = message_for(g)

        if level == 2 and not self.dry:  # '07d/07e' call task for the buyer
            self._try_write("append_rows", "Call_Tasks", [call_task_row(g, ctx.run_ts)])
        if level == 2 and s.agent_calls and self.caller and not self.dry:
            any_success = self._agent_call(g)  # the buyer's WhatsApp is a copy: without it, Scenario 3 is still done

        if level == 3:  # '07f/07g/07i' meeting task + log
            meeting = meeting_task_row(g, ctx.run_ts, ctx.now, s)
            if s.meet_invites and self.meet and not self.dry:
                meet_link = self._schedule_meet(g, meeting)
                if meet_link:
                    message += f"\n\nGoogle Meet ({meeting['Proposed Date']}): {meet_link}"
            if not self.dry:
                self._try_write("append_rows", "Meeting_Tasks", [meeting])
            self.logs.append(log_row(
                ctx, **{"PO Number": g["poNumber"], "Vendor": g["vendorLabel"],
                        "Recipient Role": "SCM Head / Buyer / SCM Coordinator", "Recipient Name": "Management Meeting",
                        "Alert Type": "MGMT_MEETING_DAY3", "Escalation Level": "3",
                        "Message": f"{meeting['Meeting Title']} | {meeting['Meeting Status']}",
                        "UltraMsg Response": "N/A – no WhatsApp at Day 3",
                        "Status": "DRY_RUN" if self.dry else "MEETING_TASK_CREATED",
                        "Dedupe Key": "" if self.dry else g["alertKey"]},
            ))
            self._update_tracker(g)
            tracker_updated = True

        for rcpt in recipients_for(g, s):
            if self._stop_requested():
                break
            if self._send_one(g, rcpt, message):
                any_success = True
        if any_success and not tracker_updated:  # '09d-09f' advance the escalation level
            self._update_tracker(g)

    @traceable(name="agent_call", run_type="tool", process_inputs=lambda i: {
        "vendor": i["g"]["vendorLabel"], "mobile": mask_phones(i["g"].get("scenario3Raw"))})
    def _agent_call(self, g: dict) -> bool:
        """Scenario 3: the voice agent rings the Scenario 3 number. True if the vendor has had its call:
        it was placed now, or the vendor already spoke in this cycle."""
        s = self.s
        raw = txt(g.get("scenario3Raw"))
        mobile = normalize_mobile(raw)
        base = {"PO Number": g["poNumber"], "Vendor": g["vendorLabel"], "Recipient Role": "Vendor (Agent call, Scenario 3)",
                "Recipient Name": g["vendorName"], "Mobile": mobile or raw or "(blank)", "Alert Type": "AGENT_CALL_DAY2",
                "Escalation Level": "2", "Message": f"Voice agent call to {mobile or raw or '-'}"}
        spoke_at = self._vendor_spoke(g)
        if spoke_at:
            log.info("No agent call to %s: the vendor already spoke in this cycle", g["vendorLabel"])
            self.logs.append(log_row(self.ctx, **{
                **base, "Message": f"No call: the vendor already spoke on the call of "
                                   f"{spoke_at.astimezone(TIMEZONE):%Y-%m-%d %H:%M}, this cycle",
                "UltraMsg Response": "Not attempted", "Status": "CALL_SKIPPED"}))
            return True
        if not is_valid_mobile(mobile):
            self.logs.append(log_row(self.ctx, **base, **{
                "UltraMsg Response": "Not attempted", "Status": "INVALID_MOBILE",
                "Error": "Invalid/Missing Scenario 3 number", "Retry Status": "FIX_MOBILE_IN_SOURCE"}))
            return False
        key = self._register_call(g, mobile)  # a row the voice server fills with the transcript and what it understood
        try:
            call_id = self.caller.call(mobile, call_script(g, s.max_lines_per_message, key))
        except Exception as e:
            self._on_call_row(calls.fail_call, key, str(e))
            log.error("Agent call to %s failed: %s", mask_phones(mobile), e)
            self.logs.append(log_row(self.ctx, **base, **{
                "UltraMsg Response": "-", "Status": "CALL_FAILED", "Error": str(e)[:500], "Retry Status": "CALL_MANUALLY"}))
            return False
        self._on_call_row(calls.attach_frejun_id, key, call_id)
        log.info("Agent call to %s placed, FreJun call id %s", mask_phones(mobile), call_id)
        self.logs.append(log_row(self.ctx, **base, **{"UltraMsg Response": f"FreJun call id {call_id}",
                                                      "Status": "CALL_PLACED"}))
        return True

    def _vendor_spoke(self, g: dict) -> datetime | None:
        """When the vendor spoke on an agent call in its current cycle, if it did. If that cannot be read the
        call is placed: a missed escalation costs more than a second call."""
        start = self.cycle_starts.get(txt(g["vendorCode"]), self.ctx.run_date)
        since = datetime(start.year, start.month, start.day, tzinfo=TIMEZONE)
        if self.s.fresh_start:
            since = max(since, datetime.strptime(self.s.fresh_start, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TIMEZONE))
        try:
            return calls.spoke_since(g["vendorCode"], since)
        except Exception:
            log.exception("Could not read %s's earlier calls", g["vendorLabel"])
            return None

    def _register_call(self, g: dict, mobile: str) -> str:
        """Create the call's row. Returns its key, or '' if it could not be saved (the call is still placed)."""
        key = calls.new_key()
        try:
            calls.register_call(key, g["vendorCode"], g["vendorName"], mobile,
                                script_lines(g, self.s.max_lines_per_message), self.ctx.triggered_by)
        except Exception:
            log.exception("Could not save the call row; the call is placed without a transcript record")
            return ""
        return key

    @staticmethod
    def _on_call_row(update, key: str, value: str) -> None:
        """Best effort: a failed bookkeeping write never changes what happened to the call."""
        if not key:
            return
        try:
            update(key, value)
        except Exception:
            log.exception("Could not update call row %s", key)

    @traceable(name="meet_invite", run_type="tool", process_inputs=lambda i: {"vendor": i["g"]["vendorLabel"]})
    def _schedule_meet(self, g: dict, meeting: dict) -> str:
        """Day 3: Google Calendar event with a Meet link. Updates the meeting row; returns the Meet link or ''."""
        attendees = [e for e in meeting["Attendee Emails"].split(",") if e]
        if not attendees:
            meeting["Meeting Status"] = "PENDING – no attendee emails. Schedule manually."
            return ""
        start = datetime.strptime(meeting["Proposed Date"], "%Y-%m-%d %H:%M").replace(tzinfo=TIMEZONE)
        try:
            links = self.meet.schedule(meeting["Meeting Title"], meeting["Meeting Description"], start, attendees)
        except Exception as e:
            log.error("Could not create the Google Meet for %s: %s", g["vendorLabel"], e)
            meeting["Meeting Status"] = f"PENDING – Google Meet failed ({str(e)[:200]}). Schedule manually."
            return ""
        meeting["Meeting Status"] = f"SCHEDULED – {links['meet_link'] or links['event_link']} – invited {', '.join(attendees)}"
        return links["meet_link"] or links["event_link"]

    @traceable(name="whatsapp_send", run_type="tool", process_inputs=lambda i: {
        "vendor": i["g"]["vendorLabel"], "alert_type": i["g"]["alertType"], "role": i["rcpt"]["role"],
        "mobile": mask_phones(i["rcpt"].get("rawMobile")), "message": mask_phones(i["message"])})
    def _send_one(self, g: dict, rcpt: dict, message: str) -> bool:
        ctx, s = self.ctx, self.s
        raw = txt(rcpt.get("rawMobile"))
        mobile = normalize_mobile(raw)
        base = {"PO Number": g["poNumber"], "Vendor": g["vendorLabel"], "Recipient Role": rcpt["role"],
                "Recipient Name": rcpt.get("name") or "", "Alert Type": g["alertType"],
                "Escalation Level": str(g["nextLevel"]), "Message": message}
        alert = {"vendor": g["vendorLabel"], "level": g["nextLevel"], "alertType": g["alertType"],
                 "role": rcpt["role"], "mobile": mobile or raw, "lines": g["overdueLines"]}
        self.report.alerts.append(alert)

        if not is_valid_mobile(mobile):  # '09c Log Invalid Mobile'
            alert["status"] = "INVALID_MOBILE"
            self.logs.append(log_row(ctx, **base, **{
                "Mobile": raw or "(blank)", "UltraMsg Response": "Not attempted", "Status": "INVALID_MOBILE",
                "Error": "Invalid/Missing Mobile Number", "Retry Status": "FIX_MOBILE_IN_SOURCE"}))
            return False

        if self.dry:
            response = DRY_RUN_RESPONSE
        else:
            if self._sent_any_real:
                self.sleep(s.send_interval_seconds)  # UltraMsg rate limit
            self._sent_any_real = True
            try:
                response = self.sender.send_text(mobile, message)
            except Exception as e:
                response = {"error": mask_token(str(e))}

        if str(response.get("sent", "")).lower() == "true":  # '09a Build Success Log'
            alert["status"] = "DRY_RUN" if self.dry else "SENT"
            self.logs.append(log_row(ctx, **base, **{
                "Mobile": mobile,
                "UltraMsg Response": json.dumps({k: response.get(k) for k in ("sent", "message", "id")}),
                "Status": alert["status"], "Dedupe Key": "" if self.dry else g["alertKey"]}))
            return True

        alert["status"] = "FAILED"  # '09b Build Failure Log'
        error = response.get("error")
        error_text = (error.get("message") or json.dumps(error)) if isinstance(error, dict) else (error or "UltraMsg did not confirm sent")
        log.error("WhatsApp to %s failed: %s", mobile, error_text)
        self.logs.append(log_row(ctx, **base, **{
            "Mobile": mobile, "UltraMsg Response": mask_token(json.dumps(response, ensure_ascii=False))[:500],
            "Status": "FAILED", "Error": mask_token(str(error_text)),
            "Retry Status": "RETRY_PENDING – resent on next run if no other recipient of this PO succeeded"}))
        return False

    def _update_tracker(self, g: dict) -> None:
        if self.dry:  # '09h Live Mode Only'
            return
        try:
            self._write("upsert_rows", "Line_Tracker", tracker_rows(g, self.ctx.run_iso, self.ctx.now_text()), key="Line Key")
        except Exception as e:  # '09g Log Tracker Update Error'
            log.error("Line_Tracker update failed for %s: %s", g["vendorLabel"], e)
            self.logs.append(log_row(self.ctx, **{
                "PO Number": g["poNumber"], "Vendor": g["vendorLabel"], "Alert Type": "PO_MASTER_UPDATE",
                "Escalation Level": str(g["nextLevel"]), "Message": "Alert was sent but Line_Tracker could not be updated",
                "Status": "SHEET_UPDATE_FAILED", "Error": str(e)[:500],
                "Retry Status": "MANUAL – fix Line_Tracker row (Alert_Log Dedupe Key still blocks re-send today)"}))

    def _try_write(self, op: str, title: str, rows: list[dict]) -> None:
        try:
            self._write(op, title, rows)
        except Exception as e:
            log.error("Could not write to %s: %s", title, e)

    def _flush_logs(self) -> None:
        """'09 Append Alert Log': writes the log rows collected since the last flush."""
        if not self.logs:
            return
        rows, self.logs = self.logs, []
        self.report.log_rows.extend(rows)
        try:
            self._write("append_rows", "Alert_Log", rows)
        except Exception:
            log.exception("Could not append %d row(s) to Alert_Log", len(rows))


def _merged(tracker: list[dict], updates: list[dict]) -> list[dict]:
    """The tracker with `updates` (rows keyed by Line Key) applied."""
    changed = {u["Line Key"]: u for u in updates}
    return [{**t, **changed.get(txt(t.get("Line Key")), {})} for t in tracker]


def _cycle_periods(tracker: list[dict], period: datetime) -> dict[str, datetime]:
    """Line key -> the first check period of the line's cycle; a line sent back to the start begins at `period`."""
    out = {}
    for t in tracker:
        if "CLEARED" in txt(t.get("Status")).upper():
            continue
        first = parse_date(t.get("First Alert Date"))
        out[txt(t.get("Line Key"))] = datetime(first.year, first.month, first.day, tzinfo=TIMEZONE) if first else period
    return out


def _group_summary(g: dict) -> dict:
    return {"vendor": g["vendorLabel"], "level": g["nextLevel"], "alert_type": g["alertType"],
            "lines": g["overdueLines"], "pos": g["poCount"], "max_days_overdue": g["daysOverdue"],
            "phone_source": g.get("phoneSource")}


def _report_summary(report: RunReport) -> dict:
    statuses: dict[str, int] = {}
    for a in report.alerts:
        statuses[a.get("status", "?")] = statuses.get(a.get("status", "?"), 0) + 1
    return {"run_date": report.run_date, "vendor_groups": report.vendor_groups,
            "messages": len(report.alerts), "by_status": statuses, "cleared_lines": report.cleared_lines,
            "cycle_resets": report.cycle_resets, "stopped": report.stopped, "alert_log_rows": len(report.log_rows), "check": report.check}


@traceable(
    name="po_followup_daily_run",
    run_type="chain",
    tags=["po-followup"],
    process_inputs=lambda i: {"triggered_by": i.get("triggered_by"), "dry_run": i["settings"].dry_run},
    process_outputs=_report_summary,
)
def run_daily(settings: Settings, sheet, sender, *, triggered_by: str = "Manual run",
              now: datetime | None = None, sleep=time.sleep, caller=None, meet=None, should_stop=None) -> RunReport:
    ctx = RunContext(now or datetime.now(TIMEZONE), triggered_by)
    return DailyRun(settings, sheet, sender, ctx, sleep=sleep, caller=caller, meet=meet, should_stop=should_stop).execute()
