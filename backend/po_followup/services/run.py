"""One daily run of the PO follow-up workflow (n8n: '01 Daily PO Trigger' through '09 Append Alert Log')."""

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from monitoring.tracing import mask_phones, traceable

from .config import Settings
from .escalation import (
    apply_phone_master, build_groups, detect_cleared, is_valid_mobile, normalize_mobile, select_alerts, txt,
)
from .agent_call import call_script
from .messages import call_task_row, log_row, meeting_task_row, message_for, recipients_for, tracker_rows
from .whatsapp import mask_token

log = logging.getLogger("po_followup")

ZONE = ZoneInfo("Asia/Kolkata")
DRY_RUN_RESPONSE = {"sent": "true", "message": "DRY RUN – WhatsApp NOT sent (testMode = true)", "id": "DRY-RUN"}


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
        return datetime.now(ZONE).strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class RunReport:
    run_date: str
    preview: bool
    alerts: list[dict] = field(default_factory=list)  # one entry per recipient message
    cleared_lines: int = 0
    vendor_groups: int = 0
    sheet_writes: list[str] = field(default_factory=list)
    log_rows: list[dict] = field(default_factory=list)


class DailyRun:
    def __init__(self, settings: Settings, sheet, sender, ctx: RunContext, preview: bool = False, sleep=time.sleep,
                 caller=None, meet=None):
        self.s = settings
        self.sheet = sheet
        self.sender = sender
        self.caller = caller  # Scenario 3 agent call (agent_call.AgentCaller); None = no call
        self.meet = meet  # Day 3 Google Meet invite (meet.MeetScheduler); None = task row only
        self.ctx = ctx
        self.preview = preview
        self.dry = settings.test_mode or preview  # no real WhatsApp
        self.sleep = sleep
        self.report = RunReport(ctx.run_iso, preview)
        self.logs: list[dict] = []  # Alert_Log rows not written yet
        self._sent_any_real = False

    # ----- sheet writes (skipped in preview) -----

    @traceable(name="sheet_write", run_type="tool",
               process_inputs=lambda i: {"op": i["op"], "tab": i["title"], "rows": len(i["rows"])})
    def _write(self, op: str, title: str, rows: list[dict], **kwargs) -> None:
        self.report.sheet_writes.append(f"{op} {title}: {len(rows)} row(s)" + (" [preview: not written]" if self.preview else ""))
        if not self.preview:
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
        today_log = [r for r in self._read("Alert_Log", required=False) if txt(r.get("Alert Date")) == ctx.run_iso]
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
        if cleared and s.tracker_writes_enabled:
            try:
                self._write("upsert_rows", "Line_Tracker", cleared, key="Line Key")
            except Exception as e:
                log.error("Could not mark %d line(s) CLEARED: %s", len(cleared), e)

        groups = build_groups(master, tracker, ctx.now, s.max_lines_per_message, s.intervals_minutes)
        groups = apply_phone_master(groups, phone_master)
        sent_keys = {txt(r.get("Dedupe Key")) for r in today_log if txt(r.get("Dedupe Key"))}
        groups = select_alerts(groups, sent_keys, s.pilot_mode, s.pilot_vendor_codes)
        self.report.vendor_groups = len(groups)
        log.info("%d vendor/level group(s) to alert today (%s)", len(groups), "preview" if self.preview else
                 "dry run" if s.test_mode else "live")

        for g in groups:
            self._handle_group(g)
            # Log each vendor as soon as it is handled: if the process dies mid-run, what was already
            # sent is still in Alert_Log (audit trail + today's dedupe keys), not lost with the run
            self._flush_logs()

    @traceable(name="vendor_alert", run_type="chain", process_inputs=lambda i: _group_summary(i["g"]))
    def _handle_group(self, g: dict) -> None:
        ctx, s = self.ctx, self.s
        level = g["nextLevel"]
        tracker_updated = False

        message = message_for(g)

        if level == 2 and s.tracker_writes_enabled:  # '07d/07e' call task for the buyer
            self._try_write("append_rows", "Call_Tasks", [call_task_row(g, ctx.run_ts)])
        if level == 2 and s.agent_calls and self.caller and not self.dry:
            self._agent_call(g)

        if level == 3:  # '07f/07g/07i' meeting task + log
            meeting = meeting_task_row(g, ctx.run_ts, ctx.now, s)
            if s.meet_invites and self.meet and not self.dry:
                meet_link = self._schedule_meet(g, meeting)
                if meet_link:
                    message += f"\n\nGoogle Meet ({meeting['Proposed Date']}): {meet_link}"
            if s.tracker_writes_enabled:
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

        any_success = False
        for rcpt in recipients_for(g, s):
            if self._send_one(g, rcpt, message):
                any_success = True
        if any_success and not tracker_updated:  # '09d-09f' advance the escalation level
            self._update_tracker(g)

    @traceable(name="agent_call", run_type="tool", process_inputs=lambda i: {
        "vendor": i["g"]["vendorLabel"], "mobile": mask_phones(i["g"].get("scenario3Raw"))})
    def _agent_call(self, g: dict) -> None:
        """Scenario 3: the voice agent rings the Scenario 3 number (pilot: the pilot mobile)."""
        s = self.s
        raw = txt(g.get("scenario3Raw"))
        mobile = normalize_mobile(raw, s.pilot_mode, s.pilot_mobile)
        base = {"PO Number": g["poNumber"], "Vendor": g["vendorLabel"], "Recipient Role": "Vendor (Agent call, Scenario 3)",
                "Recipient Name": g["vendorName"], "Mobile": mobile or raw or "(blank)", "Alert Type": "AGENT_CALL_DAY2",
                "Escalation Level": "2", "Message": f"Voice agent call to {mobile or raw or '-'}"
                + (f" (pilot – real number: {raw or 'none'})" if s.pilot_mode else "")}
        if not is_valid_mobile(mobile):
            self.logs.append(log_row(self.ctx, **base, **{
                "UltraMsg Response": "Not attempted", "Status": "INVALID_MOBILE",
                "Error": "Invalid/Missing Scenario 3 number", "Retry Status": "FIX_MOBILE_IN_SOURCE"}))
            return
        try:
            call_id = self.caller.call(mobile, call_script(g, s.max_lines_per_message))
        except Exception as e:
            log.error("Agent call to %s failed: %s", mask_phones(mobile), e)
            self.logs.append(log_row(self.ctx, **base, **{
                "UltraMsg Response": "-", "Status": "CALL_FAILED", "Error": str(e)[:500], "Retry Status": "CALL_MANUALLY"}))
            return
        log.info("Agent call to %s placed, FreJun call id %s", mask_phones(mobile), call_id)
        self.logs.append(log_row(self.ctx, **base, **{"UltraMsg Response": f"FreJun call id {call_id}",
                                                      "Status": "CALL_PLACED"}))

    @traceable(name="meet_invite", run_type="tool", process_inputs=lambda i: {"vendor": i["g"]["vendorLabel"]})
    def _schedule_meet(self, g: dict, meeting: dict) -> str:
        """Day 3: Google Calendar event with a Meet link. Updates the meeting row; returns the Meet link or ''."""
        s = self.s
        if s.pilot_mode:
            attendees = [s.pilot_email] if s.pilot_email else []
        else:
            attendees = [e for e in meeting["Attendee Emails"].split(",") if e]
        if not attendees:
            meeting["Meeting Status"] = ("PENDING – set FOLLOWUP_PILOT_EMAIL for pilot Meet invites. Schedule manually."
                                         if s.pilot_mode else "PENDING – no attendee emails. Schedule manually.")
            return ""
        start = datetime.strptime(meeting["Proposed Date"], "%Y-%m-%d %H:%M").replace(tzinfo=ZONE)
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
        mobile = normalize_mobile(raw, s.pilot_mode, s.pilot_mobile)
        body = (
            f"🧪 PILOT TEST – real recipient would be: {rcpt['role']} ({raw or 'no number in sheet'})\n\n{message}"
            if s.pilot_mode else message
        )
        base = {"PO Number": g["poNumber"], "Vendor": g["vendorLabel"], "Recipient Role": rcpt["role"],
                "Recipient Name": rcpt.get("name") or "", "Alert Type": g["alertType"],
                "Escalation Level": str(g["nextLevel"]), "Message": body}
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
                response = self.sender.send_text(mobile, body)
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
        if not self.s.tracker_writes_enabled:  # '09h Live Mode Only'
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


def _group_summary(g: dict) -> dict:
    return {"vendor": g["vendorLabel"], "level": g["nextLevel"], "alert_type": g["alertType"],
            "lines": g["overdueLines"], "pos": g["poCount"], "max_days_overdue": g["daysOverdue"],
            "phone_source": g.get("phoneSource")}


def _report_summary(report: RunReport) -> dict:
    statuses: dict[str, int] = {}
    for a in report.alerts:
        statuses[a.get("status", "?")] = statuses.get(a.get("status", "?"), 0) + 1
    return {"run_date": report.run_date, "preview": report.preview, "vendor_groups": report.vendor_groups,
            "messages": len(report.alerts), "by_status": statuses, "cleared_lines": report.cleared_lines,
            "alert_log_rows": len(report.log_rows)}


@traceable(
    name="po_followup_daily_run",
    run_type="chain",
    tags=["po-followup"],
    process_inputs=lambda i: {"triggered_by": i.get("triggered_by"), "preview": i.get("preview"),
                              "test_mode": i["settings"].test_mode, "pilot_mode": i["settings"].pilot_mode},
    process_outputs=_report_summary,
)
def run_daily(settings: Settings, sheet, sender, *, triggered_by: str = "Manual / Test Run",
              preview: bool = False, now: datetime | None = None, sleep=time.sleep,
              caller=None, meet=None) -> RunReport:
    ctx = RunContext(now or datetime.now(ZONE), triggered_by)
    return DailyRun(settings, sheet, sender, ctx, preview=preview, sleep=sleep, caller=caller, meet=meet).execute()
