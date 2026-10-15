"""Vendor WhatsApp replies (n8n: '11 UltraMsg Incoming Webhook' -> '15 Store Vendor Reply').

Set the UltraMsg instance webhook URL to
    https://<your-render-app>.onrender.com/webhooks/ultramsg?secret=<ULTRAMSG_WEBHOOK_SECRET>
and enable webhook_message_received. Replies are stored raw in Vendor_Replies.
The PO number is filled only when the vendor writes it explicitly - it is never guessed."""

import logging
import os
import re
import secrets
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

log = logging.getLogger("po_followup.replies")
router = APIRouter()

PO_RE = re.compile(r"\bP\.?\s?O\.?\s*(?:No\.?|Number|Num|#)?\s*[:\-#]?\s*([A-Z0-9][A-Z0-9\-/]{2,})", re.I)


def extract_po_number(text: str) -> str:
    m = PO_RE.search(text or "")
    return m.group(1).upper() if m and re.search(r"\d{3,}", m.group(1)) else ""


def reply_record(payload: dict, now: datetime | None = None) -> dict | None:
    """UltraMsg webhook body -> Vendor_Replies row, or None if it isn't an inbound 1:1 vendor message."""
    data = payload.get("data") or {}
    event = payload.get("event_type") or ""
    sender = str(data.get("from") or "")
    from_me = data.get("fromMe") in (True, "true")
    if event != "message_received" or from_me or "@g.us" in sender:
        return None
    text = str(data.get("body") or "")
    po = extract_po_number(text)
    now = now or datetime.now(ZoneInfo("Asia/Kolkata"))
    return {
        "Received At": now.strftime("%Y-%m-%d %H:%M:%S"),
        "Message ID": data.get("id") or "",
        "Sender Mobile": re.sub(r"\D", "", re.sub(r"@.*$", "", sender)),
        "Sender Name": data.get("pushname") or "",
        "PO Number": po,
        "PO Match Method": "Explicit PO number in message" if po else "NOT MATCHED – no PO number in message (not guessed)",
        "Vendor": "",
        "Vendor Response": text,
        "Promised Dispatch Date": "",
        "Response Status": "RAW – PENDING_PROCESSING",
        "Processing Notes": "AI extraction not configured – raw reply stored for later processing.",
    }


_sheet = None


def _store(record: dict) -> None:
    global _sheet
    from backend.po_followup.integrations.sheets import PoSheet  # imported lazily: only needed once a reply arrives

    if _sheet is None:
        _sheet = PoSheet(os.getenv("PO_SHEET_ID", ""))
    _sheet.append_rows("Vendor_Replies", [record])


@router.post("/webhooks/ultramsg")
async def ultramsg_webhook(request: Request):
    expected = os.getenv("ULTRAMSG_WEBHOOK_SECRET")
    given = request.query_params.get("secret", "")
    if expected and not secrets.compare_digest(given.encode(), expected.encode()):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    try:
        payload = await request.json()
    except ValueError:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    record = reply_record(payload if isinstance(payload, dict) else {})
    if record is None:
        return {"ok": True, "stored": False}
    try:
        await run_in_threadpool(_store, record)
    except Exception:
        global _sheet
        _sheet = None  # reconnect next time
        log.exception("Could not store vendor reply from %s", record["Sender Mobile"])
        # 200 anyway: UltraMsg retries failed webhooks, which would only repeat the failure
        return {"ok": False, "stored": False}
    log.info("Stored vendor reply from %s (PO %s)", record["Sender Mobile"], record["PO Number"] or "not given")
    return {"ok": True, "stored": True}
