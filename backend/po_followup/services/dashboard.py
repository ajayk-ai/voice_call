"""Dashboard report: every overdue PO line in Master (OD Days > 0), the scenario its overdue streak confirms
(domain/overdue.py: overdue 1 check -> Scenario 1, 2 in a row -> Scenario 2, 3 -> Scenario 3, 4+ -> meeting) and
which scenario messages have actually gone out.

Read-only: nothing here sends messages or records checks. Shape: models.dashboard.DashboardData.
`build` is pure (tested without a database); `overview` loads the data for one environment.
"""

import re
from datetime import date, datetime, timedelta

from backend.po_followup.domain.escalation import last_alert_time, line_key, map_row, num, parse_date, pretty, txt
from backend.po_followup.domain.overdue import LEVELS, confirmed_level, history, period_start, streaks
from backend.po_followup.services.run import ZONE
from backend.po_followup.services.trace import DONE_STATUSES, _active, _iso, _level, vendor_filter

SCENARIOS = [
    {"name": "Scenario 1", "title": "Vendor reminder", "who": "Vendor planner and coordinator get a WhatsApp"},
    {"name": "Scenario 2", "title": "Vendor management", "who": "Vendor management is asked to step in"},
    {"name": "Scenario 3", "title": "Buyer call", "who": "The buyer calls the vendor (with the voice agent)"},
]
# One tab per confirmed scenario; index = confirmed escalation level
STAGES = ["Scenario 1", "Scenario 2", "Scenario 3", "Management meeting"]
CLEARED_RECENT_DAYS = 7
HISTORY_CHECKS = 7


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


def scenario_states(level: int | None, confirmed: int, last: datetime | None, first: date | None, sent: dict,
                    period: datetime, period_minutes: float, on_hold: bool) -> list[dict]:
    """Scenario 1-3 for one line.

    done       the message went out (at = when)
    due        confirmed by the streak and next in line: goes out on the next run
    on_hold    next in line but this vendor is not selected for follow-up
    queued     confirmed, but waits for the scenario before it to go out first (scenarios are never skipped)
    upcoming   not confirmed yet (at = the check that confirms it, if the line is still overdue then)
    """
    out = []
    nxt = 0 if level is None else level + 1
    for s in range(len(SCENARIOS)):
        if level is not None and s <= level:
            row = _sent_for_line(sent.get(s, []), first)
            at = _iso(row["first_at"]) if row else (last.isoformat() if last and s == level else None)
            out.append({"state": "done", "at": at, "simulated": bool(row and row.get("simulated"))})
        elif s > confirmed:
            out.append({"state": "upcoming", "at": (period + timedelta(minutes=period_minutes * (s - confirmed))).isoformat()})
        elif s == nxt:
            out.append({"state": "on_hold" if on_hold else "due", "at": None})
        else:
            out.append({"state": "queued", "at": None})
    return out


def build(master: list[dict], tracker: list[dict], alert_days: list[dict], earlier_checks: list[tuple],
          settings: dict, now: datetime) -> dict:
    """`earlier_checks`: [(period start, {overdue line keys})] recorded before the current period, newest first.
    The current period's check is taken from `master` as it is now."""
    tracked = {txt(t.get("Line Key")): t for t in tracker if txt(t.get("Line Key"))}
    sent = _sent_index(alert_days)
    pilot = vendor_filter(settings)  # None: every vendor is followed up
    period_minutes = float(settings["check_period_minutes"])
    period = period_start(now, period_minutes)
    pos: dict[str, dict] = {}
    current: dict[str, dict] = {}
    rows: list[tuple] = []

    for record in master:
        r = map_row(record)
        po = txt(r.get("poNumber"))
        code = txt(r.get("vendorCode")) or txt(r.get("vendorName"))
        od = num(r.get("odDays"))
        if not po or not code or od is None or od <= 0:
            continue
        key = line_key(po, txt(r.get("poItem")), txt(r.get("schedLine")))
        if key in current:
            continue
        current[key] = {"vendor_code": code, "od": od}
        rows.append((key, po, code, od, r))

    checks = [(period, set(current))] + list(earlier_checks)
    line_streaks = streaks(checks, current)
    stage_counts = [0] * LEVELS

    for key, po, code, od, r in rows:
        t = tracked.get(key)
        t = t if t and _active(t) else None
        level = _level(t.get("Escalation Level")) if t else None
        last = last_alert_time(t, ZONE) if t else None
        first = parse_date(t.get("First Alert Date")) if t else None
        on_hold = pilot is not None and code not in pilot
        streak = line_streaks[key]
        stage = confirmed_level(streak)
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
            "stage": stage, "stage_name": STAGES[stage], "streak": streak, "on_hold": on_hold,
            "first_alerted": pretty(first),
            "scenarios": scenario_states(level, stage, last, first, sent.get(code, {}), period, period_minutes, on_hold),
            "meeting": level is not None and level >= 3,
            "history": history(checks, key, HISTORY_CHECKS),
        })

    cutoff = (now.date() - timedelta(days=CLEARED_RECENT_DAYS)).isoformat()
    cleared_recent = sum(1 for t in tracker if not _active(t) and txt(t.get("Cleared Date")) >= cutoff)
    out = sorted(pos.values(), key=lambda p: (-p["max_od"], p["vendor_name"], p["po"]))
    for p in out:
        p["lines"].sort(key=lambda l: (-l["od"], l["key"]))
    return {
        "now": now.isoformat(),
        "pilot_mode": bool(settings.get("pilot_mode")),
        "selected_only": pilot is not None,
        "selected_vendors": len(pilot or ()),
        "check_period_minutes": period_minutes,
        "last_check": earlier_checks[0][0].isoformat() if earlier_checks else None,
        "scenarios": SCENARIOS,
        "stages": STAGES,
        "summary": {
            "lines": len(current), "pos": len(out), "vendors": len({p["vendor_code"] for p in out}),
            "max_od": max((p["max_od"] for p in out), default=0),
            "over_30": sum(1 for p in out for l in p["lines"] if l["od"] > 30),
            "by_stage": stage_counts, "cleared_recent": cleared_recent, "cleared_days": CLEARED_RECENT_DAYS,
            "earlier_checks": len(earlier_checks),
        },
        "pos": out,
    }


def overview(env: str) -> dict:
    from backend.po_followup.db import store
    from backend.po_followup.services import followup as service
    from backend.po_followup.services import trace

    now = datetime.now(ZONE)
    settings = service.get_settings(env)
    pg = store.PgStore(env)
    period = period_start(now, float(settings["check_period_minutes"]))
    earlier = pg.recent_checks(period - timedelta(microseconds=1), HISTORY_CHECKS - 1)
    data = build(trace.source_records(env, "Master"), pg.read_records("Line_Tracker"),
                 store.alert_days(env, DONE_STATUSES), earlier, settings, now)
    return {"env": env, "synced_at": store.stats(env).get("master_synced_at"), **data}
