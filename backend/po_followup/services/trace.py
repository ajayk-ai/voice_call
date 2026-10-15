"""Per-vendor escalation trace for the admin page: where a vendor is in
Scenario 1 (Day 0) -> Scenario 2 (Day 1) -> Scenario 3 (Day 2 call) -> Day 3 meeting, who was messaged,
and when the next step is due."""

import re
import threading
import time
from datetime import datetime, timedelta

from backend.po_followup.domain.escalation import (
    CONTACT_FIELDS, MAX_LEVEL, apply_phone_master, interval_elapsed, last_alert_time, line_key, map_row, num, txt,
)
from backend.po_followup.services.run import ZONE

# One entry per escalation level; "number" is the Phone_Master column whose number is used at that step
STEPS = [
    {"level": 0, "name": "Scenario 1", "title": "Vendor alert", "number": "scenario1",
     "who": "Vendor planner + coordinator", "action": "WhatsApp with the overdue PO lines"},
    {"level": 1, "name": "Scenario 2", "title": "Vendor management escalation", "number": "scenario2",
     "who": "Vendor management", "action": "WhatsApp: no response, please intervene"},
    {"level": 2, "name": "Scenario 3", "title": "Buyer call task", "number": "scenario3",
     "who": "Buyer", "action": "Voice agent calls the Scenario 3 number + WhatsApp to the buyer + Call_Tasks row"},
    {"level": 3, "name": "Day 3", "title": "Management meeting", "number": None,
     "who": "Buyer + SCM HOD", "action": "Google Meet invite + WhatsApp + Meeting_Tasks row"},
]
# Alert_Log statuses that mean the step went out (DRY_RUN: simulated, no WhatsApp)
DONE_STATUSES = {"SENT", "DRY_RUN", "MEETING_TASK_CREATED"}


def _iso(ts: str) -> str | None:
    """'2026-10-09 10:15:00' (IST, as written in the tables) -> ISO 8601 with offset, for the browser."""
    try:
        return datetime.strptime(txt(ts), "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZONE).isoformat()
    except ValueError:
        return None


def _level(v) -> int | None:
    m = re.search(r"\d+", txt(v))
    return int(m.group()) if m else None


def _active(t: dict) -> bool:
    return not re.search(r"CLEARED", txt(t.get("Status")), re.I)


# ---------- master data per vendor ----------

def index_vendors(master: list[dict]) -> dict[str, dict]:
    """Vendor code -> name, contacts from Master, and pending lines (OD Days > 0), same rules as the run."""
    vendors: dict[str, dict] = {}
    for record in master:
        r = map_row(record)
        po = txt(r.get("poNumber"))
        code = txt(r.get("vendorCode")) or txt(r.get("vendorName"))
        if not po or not code:
            continue
        v = vendors.setdefault(code, {"code": code, "name": txt(r.get("vendorName")) or code, "contacts": {},
                                      "lines": {}})
        for c in CONTACT_FIELDS:
            value = txt(r.get(c))
            if not v["contacts"].get(c) and value and value != "#N/A":
                v["contacts"][c] = value
        od = num(r.get("odDays"))
        if od is None or od <= 0:
            continue
        key = line_key(po, txt(r.get("poItem")), txt(r.get("schedLine")))
        v["lines"].setdefault(key, {
            "key": key, "po": po, "item": txt(r.get("poItem")), "sched": txt(r.get("schedLine")),
            "desc": txt(r.get("materialDescription")) or txt(r.get("material")),
            "pending": f"{txt(r.get('pendingQty'))} {txt(r.get('uom'))}".strip(), "od": od,
        })
    return vendors


def contacts(vendor: dict, phone_master: list[dict], scm_hod_mobile: str) -> dict:
    """The numbers each step uses: Phone_Master Scenario 1/2/3, falling back to Master like the run does."""
    c = vendor["contacts"]
    g = apply_phone_master([{
        "vendorCode": vendor["code"],
        "vendorPlannerMobileRaw": c.get("vendorPlannerMobile", ""),
        "vendorCoordMobileRaw": c.get("vendorCoordMobile", ""),
        "vendorMgmtMobileRaw": c.get("vendorMgmtMobile", ""),
    }], phone_master)[0]
    return {
        "scenario1": g["scenario1Raw"], "scenario2": g["scenario2Raw"], "scenario3": g["scenario3Raw"],
        "source": g["phoneSource"], "buyer_mobile": c.get("buyerMobile", ""), "buyer_email": c.get("buyerEmail", ""),
        "scm_hod_mobile": scm_hod_mobile,
    }


# ---------- where a vendor is ----------

def position(tracker_rows: list[dict]) -> tuple[int | None, datetime | None]:
    """Highest escalation level among the vendor's open tracker lines, and when that level was alerted."""
    level, last = None, None
    for t in tracker_rows:
        lv = _level(t.get("Escalation Level"))
        if not _active(t) or lv is None:
            continue
        at = last_alert_time(t, ZONE)
        if level is None or lv > level or (lv == level and at and (last is None or at > last)):
            level, last = lv, at
    return level, last


def journey(level: int | None, last_alert: datetime | None, alert_rows: list[dict], intervals: list,
            now: datetime) -> list[dict]:
    """The four steps with state done / next / later, their messages (newest first) and when the next is due."""
    steps = []
    for step in STEPS:
        lv = step["level"]
        msgs = [{
            "at": _iso(r.get("Timestamp")), "role": r.get("Recipient Role"), "name": r.get("Recipient Name"),
            "mobile": r.get("Mobile"), "status": r.get("Status"), "error": r.get("Error"), "message": r.get("Message"),
            "triggered_by": r.get("Triggered By"),
        } for r in reversed(alert_rows) if _level(r.get("Escalation Level")) == lv]
        done_at = next((m["at"] for m in msgs if m["status"] in DONE_STATUSES), None)
        if level is not None and lv <= level:
            state, due = "done", None
        elif lv == (0 if level is None else level + 1):
            state = "next"
            due = None if level is None or last_alert is None else last_alert + timedelta(minutes=float(intervals[level]))
        else:
            state, due = "later", None
        steps.append({**step, "state": state, "done_at": done_at if state == "done" else None,
                      "due_at": due.isoformat() if due else None,
                      "due_now": state == "next" and (level is None or interval_elapsed(last_alert, now, float(intervals[level]))),
                      # wait after the previous step before this one can go out
                      "wait_minutes": float(intervals[lv - 1]) if lv > 0 else None,
                      "messages": msgs[:20]})
    return steps


def next_step_note(steps: list[dict], pending_lines: int, in_pilot: bool, s: dict, next_auto: datetime | None) -> str:
    """One sentence saying what happens next for this vendor, in plain words."""
    nxt = next((st for st in steps if st["state"] == "next"), None)
    if nxt is None:
        return "All steps done: the meeting stage is reached. Nothing more is sent for these lines."
    if pending_lines == 0:
        return "No pending lines (OD Days > 0) for this vendor, so nothing is sent. Open lines are marked CLEARED on the next run."
    if s.get("pilot_mode") and not in_pilot:
        return f"{nxt['name']} is next, but pilot mode is on and this vendor is not in the pilot vendor list, so runs skip it."
    how = ("goes out on the next automatic run" if s.get("auto_enabled") and next_auto else
           "goes out when you start a run (automatic runs are off)")
    if nxt["due_now"]:
        return f"{nxt['name']} ({nxt['title']}) is due now and {how}."
    return f"{nxt['name']} ({nxt['title']}) is next. Once its wait is over it {how}."


# ---------- loaders (database) ----------

# Master / Phone_Master change only on a sheet sync or a manual edit, so a short cache keeps the page quick
SOURCE_CACHE_SECONDS = 20
_cache: dict[tuple, tuple[float, list]] = {}
_cache_lock = threading.Lock()


def source_records(env: str, title: str) -> list[dict]:
    from backend.po_followup.db import store

    with _cache_lock:
        hit = _cache.get((env, title))
        if hit and time.monotonic() - hit[0] < SOURCE_CACHE_SECONDS:
            return hit[1]
    rows = store.PgStore(env).read_records(title)
    with _cache_lock:
        _cache[(env, title)] = (time.monotonic(), rows)
    return rows


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _settings_and_data(env: str):
    from backend.po_followup.services import followup as service
    from backend.po_followup.db import store

    return service.get_settings(env), source_records(env, "Master"), store.PgStore(env).read_records("Line_Tracker")


def _pilot_codes(s: dict) -> set[str]:
    return {c.strip() for c in s["pilot_vendor_codes"].split(",") if c.strip()}


def vendor_list(env: str) -> list[dict]:
    """Every vendor with pending lines or open tracker lines, furthest escalated first."""
    s, master, tracker = _settings_and_data(env)
    vendors = index_vendors(master)
    by_vendor: dict[str, list] = {}
    for t in tracker:
        by_vendor.setdefault(txt(t.get("Vendor Code")), []).append(t)
    pilot = _pilot_codes(s)
    out = []
    for code in set(vendors) | {c for c, rows in by_vendor.items() if c and any(_active(t) for t in rows)}:
        v = vendors.get(code) or {
            "name": next((txt(t.get("Vendor Name")) for t in by_vendor[code]), code), "lines": {}}
        level, last = position(by_vendor.get(code, []))
        if not v["lines"] and level is None:
            continue
        out.append({
            "code": code, "name": v["name"], "pending_lines": len(v["lines"]),
            "max_od": max((l["od"] for l in v["lines"].values()), default=0),
            "level": level, "step": STEPS[level]["name"] if level is not None else None,
            "last_alert_at": last.isoformat() if last else None, "in_pilot": code in pilot,
        })
    out.sort(key=lambda v: (-(v["level"] if v["level"] is not None else -1), -v["max_od"], v["name"]))
    return out


def vendor_trace(env: str, code: str) -> dict | None:
    from backend.po_followup.services import followup as service
    from backend.po_followup.db import store

    s, master, tracker = _settings_and_data(env)
    vendor = index_vendors(master).get(code)
    tracker_rows = [t for t in tracker if txt(t.get("Vendor Code")) == code]
    alert_rows = store.vendor_rows(env, "Alert_Log", code)
    if vendor is None and not tracker_rows and not alert_rows:
        return None
    if vendor is None:
        vendor = {"code": code, "name": next((txt(t.get("Vendor Name")) for t in tracker_rows), code),
                  "contacts": {}, "lines": {}}
    now = datetime.now(ZONE)
    level, last = position(tracker_rows)
    steps = journey(level, last, alert_rows, s["intervals_minutes"], now)
    next_auto = service.next_auto_run(env, s, now)
    in_pilot = code in _pilot_codes(s)

    tracked = {txt(t.get("Line Key")): t for t in tracker_rows}
    lines = []
    for key in list(vendor["lines"]) + [k for k in tracked if k not in vendor["lines"]]:
        l, t = vendor["lines"].get(key), tracked.get(key, {})
        at = last_alert_time(t, ZONE) if t else None
        lines.append({
            "key": key, "po": l["po"] if l else txt(t.get("PO No")), "desc": l["desc"] if l else txt(t.get("Material")),
            "pending": l["pending"] if l else "", "od": l["od"] if l else txt(t.get("OD Days")),
            "level": _level(t.get("Escalation Level")) if t else None,
            "status": txt(t.get("Status")) or ("PENDING" if l else ""),
            "last_alert_at": at.isoformat() if at else None,
        })

    phone_master = source_records(env, "Phone_Master")
    return {
        "code": code, "name": vendor["name"], "now": now.isoformat(),
        "pending_lines": len(vendor["lines"]), "level": level, "in_pilot": in_pilot,
        "contacts": contacts(vendor, phone_master, s["scm_hod_mobile"]),
        "steps": steps,
        "next_note": next_step_note(steps, len(vendor["lines"]), in_pilot, s, next_auto),
        "next_auto_run": next_auto.isoformat() if next_auto else None,
        "lines": lines,
        "call_tasks": store.vendor_rows(env, "Call_Tasks", code),
        "meeting_tasks": store.vendor_rows(env, "Meeting_Tasks", code),
        "max_level": MAX_LEVEL,
    }
