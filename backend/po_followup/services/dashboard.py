"""Dashboard report: every overdue PO line in Master (OD Days > 0) and where it stands in
Scenario 1 (vendor reminder) -> Scenario 2 (vendor management) -> Scenario 3 (buyer call) -> meeting.

Read-only: nothing here sends messages or changes follow-up state. Shape: models.dashboard.DashboardData.
`build` is pure (tested without a database); `overview` loads the data for one environment.
"""

import re
from datetime import date, datetime, timedelta

from backend.po_followup.domain.escalation import interval_elapsed, last_alert_time, line_key, map_row, num, parse_date, pretty, txt
from backend.po_followup.services.run import ZONE
from backend.po_followup.services.trace import DONE_STATUSES, _active, _iso, _level, _pilot_codes

SCENARIOS = [
    {"name": "Scenario 1", "title": "Vendor reminder", "who": "Vendor planner and coordinator get a WhatsApp"},
    {"name": "Scenario 2", "title": "Vendor management", "who": "Vendor management is asked to step in"},
    {"name": "Scenario 3", "title": "Buyer call", "who": "The buyer calls the vendor (with the voice agent)"},
]
# Index = escalation level + 1 (no level yet = 0)
STAGES = ["Not yet notified", "Vendor reminded", "With vendor management", "Buyer call", "Management meeting"]
CLEARED_RECENT_DAYS = 7


def _vendor_code(label: str) -> str:
    """Alert_Log 'Vendor' is 'Vendor Name (400186)'."""
    m = re.search(r"\(([^()]*)\)\s*$", txt(label))
    return m.group(1).strip() if m else txt(label)


def _sent_index(alert_days: list[dict]) -> dict[str, dict[int, list[dict]]]:
    """vendor code -> level -> days a message went out at that level, oldest first."""
    out: dict[str, dict[int, list[dict]]] = {}
    for r in alert_days:
        lv = _level(r.get("level"))
        if lv is not None:
            out.setdefault(_vendor_code(r.get("vendor")), {}).setdefault(lv, []).append(r)
    return out


def _sent_for_line(sent: list[dict], since: date | None) -> dict | None:
    """The first message at this level on or after the line was first alerted (earlier ones were for other lines)."""
    since_iso = since.isoformat() if since else ""
    return next((r for r in sent if txt(r.get("alert_date")) >= since_iso), None)


def scenario_states(level: int | None, last: datetime | None, first: date | None, sent: dict, intervals: list,
                    now: datetime, on_hold: bool) -> list[dict]:
    """Scenario 1-3 for one line: done (with when), due (now or at a time), on_hold (pilot skips it) or upcoming."""
    out = []
    nxt = 0 if level is None else level + 1
    for s in range(len(SCENARIOS)):
        if level is not None and s <= level:
            row = _sent_for_line(sent.get(s, []), first)
            at = _iso(row["first_at"]) if row else (last.isoformat() if last and s == level else None)
            out.append({"state": "done", "at": at, "simulated": bool(row and row.get("simulated"))})
        elif s == nxt:
            due = last + timedelta(minutes=float(intervals[level])) if level is not None and last else None
            if on_hold:
                state = "on_hold"
            elif level is None or interval_elapsed(last, now, float(intervals[level])):
                state = "due"
            else:
                state = "scheduled"
            out.append({"state": state, "at": due.isoformat() if due and state == "scheduled" else None})
        else:
            out.append({"state": "upcoming", "at": None})
    return out


def build(master: list[dict], tracker: list[dict], alert_days: list[dict], settings: dict, now: datetime) -> dict:
    tracked = {txt(t.get("Line Key")): t for t in tracker if txt(t.get("Line Key"))}
    sent = _sent_index(alert_days)
    pilot = _pilot_codes(settings) if settings.get("pilot_mode") else None
    intervals = settings["intervals_minutes"]
    pos: dict[str, dict] = {}
    seen: set[str] = set()
    stage_counts = [0] * len(STAGES)

    for record in master:
        r = map_row(record)
        po = txt(r.get("poNumber"))
        code = txt(r.get("vendorCode")) or txt(r.get("vendorName"))
        od = num(r.get("odDays"))
        if not po or not code or od is None or od <= 0:
            continue
        key = line_key(po, txt(r.get("poItem")), txt(r.get("schedLine")))
        if key in seen:
            continue
        seen.add(key)

        t = tracked.get(key)
        t = t if t and _active(t) else None
        level = _level(t.get("Escalation Level")) if t else None
        last = last_alert_time(t, ZONE) if t else None
        first = parse_date(t.get("First Alert Date")) if t else None
        on_hold = pilot is not None and code not in pilot
        stage = 0 if level is None else min(level + 1, len(STAGES) - 1)
        stage_counts[stage] += 1

        email = txt(r.get("buyerEmail"))
        p = pos.setdefault(po, {
            "po": po, "vendor_code": code, "vendor_name": txt(r.get("vendorName")) or code,
            "buyer": email.split("@")[0] if email else "", "purchasing_group": txt(r.get("purchasingGroup")),
            "po_date": pretty(parse_date(r.get("poDate"))), "max_od": 0, "stage": 0, "lines": [],
        })
        p["max_od"] = max(p["max_od"], od)
        p["stage"] = max(p["stage"], stage)
        p["lines"].append({
            "key": key, "item": txt(r.get("poItem")), "sched": txt(r.get("schedLine")),
            "material": txt(r.get("material")), "desc": txt(r.get("materialDescription")),
            "pending": txt(r.get("pendingQty")), "uom": txt(r.get("uom")), "od": od,
            "due": pretty(now.date() - timedelta(days=od)),  # Del.Date cells are unreliable, as in the run
            "stage": stage, "stage_name": STAGES[stage], "on_hold": on_hold,
            "first_alerted": pretty(first),
            "scenarios": scenario_states(level, last, first, sent.get(code, {}), intervals, now, on_hold),
            "meeting": level is not None and level >= 3,
        })

    cutoff = (now.date() - timedelta(days=CLEARED_RECENT_DAYS)).isoformat()
    cleared_recent = sum(1 for t in tracker if not _active(t) and txt(t.get("Cleared Date")) >= cutoff)
    out = sorted(pos.values(), key=lambda p: (-p["max_od"], p["vendor_name"], p["po"]))
    for p in out:
        p["lines"].sort(key=lambda l: (-l["od"], l["key"]))
    return {
        "now": now.isoformat(),
        "pilot_mode": bool(settings.get("pilot_mode")),
        "scenarios": SCENARIOS,
        "stages": STAGES,
        "summary": {
            "lines": len(seen), "pos": len(out), "vendors": len({p["vendor_code"] for p in out}),
            "max_od": max((p["max_od"] for p in out), default=0),
            "over_30": sum(1 for p in out for l in p["lines"] if l["od"] > 30),
            "by_stage": stage_counts, "cleared_recent": cleared_recent, "cleared_days": CLEARED_RECENT_DAYS,
        },
        "pos": out,
    }


def overview(env: str) -> dict:
    from backend.po_followup.services import followup as service
    from backend.po_followup.db import store
    from backend.po_followup.services import trace

    now = datetime.now(ZONE)
    data = build(trace.source_records(env, "Master"), store.PgStore(env).read_records("Line_Tracker"),
                 store.alert_days(env, DONE_STATUSES), service.get_settings(env), now)
    return {"env": env, "synced_at": store.stats(env).get("master_synced_at"), **data}
