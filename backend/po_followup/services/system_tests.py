"""The admin's checks: one real WhatsApp message, one real voice-agent call, one real Google Meet invite.

They use the same integrations as a follow-up run but touch no vendor, tracker or log, so they can be repeated freely.
"""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import httpx
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request

from backend.core.env import TIMEZONE
from backend.core.google_auth import configured as delegation_configured, delegated_credentials
from backend.po_followup.config import Settings
from backend.po_followup.domain.escalation import indian_mobile
from backend.po_followup.domain.messages import message_for, proposed_meeting_time
from backend.po_followup.integrations.agent_call import AgentCaller, call_script
from backend.po_followup.integrations.meet import SCOPES, MeetScheduler
from backend.po_followup.integrations.whatsapp import UltraMsg, mask_token
from backend.po_followup.services import users
from backend.po_followup.services.vendor_numbers import check_emails
from backend.voice_agent import call_context
from backend.voice_agent.config import FREJUN_PHONE_NUMBER, TELER_API_KEY

SAMPLE_VENDOR = {
    "vendorLabel": "Sample Vendor (TEST)", "vendorName": "Sample Vendor", "vendorCode": "TEST",
    "lines": [{"poNumber": "4500000001", "poItem": "10", "desc": "Sample part", "dueText": "1 week ago",
               "pending": "100", "uom": "NOS", "od": 7}],
}


SAMPLE_ALERT = {
    "nextLevel": 0, "vendorName": "Sample Vendor", "vendorCode": "TEST", "overdueLines": 1, "poCount": 1, "daysOverdue": 7,
    "lineSummary": "PO 4500000001 / 10: Sample part, 100 NOS pending, due 1 week ago",
}
CHECK_SECONDS = 10


def _number(raw: str) -> str:
    m = indian_mobile(raw)
    if not m:
        raise ValueError("Enter a 10-digit Indian mobile number.")
    return m


def _check_whatsapp() -> dict:
    s = Settings.from_env()
    if not (s.ultramsg_base_url and s.ultramsg_token):
        return {"ok": False, "detail": "Add ULTRAMSG_BASE_URL and ULTRAMSG_TOKEN to .env."}
    r = httpx.get(f"{s.ultramsg_base_url}/instance/status", params={"token": s.ultramsg_token}, timeout=CHECK_SECONDS)
    state = r.json().get("status", {}).get("accountStatus", {}).get("status", "") if r.status_code == 200 else ""
    if state == "authenticated":
        return {"ok": True, "detail": "WhatsApp number is connected."}
    return {"ok": False, "detail": f"WhatsApp number is not connected ({state or f'HTTP {r.status_code}'}). Scan the QR code in UltraMsg."}


def _check_call() -> dict:
    if not (TELER_API_KEY and FREJUN_PHONE_NUMBER):
        return {"ok": False, "detail": "Add TELER_API_KEY and FREJUN_PHONE_NUMBER to .env."}
    base = os.getenv("PUBLIC_URL", "").rstrip("/")
    if not base.startswith("https://"):
        return {"ok": False, "detail": "PUBLIC_URL must be an https address that reaches this app."}
    r = httpx.get(f"{base}/health", timeout=CHECK_SECONDS)
    if r.status_code == 200 and r.headers.get("x-call-script") == call_context.VERSION:
        return {"ok": True, "detail": "FreJun can reach this app."}
    return {"ok": False, "detail": f"PUBLIC_URL does not reach this app (HTTP {r.status_code}). Check the tunnel and PUBLIC_URL."}


def _check_meeting() -> dict:
    if not delegation_configured():
        return {"ok": False, "detail": "Add GOOGLE_DELEGATED_USER and the Google service account to .env."}
    try:
        delegated_credentials(SCOPES).refresh(Request())
    except RefreshError:
        return {"ok": False, "detail": "Google refused Calendar access. In Workspace Admin, Domain-wide delegation, allow the "
                                       f"scope {SCOPES[0]} for this service account."}
    return {"ok": True, "detail": "Google Calendar access works."}


def _safe(check) -> dict:
    try:
        return check()
    except Exception as e:  # network, tunnel down, Google refused
        return {"ok": False, "detail": mask_token(f"Could not connect: {e}")[:200]}


def status() -> dict:
    """Live connection check of each service; sends nothing."""
    checks = {"whatsapp": _check_whatsapp, "call": _check_call, "meeting": _check_meeting}
    with ThreadPoolExecutor(len(checks)) as pool:
        return dict(zip(checks, pool.map(_safe, checks.values())))


def whatsapp(mobile: str, message: str = "") -> dict:
    m = _number(mobile)
    s = Settings.from_env()
    if not (s.ultramsg_base_url and s.ultramsg_token):
        raise ValueError("WhatsApp is not set up: add ULTRAMSG_BASE_URL and ULTRAMSG_TOKEN to .env.")
    try:
        result = UltraMsg(s.ultramsg_base_url, s.ultramsg_token).send_text(
            "91" + m, message.strip() or "TEST MESSAGE, please ignore.\n\n" + message_for(SAMPLE_ALERT))
    except Exception as e:  # network or HTTP error
        raise ValueError(f"WhatsApp not sent: {mask_token(str(e))}") from e
    if str(result.get("sent")).lower() != "true":
        raise ValueError(f"WhatsApp not sent: {result.get('error') or result.get('message') or result}")
    users.audit(users.admin_username(), "test_whatsapp", {"to": m})
    return {"ok": True, "to": m}


def call(mobile: str) -> dict:
    m = _number(mobile)
    try:
        call_id = AgentCaller().call("91" + m, call_script(SAMPLE_VENDOR))
    except Exception as e:  # not configured, tunnel down, FreJun error: show the reason on the page
        raise ValueError(f"Call not placed: {getattr(e, 'message', None) or e}") from e
    users.audit(users.admin_username(), "test_call", {"to": m})
    return {"ok": True, "to": m, "call_id": call_id}


def meeting(email: str) -> dict:
    address = check_emails(email)
    if not address or "," in address:
        raise ValueError("Enter one e-mail id.")
    start = datetime.strptime(proposed_meeting_time(datetime.now(TIMEZONE)), "%Y-%m-%d %H:%M").replace(tzinfo=TIMEZONE)
    try:
        links = MeetScheduler().schedule("PO Agent test meeting", "Test invite from PO Agent. You can delete this event.",
                                         start, [address])
    except Exception as e:  # scope missing, no access, Google error
        raise ValueError(f"Invite not created: {e}") from e
    users.audit(users.admin_username(), "test_meeting", {"to": address})
    return {"ok": True, "to": address, "starts": start.isoformat(), **links}
