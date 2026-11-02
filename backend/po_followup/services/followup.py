"""Runs, settings and the automatic scheduler."""

import logging
import os
import re
import threading
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta

from psycopg.types.json import Jsonb

from backend.core.env import TIMEZONE, env_int
from backend.core.tracing import flush as flush_traces

from backend.po_followup.db import store
from backend.po_followup.config import Settings
from backend.po_followup.domain.escalation import is_valid_mobile, normalize_mobile
from backend.po_followup.integrations.agent_call import AgentCaller
from backend.po_followup.integrations.meet import MeetScheduler
from backend.po_followup.integrations.whatsapp import UltraMsg, mask_token
from backend.po_followup.services.run import _report_summary, run_daily

log = logging.getLogger("po_followup.service")

MODES = ("dry", "live")
MAX_EVERY_MINUTES = max(1, env_int("FOLLOWUP_MAX_INTERVAL_DAYS", 30)) * 1440  # longest repeat interval
# A daily run missed by up to this much (PC asleep / admin started late) still runs; later than that waits a day
DAILY_CATCH_UP = timedelta(hours=2)


def default_settings() -> dict:
    base = Settings.from_env()
    return {
        # overdue lines are checked once per period; each check a line stays overdue confirms the next scenario
        "check_period_minutes": base.check_period_minutes,
        "auto_enabled": False,
        "auto_mode": "dry",  # dry / live
        "schedule_type": "daily",  # daily / interval
        "daily_time": "08:30",
        "every_minutes": env_int("FOLLOWUP_DEFAULT_INTERVAL_MINUTES", 1440),
        "vendor_codes": "",  # the vendors chosen in Vendor Control, comma separated
        "vendor_scope": base.vendor_scope,  # selected / all
        # Set and cleared only through control.emergency_stop / emergency_clear
        "emergency_stop": False,
        "emergency_stop_at": "",
        "emergency_stop_by": "",
        "fresh_start_at": "",  # set only by store.fresh_start
    }


def get_settings() -> dict:
    defaults = default_settings()
    # settings saved by older versions are ignored
    return {**defaults, **{k: v for k, v in store.get_settings().items() if k in defaults}}


def validate_settings(values: dict) -> dict:
    """Checks a partial settings update from the admin page; raises ValueError with a readable message."""
    out = {}
    for k, v in values.items():
        if k == "check_period_minutes":
            if not (isinstance(v, (int, float)) and not isinstance(v, bool) and 1 <= v <= 1440):
                raise ValueError("Check period must be 1 minute to 1 day (1440 minutes)")
        elif k == "auto_enabled":
            if not isinstance(v, bool):
                raise ValueError(f"{k} must be true or false")
        elif k == "auto_mode":
            if v not in ("dry", "live"):
                raise ValueError("Automatic mode must be dry or live")
        elif k == "schedule_type":
            if v not in ("daily", "interval"):
                raise ValueError("Schedule must be daily or interval")
        elif k == "daily_time":
            if not (isinstance(v, str) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v)):
                raise ValueError("Daily time must be HH:MM")
        elif k == "every_minutes":
            if not (isinstance(v, (int, float)) and not isinstance(v, bool) and 1 <= v <= MAX_EVERY_MINUTES):
                raise ValueError(f"Run interval must be 1 minute to {MAX_EVERY_MINUTES // 1440} days")
        elif k == "vendor_codes":
            if not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_./ ,-]*", v):
                raise ValueError("Vendor codes may contain only letters, digits, - _ . / and commas")
        elif k == "vendor_scope":
            if v not in ("selected", "all"):
                raise ValueError("Vendor scope must be selected or all")
        else:
            raise ValueError(f"Unknown setting {k}")
        out[k] = v
    return out


def effective_settings(mode: str) -> Settings:
    s = get_settings()
    return replace(
        Settings.from_env(),
        dry_run=mode != "live",
        vendor_codes=frozenset(c.strip() for c in s["vendor_codes"].split(",") if c.strip()),
        vendor_scope=s["vendor_scope"],
        check_period_minutes=float(s["check_period_minutes"]),
        fresh_start=s["fresh_start_at"],
    )


# ---------- sheet refresh ----------

def sync_sheet() -> dict:
    """Pull the latest Google Sheet into the database (same as `python -m backend.po_sync --once`)."""
    from backend.po_sync import sync as po_sync

    sheet_id = os.getenv("PO_SHEET_ID")
    if not sheet_id:
        raise RuntimeError("Set PO_SHEET_ID to refresh from the Google Sheet")
    cfg = po_sync.Config(
        delegated_user=os.getenv("GOOGLE_DELEGATED_USER", ""),
        sheet_id=sheet_id,
        interval_seconds=60,
        db=store.db_params(),
    )
    return po_sync.sync_once(cfg, po_sync.open_sheet(cfg))


# ---------- runs ----------

class Busy(RuntimeError):
    pass


# Two locks: an in-process lock (threads of this server) and a PostgreSQL advisory lock (other processes, the CLI).
# A run holds both from before its row is created until it has finished.
_lock = threading.Lock()


def is_busy() -> bool:
    return _lock.locked()


def _acquire() -> store.RunLock:
    if not _lock.acquire(blocking=False):
        raise Busy("A run is already in progress")
    db_lock = store.RunLock()
    try:
        if not db_lock.acquire():
            raise Busy("A run is in progress in another server process")
    except Exception:
        _lock.release()
        raise
    return db_lock


def _release(db_lock: store.RunLock) -> None:
    try:
        db_lock.release()
    finally:
        _lock.release()


@contextmanager
def runs_held():
    """No run can start inside this block; raises Busy if one is in progress."""
    held = _acquire()
    try:
        yield
    finally:
        _release(held)


def start_run(mode: str, trigger: str, wait: bool = False) -> int:
    """Start a run (in the background unless wait=True); returns the run id.
    Raises Busy if one is already running, in this or any other process."""
    if mode not in MODES:
        raise ValueError(f"Mode must be one of {MODES}")
    held = _acquire()
    try:
        if store.emergency_on():
            raise ValueError("The emergency stop is on. Clear it first.")
        if mode == "live" and not whatsapp_ready():
            raise ValueError("Live runs need ULTRAMSG_BASE_URL and ULTRAMSG_TOKEN in .env")
        with store.connect() as conn:
            run_id = conn.execute(
                "INSERT INTO followup.runs (trigger, mode, status) VALUES (%s, %s, 'running') RETURNING id",
                (trigger, mode)).fetchone()["id"]
    except Exception:
        _release(held)
        raise
    if wait:
        _execute(run_id, mode, trigger, held)
    else:
        threading.Thread(target=_execute, args=(run_id, mode, trigger, held), daemon=True, name=f"run-{run_id}").start()
    return run_id


def _execute(run_id: int, mode: str, trigger: str, held: store.RunLock) -> None:
    status, summary, report_json, error = "failed", None, None, None
    try:
        log.info("Refreshing from the Google Sheet")
        sync_sheet()
        settings = effective_settings(mode)
        sender = caller = meet = None
        if mode == "live":
            sender = UltraMsg(settings.ultramsg_base_url, settings.ultramsg_token)
            caller, meet = AgentCaller(), MeetScheduler()
        report = run_daily(settings, store.PgStore(), sender, caller=caller, meet=meet, should_stop=store.emergency_on,
                           triggered_by=f"{'Automatic' if trigger == 'auto' else 'Manual'} {mode} run")
        summary = _report_summary(report)
        report_json = {"alerts": report.alerts, "sheet_writes": report.sheet_writes}
        status = "stopped" if report.stopped else "ok"
    except Exception as e:
        log.exception("Run %s failed", run_id)
        error = str(e)[:2000] or type(e).__name__
    finally:
        try:
            with store.connect() as conn:
                conn.execute(
                    "UPDATE followup.runs SET finished_at = now(), status = %s, summary = %s, report = %s, error = %s "
                    "WHERE id = %s",
                    (status, Jsonb(summary) if summary else None, Jsonb(report_json) if report_json else None, error, run_id))
        except Exception:
            # The row stays 'running'; the scheduler marks it failed once the lock is free (mark_interrupted_runs)
            log.exception("Could not record the result of run %s", run_id)
        finally:
            _release(held)
            flush_traces()
    if status == "failed" and trigger == "auto":
        notify_failure(run_id, mode, error or "")


def notify_failure(run_id: int, mode: str, error: str) -> None:
    """WhatsApp the admin (FOLLOWUP_ALERT_MOBILE) when an automatic run fails, so a missed day is noticed."""
    mobile = normalize_mobile(os.getenv("FOLLOWUP_ALERT_MOBILE", ""))
    if not is_valid_mobile(mobile):
        return
    s = Settings.from_env()
    if not (s.ultramsg_base_url and s.ultramsg_token):
        log.warning("Cannot send the failure alert: UltraMsg is not configured")
        return
    text = (f"⚠️ PO follow-up: automatic {mode} run #{run_id} FAILED\n\n"
            f"{mask_token(error)[:600]}\n\n"
            f"A failed daily run is retried up to {AUTO_MAX_ATTEMPTS} times, "
            f"{int(AUTO_RETRY_DELAY.total_seconds() // 60)} minutes apart.")
    try:
        UltraMsg(s.ultramsg_base_url, s.ultramsg_token).send_text(mobile, text)
    except Exception as e:
        log.error("Could not send the failure alert: %s", mask_token(str(e)))


def list_runs(limit: int = 25) -> list[dict]:
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT id, started_at, finished_at, trigger, mode, status, summary, error FROM followup.runs "
            "ORDER BY id DESC LIMIT %s", (limit,)).fetchall()
    return [_jsonable(r) for r in rows]


def _jsonable(r: dict) -> dict:
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in r.items()}


def mark_interrupted_runs() -> int:
    """Runs left 'running' by a process that died mid-run. Only touched while nobody holds the run lock,
    so a run that is really executing (here or in another process) is never marked."""
    if is_busy():
        return 0
    lock = store.RunLock()
    if not lock.acquire():
        return 0
    try:
        with store.connect() as conn:
            marked = conn.execute(
                "UPDATE followup.runs SET status = 'failed', finished_at = now(), "
                "error = 'Interrupted: the server process stopped during this run' WHERE status = 'running'").rowcount
    finally:
        lock.release()
    if marked:
        log.warning("Marked %d interrupted run(s) as failed", marked)
    return marked


def whatsapp_ready() -> bool:
    s = Settings.from_env()
    return bool(s.ultramsg_base_url and s.ultramsg_token)


# A failed daily run is retried this often, at most this many attempts per day (then it waits for tomorrow)
AUTO_RETRY_DELAY = timedelta(minutes=10)
AUTO_MAX_ATTEMPTS = env_int("FOLLOWUP_AUTO_MAX_ATTEMPTS", 3)


def recent_auto_runs(limit: int = 10) -> list[dict]:
    """Latest automatic runs, newest first: [{"id", "started_at", "status", "error"}]."""
    with store.connect() as conn:
        rows = conn.execute("SELECT id, started_at, status, error FROM followup.runs WHERE trigger = 'auto' "
                            "ORDER BY id DESC LIMIT %s", (limit,)).fetchall()
    return [{**r, "started_at": r["started_at"].astimezone(TIMEZONE)} for r in rows]


def next_auto_run(s: dict | None = None, now: datetime | None = None) -> datetime | None:
    s = s or get_settings()
    if not s["auto_enabled"] or s.get("emergency_stop"):
        return None
    now = now or datetime.now(TIMEZONE)
    runs = recent_auto_runs()
    if s["schedule_type"] == "interval":
        return now if not runs else max(now, runs[0]["started_at"] + timedelta(minutes=s["every_minutes"]))
    hh, mm = (int(x) for x in s["daily_time"].split(":"))
    slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    today = [r for r in runs if r["started_at"] >= slot]
    if any(r["status"] not in ("failed", "stopped") for r in today):
        return slot + timedelta(days=1)  # today's run succeeded (or is running)
    if today:  # only failed or stopped attempts so far: retry a few times, then give up for today
        if len(today) >= AUTO_MAX_ATTEMPTS:
            return slot + timedelta(days=1)
        return max(now, today[0]["started_at"] + AUTO_RETRY_DELAY)
    if now < slot or now - slot <= DAILY_CATCH_UP:
        return slot  # later today, or missed by a little (PC was asleep): run now
    return slot + timedelta(days=1)  # missed by too much: wait for tomorrow


def is_due(s: dict, now: datetime) -> bool:
    nxt = next_auto_run(s, now)
    return nxt is not None and nxt <= now


class Scheduler:
    """Checks every `tick` seconds and starts an automatic run when due.
    Safe to run in several processes: the run lock lets only one of them run at a time."""

    # every this many ticks, runs left 'running' by a dead process are marked failed
    CLEANUP_EVERY = 4

    def __init__(self, tick: float | None = None):
        self.tick = tick if tick is not None else max(1, env_int("FOLLOWUP_SCHEDULER_TICK_SECONDS", 15))
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True, name="followup-scheduler")

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def _loop(self):
        ticks = failing = 0
        while not self.stop_event.wait(self.tick):
            ticks += 1
            try:
                if ticks % self.CLEANUP_EVERY == 0:
                    mark_interrupted_runs()
                s = get_settings()
                if is_due(s, datetime.now(TIMEZONE)) and not is_busy():
                    run_id = start_run(s["auto_mode"], "auto")
                    log.info("Automatic %s run %s started", s["auto_mode"], run_id)
                if failing:
                    failing = 0
                    log.info("Scheduler checks work again")
            except Busy:
                pass
            except Exception:
                # Log the first failure in full, then once every ~5 minutes, not every tick
                failing += 1
                if failing == 1 or failing % 20 == 0:
                    log.exception("Scheduler check failed (%d in a row)", failing)
