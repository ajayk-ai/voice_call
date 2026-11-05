"""Confirming a line's scenario from daily overdue checks. Pure functions, no I/O.

Every run checks Master once per *check period* (production: one IST calendar day; test: a few minutes, so a
whole escalation can be watched quickly) and records which lines were overdue (OD Days > 0) in that check.

A line's *streak* is the number of checks in a row, newest first, in which it was overdue:

    check:     Mon  Tue  Wed  Thu  Fri
    overdue:   yes  yes  yes  yes  no
    streak:     1    2    3    4    0  (cleared; overdue again later -> starts at 1)

The streak confirms the scenario: 1 -> Scenario 1, 2 -> Scenario 2, 3 -> Scenario 3, 4 or more -> management
meeting. A period in which no check ran (server off, holiday) neither counts nor breaks the streak.
Only checks of the line's current cycle count, so a vendor sent back to Scenario 1 climbs one scenario per check again.
"""

from datetime import datetime, timedelta

from backend.po_followup.domain.escalation import line_key, map_row, num, txt

DAY_MINUTES = 1440
# Streak at which each level is confirmed: index = escalation level (0 = Scenario 1 ... 3 = meeting)
LEVELS = 4


def period_start(now: datetime, minutes: float) -> datetime:
    """Start of the check period containing `now`: IST midnight for daily checks, else a slot of `minutes`
    counted from midnight (now must be timezone-aware, in the business time zone)."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if minutes >= DAY_MINUTES:
        return midnight
    slot = timedelta(minutes=minutes)
    return midnight + slot * int((now - midnight) / slot)


def next_period_start(start: datetime, minutes: float) -> datetime:
    return start + timedelta(minutes=minutes)


def overdue_now(master_records: list[dict]) -> dict[str, dict]:
    """Line key -> {vendor_code, od} for every Master line with OD Days > 0 (the same pending rule as the run)."""
    out: dict[str, dict] = {}
    for record in master_records:
        r = map_row(record)
        po = txt(r.get("poNumber"))
        vendor = txt(r.get("vendorCode")) or txt(r.get("vendorName"))
        od = num(r.get("odDays"))
        if not po or not vendor or od is None or od <= 0:
            continue
        out.setdefault(line_key(po, txt(r.get("poItem")), txt(r.get("schedLine"))), {"vendor_code": vendor, "od": od})
    return out


def streaks(checks: list[tuple[datetime, set[str]]], keys, since: dict[str, datetime] | None = None) -> dict[str, int]:
    """Consecutive overdue checks per line. `checks`: (period start, overdue line keys), newest first; the
    newest is the current check. Counting stops at the first check the line was not overdue in, or before
    the line's start in `since`."""
    since = since or {}
    out = {}
    for key in keys:
        n = 0
        start = since.get(key)
        for period, overdue in checks:
            if key not in overdue or (start and period < start):
                break
            n += 1
        out[key] = n
    return out


def confirmed_level(streak: int) -> int:
    """Escalation level the streak confirms: 1 -> 0 (Scenario 1) ... 4+ -> 3 (meeting). 0 means not overdue."""
    return min(streak, LEVELS) - 1


def history(checks: list[tuple[datetime, set[str]]], key: str, limit: int = 7) -> list[dict]:
    """The line's last checks, oldest first: [{"at": period start ISO, "overdue": bool}] for the dashboard strip."""
    return [{"at": start.isoformat(), "overdue": key in overdue} for start, overdue in reversed(checks[:limit])]
