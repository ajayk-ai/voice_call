"""Per-vendor escalation trace for the admin page: where a vendor is in
Scenario 1 (Day 0) -> Scenario 2 (Day 1) -> Scenario 3 (Day 2 call) -> Day 3 meeting, who was messaged,
and when the next step is due."""

import re
import threading
import time
from datetime import date, datetime, timedelta

from backend.core.env import TIMEZONE, env_int
from backend.po_followup.domain.escalation import (
    CONTACT_FIELDS, MAX_LEVEL, apply_phone_master, cycle_starts, last_alert_time, line_key, map_row, meeting_dates, num,
    pretty, txt,
)
from backend.po_followup.config import Settings
from backend.po_followup.db import store
from backend.po_followup.domain.overdue import next_period_start, period_start
from backend.po_followup.services import followup as service

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
        return datetime.strptime(txt(ts), "%Y-%m-%d %H:%M:%S").replace(tzinfo=TIMEZONE).isoformat()
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
            "pg": txt(r.get("purchasingGroup")), "buyer": buyer_name(r.get("buyerEmail")),
        })
    return vendors


def buyer_name(email) -> str:
    value = txt(email)
    return "" if value == "#N/A" else value.split("@")[0]


def line_groups(vendor: dict) -> dict:
    """A vendor's overdue lines per purchasing group, and its POs and buyers. One vendor often supplies several
    groups. With nothing overdue, the group and buyer come from its Master contacts."""
    lines = vendor.get("lines", {}).values()
    c = vendor.get("contacts", {})
    by_pg: dict[str, dict] = {}
    for l in lines:
        if l["pg"]:
            pg = by_pg.setdefault(l["pg"], {"lines": 0, "max_od": 0, "pos": 0})
            pg["lines"] += 1
            pg["max_od"] = max(pg["max_od"], l["od"])
    for g, pg in by_pg.items():
        pg["pos"] = len({l["po"] for l in lines if l["pg"] == g})
    if not by_pg and c.get("purchasingGroup"):
        by_pg[c["purchasingGroup"]] = {"lines": 0, "max_od": 0, "pos": 0}
    buyers = {l["buyer"] for l in lines if l["buyer"]} or {buyer_name(c.get("buyerEmail"))} - {""}
    return {"by_pg": by_pg, "buyers": sorted(buyers), "po_count": len({l["po"] for l in lines})}


def cycle_info(start: date | None, reached: date | None, cycle_days: int, today: date) -> dict:
    """Where a vendor is in its cycle. `restart_on`, the day it starts again from Scenario 1, is known once it has
    reached Scenario 4."""
    return {"cycle_start": start.isoformat() if start else None, "cycle_day": (today - start).days + 1 if start else None,
            "cycle_days": cycle_days, "restart_on": (reached + timedelta(days=cycle_days)).isoformat() if reached else None}


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
        at = last_alert_time(t, TIMEZONE)
        if level is None or lv > level or (lv == level and at and (last is None or at > last)):
            level, last = lv, at
    return level, last


def since_restart(alert_rows: list[dict], fresh_start: str) -> list[dict]:
    """The Alert_Log rows since 'Restart from Scenario 1' was last pressed."""
    return [r for r in alert_rows if txt(r.get("Timestamp")) >= fresh_start]


def current_cycle(alert_rows: list[dict]) -> list[dict]:
    """The Alert_Log rows (oldest first) since the vendor last went back to Scenario 1."""
    resets = [i for i, r in enumerate(alert_rows) if r.get("Status") == "CYCLE_RESET"]
    return alert_rows[resets[-1] + 1:] if resets else alert_rows


def journey(level: int | None, last_alert: datetime | None, alert_rows: list[dict], period_minutes: float,
            now: datetime) -> list[dict]:
    """The four steps with state done / next / later, their messages (newest first) and when the next is due.
    The next scenario needs one more overdue check: it is due from the start of the check period after the
    period of the last alert (provided the lines are still overdue then)."""
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
            due = (None if level is None or last_alert is None
                   else next_period_start(period_start(last_alert, period_minutes), period_minutes))
        else:
            state, due = "later", None
        steps.append({**step, "state": state, "done_at": done_at if state == "done" else None,
                      "due_at": due.isoformat() if due else None,
                      "due_now": state == "next" and (due is None or now >= due),
                      # one more overdue check (one check period) after the previous step
                      "wait_minutes": float(period_minutes) if lv > 0 else None,
                      "messages": msgs[:20]})
    return steps


def next_step_note(steps: list[dict], pending_lines: int, chosen: bool, s: dict, next_auto: datetime | None,
                   restart_on: date | None = None) -> str:
    """One sentence saying what happens next for this vendor, in plain words."""
    nxt = next((st for st in steps if st["state"] == "next"), None)
    if nxt is None:
        return ("All steps done: the meeting stage is reached. "
                + (f"Lines still overdue on {pretty(restart_on)} start again from Scenario 1." if restart_on
                   else "Nothing more is sent for these lines."))
    if pending_lines == 0:
        return "No pending lines (OD Days > 0) for this vendor, so nothing is sent. Open lines are marked CLEARED on the next run."
    if vendor_filter(s) is not None and not chosen:
        return f"{nxt['name']} is next, but this vendor is not selected for follow-up (dashboard), so runs skip it."
    how = ("goes out on the next automatic run" if s.get("auto_enabled") and next_auto else
           "goes out when you start a run (automatic runs are off)")
    if nxt["due_now"]:
        return f"{nxt['name']} ({nxt['title']}) is due now and {how}."
    return f"{nxt['name']} ({nxt['title']}) is next. Once its wait is over it {how}."


# ---------- loaders (database) ----------

# Master / Phone_Master change only on a sheet sync or a manual edit, so a short cache keeps the page quick
SOURCE_CACHE_SECONDS = env_int("FOLLOWUP_SOURCE_CACHE_SECONDS", 20)
_cache: dict[tuple, tuple[float, list]] = {}
_cache_lock = threading.Lock()


def source_records(title: str) -> list[dict]:
    with _cache_lock:
        hit = _cache.get((title))
        if hit and time.monotonic() - hit[0] < SOURCE_CACHE_SECONDS:
            return hit[1]
    rows = store.PgStore().read_records(title)
    with _cache_lock:
        _cache[(title)] = (time.monotonic(), rows)
    return rows


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _settings_and_data():
    return service.get_settings(), source_records("Master"), store.PgStore().read_records("Line_Tracker")


def chosen_codes(s: dict) -> set[str]:
    """The vendors chosen in Vendor Control."""
    return {c.strip() for c in s.get("vendor_codes", "").split(",") if c.strip()}


def vendor_filter(s: dict) -> set[str] | None:
    """The only vendors runs follow up, or None for every vendor (same rule as config.Settings.vendor_filter)."""
    return None if s.get("vendor_scope", "all") == "all" else chosen_codes(s)


def vendor_row(code: str, vendor: dict, level: int | None, last: datetime | None, chosen: bool, cycle: dict) -> dict:
    """One vendor of the trace list."""
    return {
        "code": code, "name": vendor["name"], "pending_lines": len(vendor["lines"]),
        "max_od": max((l["od"] for l in vendor["lines"].values()), default=0),
        "level": level, "step": STEPS[level]["name"] if level is not None else None,
        "last_alert_at": last.isoformat() if last else None, "chosen": chosen,
        **line_groups(vendor), **cycle,
    }


def idle_vendor(code: str, name: str, vendor: dict | None, cycle_days: int) -> dict:
    """A chosen vendor with nothing overdue and nothing open."""
    return vendor_row(code, {**(vendor or {}), "name": name, "lines": {}}, None, None, True,
                      cycle_info(None, None, cycle_days, datetime.now(TIMEZONE).date()))


def vendor_list() -> list[dict]:
    """Every vendor with pending lines or open tracker lines, furthest escalated first."""
    s, master, tracker = _settings_and_data()
    vendors = index_vendors(master)
    by_vendor: dict[str, list] = {}
    for t in tracker:
        by_vendor.setdefault(txt(t.get("Vendor Code")), []).append(t)
    chosen = chosen_codes(s)
    starts, reached = cycle_starts(tracker), meeting_dates(tracker)
    cycle_days = Settings.from_env().cycle_days
    today = datetime.now(TIMEZONE).date()
    out = []
    for code in set(vendors) | {c for c, rows in by_vendor.items() if c and any(_active(t) for t in rows)}:
        v = vendors.get(code) or {
            "name": next((txt(t.get("Vendor Name")) for t in by_vendor[code]), code), "lines": {}}
        level, last = position(by_vendor.get(code, []))
        if not v["lines"] and level is None:
            continue
        out.append(vendor_row(code, v, level, last, code in chosen, cycle_info(starts.get(code), reached.get(code), cycle_days, today)))
    out.sort(key=lambda v: (-(v["level"] if v["level"] is not None else -1), -v["max_od"], v["name"]))
    return out


def vendor_trace(code: str) -> dict | None:
    s, master, tracker = _settings_and_data()
    vendor = index_vendors(master).get(code)
    tracker_rows = [t for t in tracker if txt(t.get("Vendor Code")) == code]
    alert_rows = since_restart(store.vendor_rows("Alert_Log", code), s["fresh_start_at"])
    if vendor is None and not tracker_rows and not alert_rows:
        return None
    if vendor is None:
        vendor = {"code": code, "name": next((txt(t.get("Vendor Name")) for t in tracker_rows), code),
                  "contacts": {}, "lines": {}}
    now = datetime.now(TIMEZONE)
    level, last = position(tracker_rows)
    steps = journey(level, last, current_cycle(alert_rows), s["check_period_minutes"], now)
    settings = Settings.from_env()
    cycle = cycle_info(cycle_starts(tracker_rows).get(code), meeting_dates(tracker_rows).get(code), settings.cycle_days, now.date())
    next_auto = service.next_auto_run(s, now)
    chosen = code in chosen_codes(s)

    tracked = {txt(t.get("Line Key")): t for t in tracker_rows}
    lines = []
    for key in list(vendor["lines"]) + [k for k in tracked if k not in vendor["lines"]]:
        l, t = vendor["lines"].get(key), tracked.get(key, {})
        at = last_alert_time(t, TIMEZONE) if t else None
        lines.append({
            "key": key, "po": l["po"] if l else txt(t.get("PO No")), "desc": l["desc"] if l else txt(t.get("Material")),
            "pending": l["pending"] if l else "", "od": l["od"] if l else txt(t.get("OD Days")),
            "level": _level(t.get("Escalation Level")) if t and _active(t) else None,
            "status": txt(t.get("Status")) or ("PENDING" if l else ""), "pg": l["pg"] if l else "",
            "last_alert_at": at.isoformat() if at else None,
        })

    phone_master = source_records("Phone_Master")
    return {
        "code": code, "name": vendor["name"], "now": now.isoformat(),
        "pending_lines": len(vendor["lines"]), "level": level, "chosen": chosen,
        "contacts": contacts(vendor, phone_master, settings.scm_hod_mobile),
        **line_groups(vendor), **cycle,
        "cycle_number": 1 + sum(r.get("Status") == "CYCLE_RESET" for r in alert_rows),
        "steps": steps,
        "next_note": next_step_note(steps, len(vendor["lines"]), chosen, s, next_auto,
                                    date.fromisoformat(cycle["restart_on"]) if cycle["restart_on"] else None),
        "next_auto_run": next_auto.isoformat() if next_auto else None,
        "lines": lines,
        "call_tasks": store.vendor_rows("Call_Tasks", code),
        "meeting_tasks": store.vendor_rows("Meeting_Tasks", code),
        "max_level": MAX_LEVEL,
    }
