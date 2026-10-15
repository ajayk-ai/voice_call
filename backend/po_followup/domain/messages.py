"""WhatsApp texts, recipients per escalation level, and the rows written to the tracking tabs."""

from datetime import datetime, timedelta

from .config import Settings
from .escalation import recipients_from

# Header rows of the tabs this workflow writes (created by `python -m po_followup setup-tabs`)
TAB_HEADERS = {
    "Line_Tracker": ["Line Key", "PO No", "PO Item", "Sch Line", "Vendor Code", "Vendor Name", "Material", "OD Days",
                     "Escalation Level", "Last Alert Type", "Last Alert Date", "First Alert Date", "Status",
                     "Cleared Date", "Updated At", "Last Alert At"],
    "Alert_Log": ["Timestamp", "Alert Date", "PO Number", "Vendor", "Recipient Role", "Recipient Name", "Mobile",
                  "Alert Type", "Escalation Level", "Message", "UltraMsg Response", "Status", "Error", "Retry Status",
                  "Triggered By", "Dedupe Key"],
    "Call_Tasks": ["Task ID", "Created At", "Call Required", "Call Date", "PO Number", "Vendor", "Vendor Mobile",
                   "Vendor HOD Mobile", "Assigned To", "Assigned To Mobile", "Escalation Level", "Days Overdue", "Issue",
                   "Task Status", "Instructions"],
    "Meeting_Tasks": ["Meeting ID", "Created At", "Meeting Title", "Proposed Date", "Participants", "Attendee Emails",
                      "PO Number", "Vendor", "Material", "PO Date", "Expected Dispatch Date", "Days Overdue",
                      "Escalation History", "Required Action", "Meeting Description", "Meeting Status"],
    "Vendor_Replies": ["Received At", "Message ID", "Sender Mobile", "Sender Name", "PO Number", "PO Match Method",
                       "Vendor", "Vendor Response", "Promised Dispatch Date", "Response Status", "Processing Notes"],
}


def _counts(g: dict) -> str:
    return f"{g['overdueLines']} | POs: {g['poCount']} | Max days overdue: {g['daysOverdue']}"


def message_for(g: dict) -> str:
    level = g["nextLevel"]
    if level == 0:
        return (
            "⚠️ PO ACTION REQUIRED – BULL MACHINES\n\n"
            f"Dear {g['vendorName']} ({g['vendorCode']}),\n\n"
            "The following PO lines are overdue for dispatch:\n\n"
            f"{g['lineSummary']}\n\n"
            f"Overdue lines: {_counts(g)}\n\n"
            "Please confirm the current dispatch status and expected dispatch date for each PO.\n"
            "(Kindly mention the PO number in your reply.)\n\n"
            "Regards,\nBull Machines SCM Team"
        )
    if level == 1:
        return (
            "🚨 PO ESCALATION – NO RESPONSE (DAY 1)\n\n"
            f"Dear {g['vendorName']} Management,\n\n"
            "We alerted your team yesterday about the following overdue PO lines, but they are still pending:\n\n"
            f"{g['lineSummary']}\n\n"
            f"Pending lines: {_counts(g)}\n\n"
            "Please intervene and share a confirmed dispatch commitment for each PO today.\n"
            "(Kindly mention the PO number in your reply.)\n\n"
            "Regards,\nBull Machines SCM"
        )
    if level == 2:
        return (
            "📞 CALL TASK – PO ESCALATION (DAY 2)\n\n"
            f"Vendor: {g['vendorName']} ({g['vendorCode']})\n"
            "No response after WhatsApp to vendor planner/coordinator (Day 0) and vendor management (Day 1).\n\n"
            f"Please CALL vendor management today: {g['vendorMgmtMobileRaw'] or 'number not available in Master'}\n\n"
            f"{g['lineSummary']}\n\n"
            f"Pending lines: {_counts(g)}\n\n"
            "Please update the outcome in the Call_Tasks sheet.\n\n"
            "Bull Machines SCM"
        )
    return (
        "📅 MEETING REQUIRED – PO ESCALATION (DAY 3)\n\n"
        f"Vendor: {g['vendorName']} ({g['vendorCode']})\n"
        "Still pending after WhatsApp (Day 0 & Day 1) and call escalation (Day 2).\n\n"
        f"{g['lineSummary']}\n\n"
        f"Pending lines: {_counts(g)}\n\n"
        "Please schedule an escalation meeting with vendor management. Details are in the Meeting_Tasks sheet.\n\n"
        "Bull Machines SCM"
    )


def recipients_for(g: dict, s: Settings) -> list[dict]:
    level = g["nextLevel"]
    name = g["vendorName"]
    if level == 0:
        return recipients_from(g["scenario1Raw"], "Vendor Contact (Scenario 1)", name, "Contact", name)
    if level == 1:
        return recipients_from(g["scenario2Raw"], "Vendor Management (Scenario 2)", name, "Management", f"{name} (Management)")
    buyer = {"role": "Buyer (Call Task)" if level == 2 else "Buyer (Meeting)", "name": g["buyerName"], "rawMobile": g["buyerMobileRaw"]}
    if level == 2:
        return [buyer]
    return [buyer, {"role": "Bull Machines SCM HOD", "name": s.scm_hod_name, "rawMobile": s.scm_hod_mobile}]


# ---------- task rows ----------

def call_task_row(g: dict, run_ts: str) -> dict:
    return {
        "Task ID": f"{g['vendorCode']}-CALL-{g['runDate']}",
        "Created At": run_ts,
        "Call Required": "YES",
        "Call Date": g["runDate"],
        "PO Number": g["poNumber"],
        "Vendor": g["vendorLabel"],
        "Vendor Mobile": g["vendorMgmtMobileRaw"],
        "Vendor HOD Mobile": g["vendorPlannerMobileRaw"],
        "Assigned To": g["buyerName"] or "SCM Coordinator",
        "Assigned To Mobile": g["buyerMobileRaw"],
        "Escalation Level": "2 – Call Flow (Day 2)",
        "Days Overdue": g["daysOverdue"],
        "Issue": g["issueSummary"],
        "Task Status": "OPEN",
        "Instructions": "Buyer to CALL vendor management (Vendor Mobile column) and get a confirmed dispatch date for "
                        "the listed PO lines. If no response, a meeting is scheduled tomorrow.",
    }


def proposed_meeting_time(now: datetime) -> str:
    """Next day 11:00, skipping Sunday."""
    days = 2 if (now + timedelta(days=1)).isoweekday() == 7 else 1
    return (now + timedelta(days=days)).replace(hour=11, minute=0, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M")


def meeting_task_row(g: dict, run_ts: str, now: datetime, s: Settings) -> dict:
    scm_head_email = g["scmHeadEmail"] or s.scm_head_email
    emails = list(dict.fromkeys(e for e in (scm_head_email, g["buyerEmail"], g["buyerLeadEmail"]) if e))
    last_type, last_date = g["lastAlertType"] or "-", g["previousAlertDate"] or "-"
    return {
        "Meeting ID": f"{g['vendorCode']}-MEET-{g['runDate']}",
        "Created At": run_ts,
        "Meeting Title": f"PO Escalation – {g['vendorName']} ({g['vendorCode']}) – {g['poCount']} POs overdue",
        "Proposed Date": proposed_meeting_time(now),
        "Participants": f"SCM Head ({scm_head_email or '-'}) | Buyer ({g['buyerEmail'] or '-'}) | "
                        f"Buyer Lead ({g['buyerLeadEmail'] or '-'}) | Vendor Planner: {g['vendorPlannerMobileRaw'] or '-'}",
        "Attendee Emails": ",".join(emails),
        "PO Number": g["poNumber"],
        "Vendor": g["vendorLabel"],
        "Material": g["material"],
        "PO Date": g["poDateDisplay"],
        "Expected Dispatch Date": g["expectedDisplay"],
        "Days Overdue": g["daysOverdue"],
        "Escalation History": "Day 0 vendor alert → Day 1 Vendor HOD + Buyer escalation → Day 2 management escalation "
                              f"+ call task. Last alert: {last_type} on {last_date}. Full history in Alert_Log "
                              "(filter by PO Number).",
        "Required Action": "Obtain confirmed dispatch commitment from vendor management; agree recovery plan; decide on "
                           "alternate sourcing if required.",
        "Meeting Description": f"Vendor: {g['vendorLabel']}\nOverdue lines: {_counts(g)}\n"
                               f"Earliest PO date: {g['poDateDisplay']} | Earliest due date: {g['expectedDisplay']}\n\n"
                               f"{g['lineSummary']}\n\nEscalation history: Day 0 vendor alert, Day 1 Vendor Planner + "
                               f"Buyer escalation, Day 2 management escalation + call task. Last alert: {last_type} on "
                               f"{last_date}.\n\nRequired action: confirmed dispatch commitment and recovery plan.",
        "Meeting Status": "PENDING – Google Calendar not configured. Schedule manually.",
    }


def tracker_rows(g: dict, run_iso: str, ts: str) -> list[dict]:
    return [
        {
            "Line Key": l["key"], "PO No": l["poNumber"], "PO Item": l["poItem"], "Sch Line": l["schedLine"],
            "Vendor Code": g["vendorCode"], "Vendor Name": g["vendorName"], "Material": l["material"],
            "OD Days": str(l["od"]), "Escalation Level": str(g["nextLevel"]), "Last Alert Type": g["alertType"],
            "Last Alert Date": run_iso, "First Alert Date": l["firstAlertDate"],
            "Status": "MEETING_SCHEDULED" if g["nextLevel"] == 3 else "OPEN", "Cleared Date": "", "Updated At": ts,
            "Last Alert At": ts,
        }
        for l in g["lines"]
    ]


# ---------- Alert_Log rows ----------

def log_row(ctx, **fields) -> dict:
    """One Alert_Log row; anything not given is '-' or empty like in the n8n version."""
    row = {
        "Timestamp": ctx.now_text(), "Alert Date": ctx.run_iso, "PO Number": "-", "Vendor": "-",
        "Recipient Role": "-", "Recipient Name": "-", "Mobile": "-", "Alert Type": "-", "Escalation Level": "-",
        "Message": "", "UltraMsg Response": "-", "Status": "", "Error": "", "Retry Status": "N/A",
        "Triggered By": ctx.triggered_by, "Dedupe Key": "",
    }
    row.update(fields)
    return row
