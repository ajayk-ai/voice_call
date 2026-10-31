"""Start / stop of the automatic follow-up, as the dashboard's Run control tab drives it.

"Start" saves the schedule (repeat every N minutes, default one day, or daily at a fixed time) and switches the
scheduler on; the scheduler (followup.Scheduler) then runs the follow-up whenever it is due. The first run happens
at once unless the schedule is daily at a time of day. "Stop" switches it off; runs already in progress finish.
Live needs the word LIVE.
"""

from datetime import datetime

from backend.core.env import TIMEZONE, env_int
from backend.po_followup.db import store
from backend.po_followup.services import followup as service
from backend.po_followup.services import trace, users, vendor_numbers

PROBLEM_ROWS = 500  # Alert_Log rows looked at for failed deliveries


def default_interval_minutes() -> int:
    """Repeat interval the dashboard starts with (.env FOLLOWUP_DEFAULT_INTERVAL_MINUTES, default one day)."""
    return max(1, env_int("FOLLOWUP_DEFAULT_INTERVAL_MINUTES", 1440))


def max_interval_minutes() -> int:
    return service.MAX_EVERY_MINUTES


def status() -> dict:
    s = service.get_settings()
    nxt = service.next_auto_run(s)
    selected = trace.chosen_codes(s)
    return {
        "default_every_minutes": default_interval_minutes(),
        "max_every_minutes": max_interval_minutes(),
        "running": bool(s["auto_enabled"]),
        "mode": s["auto_mode"],
        "schedule_type": s["schedule_type"],
        "every_minutes": s["every_minutes"],
        "daily_time": s["daily_time"],
        "scope": s["vendor_scope"],
        "selected_vendors": len(selected),
        "synced_at": store.master_synced_at(),
        "whatsapp_ready": service.whatsapp_ready(),
        "busy": service.is_busy(),
        "next_run": nxt.isoformat() if nxt else None,
        "now": datetime.now(TIMEZONE).isoformat(),
        "runs": service.list_runs(12),
    }


def chosen_vendors() -> list[dict]:
    """The vendors follow-up runs for and where each is; a chosen vendor with nothing overdue is listed too."""
    chosen = trace.vendor_filter(service.get_settings())
    rows = trace.vendor_list()
    if chosen is None:
        return _with_problems(rows)
    out = [r for r in rows if r["code"] in chosen]
    names = {v["code"]: v["name"] for v in vendor_numbers.list_numbers()["vendors"]}
    for code in sorted(chosen - {r["code"] for r in out}):
        out.append({"code": code, "name": names.get(code, code), "pending_lines": 0, "max_od": 0, "level": None,
                    "step": None, "last_alert_at": None, "chosen": True})
    return _with_problems(out)


def _with_problems(rows: list[dict]) -> list[dict]:
    """Adds each vendor's `problem`: why its latest message failed, if it did (Alert_Log, newest row first)."""
    latest: dict[str, dict] = {}
    for r in store.table_rows("Alert_Log", PROBLEM_ROWS):
        code = str(r.get("Vendor") or "").rstrip(")").rsplit("(", 1)[-1]
        latest.setdefault(code, r)
    for v in rows:
        r = latest.get(v["code"], {})
        v["problem"] = (r.get("Error") or "") if r.get("Status") != "SENT" else ""
    return rows


def start(schedule_type: str, every_minutes: float | None, daily_time: str | None, mode: str, confirm: str | None,
          scope: str | None = None, codes: list[str] | None = None, confirm_scope: str | None = None,
          actor: str = "") -> dict:
    """Start (or update) the automatic follow-up. `scope`/`codes` choose the vendors in the same step:
    scope 'selected' + codes = exactly those vendors, scope 'all' = every vendor (production asks for ALL)."""
    every_minutes = default_interval_minutes() if every_minutes is None else every_minutes
    daily_time = daily_time or service.get_settings()["daily_time"]
    values = service.validate_settings({
        "auto_enabled": True, "auto_mode": mode, "schedule_type": schedule_type,
        "every_minutes": every_minutes, "daily_time": daily_time,
    })
    current = service.get_settings()
    if mode == "live" and not (current["auto_enabled"] and current["auto_mode"] == "live") and confirm != "LIVE":
        raise ValueError("Starting automatic live follow-up needs confirm = LIVE")
    if mode == "live" and not service.whatsapp_ready():
        raise ValueError("Live runs need ULTRAMSG_BASE_URL and ULTRAMSG_TOKEN in .env")
    if scope == "selected" and not [c for c in (codes or []) if c.strip()]:
        raise ValueError("Pick at least one vendor, or choose All vendors.")
    if scope is not None:
        vendor_numbers.set_selection(codes or [], scope, confirm_scope)
        current = service.get_settings()
    chosen = trace.vendor_filter(current)  # None = every vendor
    if chosen is not None and not chosen:
        raise ValueError("No vendor is selected yet. Pick the vendors first (or choose All vendors).")
    # One run is one overdue check, so a vendor moves up one scenario per run. A repeat shorter than a day makes the
    # check period that short (otherwise a day's second run would count as the same check); a day or longer stays a day.
    values["check_period_minutes"] = min(float(every_minutes), 1440) if schedule_type == "interval" else 1440
    store.save_settings(values)
    users.audit(actor or "unknown", "follow_up_started", {
        "mode": mode, "schedule": schedule_type, "every_minutes": every_minutes, "daily_time": daily_time,
        "vendors": "all" if chosen is None else sorted(chosen)})
    return status()


def run_now(mode: str, confirm: str | None, actor: str) -> int:
    if mode == "live" and confirm != "LIVE":
        raise ValueError("A live run needs confirm = LIVE")
    run_id = service.start_run(mode, "manual")
    users.audit(actor, "run_now", {"mode": mode, "run_id": run_id})
    return run_id


def stop(actor: str = "") -> dict:
    store.save_settings({"auto_enabled": False})
    users.audit(actor or "unknown", "follow_up_stopped")
    return status()
