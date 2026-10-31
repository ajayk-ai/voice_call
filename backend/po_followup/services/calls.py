"""Scenario 3 agent calls: one row per call (followup.call_results), the transcript, and what was understood from it.

Lifecycle of a row (key = the id that travels inside the call script token, see voice_agent.call_context):

    placed      the follow-up run asked FreJun to ring the vendor          (register_call / attach_frejun_id)
    in_call     the vendor answered and the agent is talking               (start_call)
    completed   the call ended with something said by the vendor           (finish_call)
    no_response the call ended and the vendor never spoke                  (finish_call)
    failed      FreJun refused the call, or it was never answered          (fail_call)

When a call completes, Gemini reads the transcript and fills `analysis`: per overdue line the dispatch status,
the committed date, the delay reason; plus who was spoken to and what support the vendor asked for. The model
output is never trusted as it is: `normalise` checks every value and flags what a person should look at.
"""

import json
import logging
import os
import re
import uuid
from datetime import date, datetime, timedelta
from backend.core.env import TIMEZONE

from psycopg.types.json import Jsonb

from backend.po_followup.db import store

log = logging.getLogger("po_followup.calls")

ANALYSIS_MODEL = os.getenv("CALL_ANALYSIS_MODEL", "gemini-2.5-flash")
MAX_TRANSCRIPT_TURNS = 400

OUTCOMES = ("reached", "wrong_person", "refused", "incomplete")
LINE_STATUSES = ("dispatched", "ready_to_dispatch", "in_production", "not_started", "unknown")
REASONS = ("material_shortage", "capacity", "quality_issue", "payment", "logistics", "labour", "design_change",
           "awaiting_information", "other", "none")
# Statuses in which a committed date is expected; without one a person should follow up
NEEDS_DATE = ("ready_to_dispatch", "in_production", "not_started")

# A recording link in a FreJun status callback can sit under any of these keys (payload shapes differ by event)
RECORDING_KEYS = ("recording_url", "recordingUrl", "recording", "record_url", "audio_url")
CALL_ID_KEYS = ("call_id", "callId", "id", "call_sid", "CallSid")

ANALYSIS_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "outcome": {"type": "STRING", "enum": list(OUTCOMES)},
        "contact_name": {"type": "STRING"},
        "contact_role": {"type": "STRING"},
        "language": {"type": "STRING"},
        "summary": {"type": "STRING"},
        "support_needed": {"type": "STRING"},
        "follow_up_needed": {"type": "BOOLEAN"},
        "confidence": {"type": "STRING", "enum": ["high", "medium", "low"]},
        "lines": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "po": {"type": "STRING"},
                    "item": {"type": "STRING"},
                    "status": {"type": "STRING", "enum": list(LINE_STATUSES)},
                    "committed_date": {"type": "STRING"},
                    "committed_quantity": {"type": "STRING"},
                    "delay_reason": {"type": "STRING"},
                    "reason_category": {"type": "STRING", "enum": list(REASONS)},
                    "dispatch_reference": {"type": "STRING"},
                    "evidence": {"type": "STRING"},
                },
                "required": ["po", "status", "committed_date", "delay_reason", "reason_category", "evidence"],
            },
        },
    },
    "required": ["outcome", "summary", "lines", "follow_up_needed", "confidence"],
}

PROMPT = """You are reading the transcript of a phone call in which Bull Machines' AI assistant asked a vendor about overdue purchase order lines. The vendor may speak English, Hindi or Tamil; write everything in English.

Call date (Asia/Kolkata): {today}

Overdue lines the assistant asked about:
{lines}

Transcript (AGENT = Bull Machines assistant, VENDOR = person on the phone):
{transcript}

Extract what the VENDOR said. Rules:
- Record only what the vendor actually said. Never guess or infer a date, quantity or reason. If it was not said, leave the field empty and use status "unknown" / reason_category "none".
- committed_date: the date the vendor committed to dispatch, as YYYY-MM-DD. Resolve "next Monday", "day after tomorrow", "by the 12th" using the call date. If the vendor only said something vague like "soon" or "next week", leave it empty.
- status: dispatched, ready_to_dispatch, in_production, not_started, or unknown.
- delay_reason: the vendor's reason in one short sentence; reason_category: the closest category.
- evidence: the vendor's own words for this line (a short quote translated to English).
- Return one entry per line the assistant asked about. "po" is the PO number as digits only (no spaces), "item" is the item NUMBER from the list (digits, never the material description). If the call ended before a line was discussed, status "unknown".
- outcome: reached (a relevant person answered the questions), wrong_person, refused (would not discuss), incomplete (call dropped / not enough said).
- follow_up_needed: true if any line has no firm date, the vendor asked for help, or the call was incomplete.
- confidence: low if the transcript is short, garbled or contradictory."""


# ---------- rows ----------

def new_key() -> str:
    return uuid.uuid4().hex[:16]


def register_call(key: str, vendor_code: str, vendor_name: str, mobile: str, lines: list[dict],
                  trigger: str = "") -> None:
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO followup.call_results (key, vendor_code, vendor_name, mobile, lines, run_trigger) "
            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (key) DO NOTHING",
            (key, vendor_code, vendor_name, mobile, Jsonb(lines), trigger))


def attach_frejun_id(key: str, call_id: str) -> None:
    with store.connect() as conn:
        conn.execute("UPDATE followup.call_results SET frejun_call_id = %s WHERE key = %s", (str(call_id), key))


def fail_call(key: str, error: str) -> None:
    with store.connect() as conn:
        conn.execute("UPDATE followup.call_results SET status = 'failed', analysis_status = 'skipped', "
                     "analysis_error = %s, ended_at = now() WHERE key = %s", (error[:500], key))


def start_call(key: str) -> None:
    with store.connect() as conn:
        conn.execute("UPDATE followup.call_results SET status = 'in_call', connected_at = now() "
                     "WHERE key = %s AND status IN ('placed', 'in_call')", (key,))


def vendor_spoke(transcript: list[dict]) -> bool:
    return any(t["role"] == "vendor" and len(t["text"].split()) >= 1 for t in transcript)


def finish_call(key: str, transcript: list[dict], error: str | None = None) -> str:
    """Save the transcript of a call that has ended and analyse it. Returns the call's final status."""
    transcript = transcript[:MAX_TRANSCRIPT_TURNS]
    spoke = vendor_spoke(transcript)
    status = "completed" if spoke else "no_response"
    with store.connect() as conn:
        row = conn.execute("UPDATE followup.call_results SET status = %s, ended_at = now(), transcript = %s, "
                           "duration_seconds = GREATEST(0, EXTRACT(EPOCH FROM (now() - COALESCE(connected_at, placed_at)))::int), "
                           "analysis_status = %s, analysis_error = %s WHERE key = %s RETURNING lines",
                           (status, Jsonb(transcript), "pending" if spoke else "skipped", error, key)).fetchone()
    if spoke and row:
        analyse(key)
    return status


# ---------- FreJun status callbacks ----------

def _find(payload, keys):
    """First non-empty string under one of `keys`, looking one level into nested objects too."""
    if not isinstance(payload, dict):
        return None
    for k in keys:
        v = payload.get(k)
        if isinstance(v, str) and v:
            return v
    for v in payload.values():
        if isinstance(v, dict):
            found = _find(v, keys)
            if found:
                return found
    return None


def record_event(payload: dict) -> bool:
    """A FreJun status callback: keep it on the call's row and pick up the recording link when it carries one.
    The call is found by FreJun's call id. Returns True if a row matched."""
    call_id = _find(payload, CALL_ID_KEYS)
    if not call_id:
        return False
    recording = _find(payload, RECORDING_KEYS)
    stamped = {**payload, "_received_at": datetime.now(TIMEZONE).isoformat()}
    with store.connect() as conn:
        return conn.execute(
            "UPDATE followup.call_results SET events = events || %s::jsonb, "
            "recording_url = COALESCE(%s, recording_url) WHERE frejun_call_id = %s",
            (Jsonb([stamped]), recording, call_id)).rowcount > 0


# ---------- understanding the call ----------

def transcript_text(transcript: list[dict]) -> str:
    return "\n".join(f"{'AGENT' if t['role'] == 'agent' else 'VENDOR'}: {t['text']}" for t in transcript if t["text"])


def lines_text(lines: list[dict]) -> str:
    return "\n".join(f"{i}. PO {l.get('po')}, item {l.get('item') or '-'} - {l.get('desc') or ''} - due {l.get('due') or '?'} - "
                     f"{l.get('pending') or '?'} {l.get('uom') or ''} pending - {l.get('od')} days late"
                     for i, l in enumerate(lines, 1)) or "(not recorded)"


def _iso(v) -> date | None:
    try:
        return date.fromisoformat(str(v).strip()[:10]) if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", str(v).strip()) else None
    except ValueError:
        return None


def _enum(v, allowed, default):
    v = str(v or "").strip().lower().replace(" ", "_")
    return v if v in allowed else default


def _digits(v) -> str:
    return re.sub(r"\D", "", str(v or ""))


def reconcile(item: dict, asked: list[dict]) -> tuple[str, str, bool]:
    """(po, item number, known) for a line the model returned: matched to a line the agent was given by PO number
    (the model reads PO numbers back digit by digit, and sometimes puts the description where the item number goes)."""
    po = _digits(item.get("po"))
    same_po = [a for a in asked if _digits(a.get("po")) == po] if po else []
    if not same_po:
        return str(item.get("po") or "").strip(), str(item.get("item") or "").strip(), False
    if len(same_po) == 1:
        return str(same_po[0]["po"]), str(same_po[0].get("item") or ""), True
    given = str(item.get("item") or "").strip()
    for a in same_po:
        if given and given in (str(a.get("item") or ""), str(a.get("desc") or "")):
            return str(a["po"]), str(a.get("item") or ""), True
    return str(same_po[0]["po"]), given, True


def normalise(raw: dict, asked: list[dict], call_day: date) -> dict:
    """Check the model's answer and make it safe to show: unknown values become 'unknown', impossible dates are
    dropped and flagged. `flags` lists what a person should look at; `needs_review` is true if there are any."""
    flags: list[str] = []
    lines = []
    for item in raw.get("lines") or []:
        if not isinstance(item, dict):
            continue
        status = _enum(item.get("status"), LINE_STATUSES, "unknown")
        committed = _iso(item.get("committed_date"))
        po, item_no, known = reconcile(item, asked)
        label = f"PO {po or '?'}" + (f" item {item_no}" if item_no else "")
        if not known and asked:
            flags.append(f"{label}: not one of the lines the agent asked about")
        if item.get("committed_date") and committed is None:
            flags.append(f"{label}: '{item.get('committed_date')}' is not a valid date, ignored")
        elif committed and committed < call_day - timedelta(days=1):
            flags.append(f"{label}: committed date {committed.isoformat()} is in the past")
            committed = None
        elif committed and committed > call_day + timedelta(days=365):
            flags.append(f"{label}: committed date {committed.isoformat()} is more than a year away")
        if status in NEEDS_DATE and committed is None:
            flags.append(f"{label}: no firm dispatch date was given")
        lines.append({
            "po": po, "item": item_no, "status": status,
            "committed_date": committed.isoformat() if committed else "",
            "committed_quantity": str(item.get("committed_quantity") or "").strip(),
            "delay_reason": str(item.get("delay_reason") or "").strip()[:300],
            "reason_category": _enum(item.get("reason_category"), REASONS, "other" if item.get("delay_reason") else "none"),
            "dispatch_reference": str(item.get("dispatch_reference") or "").strip(),
            "evidence": str(item.get("evidence") or "").strip()[:300],
        })
    got = {_digits(l["po"]) for l in lines}
    missing = [a for a in asked if _digits(a.get("po")) not in got]
    if missing:
        flags.append(f"{len(missing)} line(s) the agent was given are not in the analysis: "
                     + ", ".join(f"PO {a.get('po')}" for a in missing[:5]))
    confidence = _enum(raw.get("confidence"), ("high", "medium", "low"), "low")
    if confidence == "low":
        flags.append("The model is not confident in this reading; check the transcript")
    dates = sorted(l["committed_date"] for l in lines if l["committed_date"])
    return {
        "outcome": _enum(raw.get("outcome"), OUTCOMES, "incomplete"),
        "contact_name": str(raw.get("contact_name") or "").strip(),
        "contact_role": str(raw.get("contact_role") or "").strip(),
        "language": str(raw.get("language") or "").strip(),
        "summary": str(raw.get("summary") or "").strip()[:1500],
        "support_needed": str(raw.get("support_needed") or "").strip()[:500],
        "follow_up_needed": bool(raw.get("follow_up_needed")) or any(l["status"] in NEEDS_DATE and not l["committed_date"] for l in lines),
        "confidence": confidence,
        "earliest_commitment": dates[0] if dates else "",
        "lines": lines,
        "flags": list(dict.fromkeys(flags)),
        "needs_review": bool(flags),
        "model": ANALYSIS_MODEL,
    }


def ask_model(transcript: list[dict], asked: list[dict], call_day: date) -> dict:
    """One Gemini call that turns the transcript into structured JSON (raw, not yet checked)."""
    from google.genai import types

    from backend.voice_agent.gemini import gemini_client

    response = gemini_client().models.generate_content(
        model=ANALYSIS_MODEL,
        contents=PROMPT.format(today=call_day.isoformat(), lines=lines_text(asked), transcript=transcript_text(transcript)),
        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=ANALYSIS_SCHEMA,
                                           temperature=0),
    )
    return json.loads(response.text)


def analyse(key: str) -> dict | None:
    """(Re-)read one call's transcript and save the result. A failure keeps the transcript and is shown on the page,
    where 'Analyse again' retries it."""
    with store.connect() as conn:
        row = conn.execute("SELECT transcript, lines, COALESCE(ended_at, placed_at) AS at FROM followup.call_results "
                           "WHERE key = %s", (key,)).fetchone()
    if not row:
        return None
    transcript = row["transcript"]
    if not vendor_spoke(transcript):
        _save_analysis(key, None, "skipped", "The vendor did not speak, nothing to analyse")
        return None
    try:
        result = normalise(ask_model(transcript, row["lines"], row["at"].astimezone(TIMEZONE).date()), row["lines"],
                           row["at"].astimezone(TIMEZONE).date())
    except Exception as e:
        log.exception("[%s] call %s: analysis failed", key)
        _save_analysis(key, None, "failed", f"{type(e).__name__}: {e}"[:500])
        return None
    _save_analysis(key, result, "ok", None)
    return result


def _save_analysis(key: str, result: dict | None, status: str, error: str | None) -> None:
    with store.connect() as conn:
        conn.execute("UPDATE followup.call_results SET analysis = %s, analysis_status = %s, analysis_error = %s "
                     "WHERE key = %s", (Jsonb(result) if result else None, status, error, key))


# ---------- reading ----------

LIST_COLUMNS = ("key, frejun_call_id, vendor_code, vendor_name, mobile, status, placed_at, connected_at, ended_at, "
                "duration_seconds, recording_url IS NOT NULL AS has_recording, analysis_status, analysis_error, analysis")


def _out(r: dict) -> dict:
    for k in ("placed_at", "connected_at", "ended_at"):
        if r.get(k):
            r[k] = r[k].astimezone(TIMEZONE).isoformat()
    return r


def mask(mobile: str) -> str:
    return f"{mobile[:2]}******{mobile[-3:]}" if len(mobile) > 6 else mobile


def list_calls(vendor_code: str | None = None, limit: int = 200) -> list[dict]:
    """Newest first. Each row has `analysis` (or None) so the page can show reasons and dates in the list."""
    with store.connect() as conn:
        store.ensure_followup_schema(conn)
        rows = conn.execute(
            f"SELECT {LIST_COLUMNS} FROM followup.call_results WHERE (%s::text IS NULL OR vendor_code = %s) "
            "ORDER BY placed_at DESC LIMIT %s", (vendor_code, vendor_code, limit)).fetchall()
    return [_out({**r, "mobile": mask(r["mobile"])}) for r in rows]


def get_call(key: str) -> dict | None:
    with store.connect() as conn:
        r = conn.execute("SELECT * FROM followup.call_results WHERE key = %s", (key,)).fetchone()
    if not r:
        return None
    r = _out({**r, "mobile": mask(r["mobile"])})
    r["has_recording"] = bool(r["recording_url"])
    return r


def insights(days: int = 30) -> dict:
    """What the calls of the last `days` days say: outcomes, delay reasons, and the commitments that were captured."""
    since = datetime.now(TIMEZONE) - timedelta(days=days)
    with store.connect() as conn:
        store.ensure_followup_schema(conn)
        rows = conn.execute("SELECT status, analysis_status, analysis FROM followup.call_results WHERE placed_at >= %s",
                            (since,)).fetchall()
    reasons: dict[str, int] = {}
    committed = lines_total = review = 0
    for r in rows:
        a = r["analysis"]
        if not a:
            continue
        review += 1 if a.get("needs_review") else 0
        for l in a.get("lines", []):
            lines_total += 1
            committed += 1 if l.get("committed_date") else 0
            if l.get("reason_category") not in (None, "none"):
                reasons[l["reason_category"]] = reasons.get(l["reason_category"], 0) + 1
    return {
        "days": days,
        "calls": len(rows),
        "answered": sum(1 for r in rows if r["status"] == "completed"),
        "no_response": sum(1 for r in rows if r["status"] == "no_response"),
        "failed": sum(1 for r in rows if r["status"] == "failed"),
        "analysed": sum(1 for r in rows if r["analysis_status"] == "ok"),
        "analysis_failed": sum(1 for r in rows if r["analysis_status"] == "failed"),
        "lines_discussed": lines_total,
        "lines_with_date": committed,
        "needs_review": review,
        "reasons": sorted(({"reason": k, "lines": v} for k, v in reasons.items()), key=lambda x: -x["lines"]),
    }
