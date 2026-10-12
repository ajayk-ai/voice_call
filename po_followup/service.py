"""Runs, settings and the automatic scheduler for the admin page (production and test environments)."""

import logging
import os
import re
import threading
from dataclasses import replace
from datetime import datetime, timedelta

from psycopg.types.json import Jsonb

from monitoring.tracing import flush as flush_traces

from . import store
from .config import Settings
from .run import ZONE, _report_summary, run_daily

log = logging.getLogger("po_followup.service")

MODES = ("preview", "dry", "live")
# A daily run missed by up to this much (PC asleep / admin started late) still runs; later than that waits a day
DAILY_CATCH_UP = timedelta(hours=2)


def default_settings(env: str) -> dict:
    base = Settings.from_env()
    test = env == "test"
    return {
        # minutes to wait after level 0, 1, 2 before the next level
        "intervals_minutes": [5, 5, 5] if test else list(base.intervals_minutes),
        "auto_enabled": False,
        "auto_mode": "dry",  # dry / live
        "schedule_type": "interval" if test else "daily",  # daily / interval
        "daily_time": base.run_at,
        "every_minutes": 5 if test else 60,
        "pilot_mode": base.pilot_mode or test,
        "pilot_mobile": base.pilot_mobile,
        "pilot_vendor_codes": ",".join(sorted(base.pilot_vendor_codes)),
        "scm_hod_mobile": base.scm_hod_mobile,
        "max_lines_per_message": base.max_lines_per_message,
        "refresh_from_sheet": not test,  # production: pull the latest Google Sheet before each run
        "vendor_test": None,  # the one-vendor test started from the admin page (test only), see start_vendor_test
    }


def get_settings(env: str) -> dict:
    return {**default_settings(env), **store.get_settings(env)}


def validate_settings(values: dict) -> dict:
    """Checks a partial settings update from the admin page; raises ValueError with a readable message."""
    out = {}
    for k, v in values.items():
        if k == "intervals_minutes":
            if not (isinstance(v, list) and len(v) == 3 and all(isinstance(x, (int, float)) and 0 < x <= 43200 for x in v)):
                raise ValueError("Intervals must be 3 numbers of minutes between 0 and 30 days")
        elif k in ("auto_enabled", "pilot_mode", "refresh_from_sheet"):
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
            if not (isinstance(v, (int, float)) and 1 <= v <= 1440):
                raise ValueError("Run interval must be 1-1440 minutes")
        elif k == "max_lines_per_message":
            if not (isinstance(v, int) and 1 <= v <= 50):
                raise ValueError("Lines per message must be 1-50")
        elif k in ("pilot_mobile", "scm_hod_mobile", "pilot_vendor_codes"):
            if not isinstance(v, str) or not re.fullmatch(r"[0-9+ ,]*", v):
                raise ValueError(f"{k} may contain only digits, +, spaces and commas")
        else:
            raise ValueError(f"Unknown setting {k}")
        out[k] = v
    return out


def effective_settings(env: str, mode: str) -> Settings:
    s = get_settings(env)
    return replace(
        Settings.from_env(),
        test_mode=mode != "live",
        # Test dry runs advance the tracker and write tasks (no WhatsApp), so escalation can be watched end to end.
        # Production dry runs change nothing.
        update_tracker_in_test_mode=env == "test",
        pilot_mode=s["pilot_mode"],
        pilot_mobile=s["pilot_mobile"],
        pilot_vendor_codes=frozenset(c.strip() for c in s["pilot_vendor_codes"].split(",") if c.strip()),
        scm_hod_mobile=s["scm_hod_mobile"],
        max_lines_per_message=int(s["max_lines_per_message"]),
        intervals_minutes=tuple(float(x) for x in s["intervals_minutes"]),
    )


# ---------- sheet refresh (production) ----------

def sync_sheet_to_prod() -> dict:
    """Pull the latest Google Sheet into the production database (same as `python -m po_sync --once`)."""
    from po_sync import sync as po_sync

    sheet_id = os.getenv("PO_SHEET_ID")
    if not sheet_id:
        raise RuntimeError("Set PO_SHEET_ID to refresh production from the Google Sheet")
    cfg = po_sync.Config(
        delegated_user=os.getenv("GOOGLE_DELEGATED_USER", ""),
        sheet_id=sheet_id,
        interval_seconds=60,
        db=store.db_params("prod"),
    )
    return po_sync.sync_once(cfg, po_sync.open_sheet(cfg))


# ---------- runs ----------

class Busy(RuntimeError):
    pass


# Two locks per environment: an in-process lock (threads of this server) and a PostgreSQL advisory lock
# (other server processes, the CLI). A run holds both from before its row is created until it has finished.
_locks = {env: threading.Lock() for env in store.ENVIRONMENTS}
# A run still 'running' after this long is reported as stuck on the admin page
STUCK_AFTER = timedelta(minutes=30)


def is_busy(env: str) -> bool:
    return _locks[env].locked()


def _acquire(env: str) -> store.RunLock:
    if not _locks[env].acquire(blocking=False):
        raise Busy(f"A {env} run is already in progress")
    db_lock = store.RunLock(env)
    try:
        if not db_lock.acquire():
            raise Busy(f"A {env} run is in progress in another server process")
    except Exception:
        _locks[env].release()
        raise
    return db_lock


def _release(env: str, db_lock: store.RunLock) -> None:
    try:
        db_lock.release()
    finally:
        _locks[env].release()


def start_run(env: str, mode: str, trigger: str, wait: bool = False, held: store.RunLock | None = None) -> int:
    """Start a run (in the background unless wait=True); returns the run id.
    Raises Busy if one is already running in this environment, in this or any other process.
    `held`: the run locks, when the caller already took them with _acquire (they are released by the run)."""
    if held is None:
        if mode not in MODES:
            raise ValueError(f"Mode must be one of {MODES}")
        held = _acquire(env)
    try:
        if mode not in MODES:
            raise ValueError(f"Mode must be one of {MODES}")
        if mode == "live" and not whatsapp_ready():
            raise ValueError("Live runs need ULTRAMSG_BASE_URL and ULTRAMSG_TOKEN in .env")
        with store.connect(env) as conn:
            run_id = conn.execute(
                "INSERT INTO followup.runs (trigger, mode, status) VALUES (%s, %s, 'running') RETURNING id",
                (trigger, mode)).fetchone()["id"]
    except Exception:
        _release(env, held)
        raise
    if wait:
        _execute(env, run_id, mode, trigger, held)
    else:
        threading.Thread(target=_execute, args=(env, run_id, mode, trigger, held), daemon=True,
                         name=f"run-{env}-{run_id}").start()
    return run_id


def _execute(env: str, run_id: int, mode: str, trigger: str, held: store.RunLock) -> None:
    status, summary, report_json, error = "failed", None, None, None
    try:
        if env == "prod" and get_settings(env)["refresh_from_sheet"]:
            log.info("[prod] refreshing from Google Sheet")
            sync_sheet_to_prod()
        settings = effective_settings(env, mode)
        sender = caller = meet = None
        if mode == "live":
            from .agent_call import AgentCaller
            from .meet import MeetScheduler
            from .whatsapp import UltraMsg

            sender = UltraMsg(settings.ultramsg_base_url, settings.ultramsg_token)
            caller, meet = AgentCaller(), MeetScheduler()
        report = run_daily(settings, store.PgStore(env), sender,
                           triggered_by=f"{'Automatic' if trigger == 'auto' else 'Manual'} {mode} run ({env})",
                           preview=mode == "preview", caller=caller, meet=meet)
        summary = _report_summary(report)
        report_json = {"alerts": report.alerts, "sheet_writes": report.sheet_writes}
        status = "ok"
    except Exception as e:
        log.exception("[%s] run %s failed", env, run_id)
        error = str(e)[:2000] or type(e).__name__
    finally:
        try:
            with store.connect(env) as conn:
                conn.execute(
                    "UPDATE followup.runs SET finished_at = now(), status = %s, summary = %s, report = %s, error = %s "
                    "WHERE id = %s",
                    (status, Jsonb(summary) if summary else None, Jsonb(report_json) if report_json else None, error, run_id))
        except Exception:
            # The row stays 'running'; the scheduler marks it failed once the lock is free (mark_interrupted_runs)
            log.exception("[%s] could not record the result of run %s", env, run_id)
        finally:
            _release(env, held)
            flush_traces()
    if status == "failed" and trigger == "auto":
        notify_failure(env, run_id, mode, error or "")


def notify_failure(env: str, run_id: int, mode: str, error: str) -> None:
    """WhatsApp the admin (FOLLOWUP_ALERT_MOBILE) when an automatic run fails, so a missed day is noticed."""
    from .escalation import is_valid_mobile, normalize_mobile
    from .whatsapp import UltraMsg, mask_token

    mobile = normalize_mobile(os.getenv("FOLLOWUP_ALERT_MOBILE", ""), False, "")
    if not is_valid_mobile(mobile):
        return
    s = Settings.from_env()
    if not (s.ultramsg_base_url and s.ultramsg_token):
        log.warning("Cannot send the failure alert: UltraMsg is not configured")
        return
    text = (f"⚠️ PO follow-up: automatic {mode} run #{run_id} ({'PRODUCTION' if env == 'prod' else 'test'}) FAILED\n\n"
            f"{mask_token(error)[:600]}\n\n"
            f"A failed daily run is retried up to {AUTO_MAX_ATTEMPTS} times, "
            f"{int(AUTO_RETRY_DELAY.total_seconds() // 60)} minutes apart. Details: admin page -> Overview.")
    try:
        UltraMsg(s.ultramsg_base_url, s.ultramsg_token).send_text(mobile, text)
    except Exception as e:
        log.error("Could not send the failure alert: %s", mask_token(str(e)))


def list_runs(env: str, limit: int = 25) -> list[dict]:
    with store.connect(env) as conn:
        rows = conn.execute(
            "SELECT id, started_at, finished_at, trigger, mode, status, summary, error FROM followup.runs "
            "ORDER BY id DESC LIMIT %s", (limit,)).fetchall()
    return [_jsonable(r) for r in rows]


def get_run(env: str, run_id: int) -> dict | None:
    with store.connect(env) as conn:
        r = conn.execute("SELECT * FROM followup.runs WHERE id = %s", (run_id,)).fetchone()
    return _jsonable(r) if r else None


def _jsonable(r: dict) -> dict:
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in r.items()}


def mark_interrupted_runs(envs=store.ENVIRONMENTS) -> int:
    """Runs left 'running' by a process that died mid-run. Only touched while nobody holds the run lock,
    so a run that is really executing (here or in another process) is never marked."""
    marked = 0
    for env in envs:
        if is_busy(env):
            continue
        lock = store.RunLock(env)
        if not lock.acquire():
            continue
        try:
            with store.connect(env) as conn:
                marked += conn.execute(
                    "UPDATE followup.runs SET status = 'failed', finished_at = now(), "
                    "error = 'Interrupted: the server process stopped during this run' WHERE status = 'running'").rowcount
        finally:
            lock.release()
    if marked:
        log.warning("Marked %d interrupted run(s) as failed", marked)
    return marked


def health(env: str) -> dict:
    """What the admin page shows under 'Health': scheduler alive, automatic run results, stuck runs."""
    runs = recent_auto_runs(env)
    streak = 0
    for r in runs:
        if r["status"] != "failed":
            break
        streak += 1
    with store.connect(env) as conn:
        stuck = conn.execute("SELECT id, started_at FROM followup.runs WHERE status = 'running' AND started_at < %s",
                             (datetime.now(ZONE) - STUCK_AFTER,)).fetchall()
    beat = _heartbeat
    return {
        "scheduler_heartbeat": beat.isoformat() if beat else None,
        "last_auto": {**runs[0], "started_at": runs[0]["started_at"].isoformat()} if runs else None,
        "failed_streak": streak,
        "stuck_runs": [_jsonable(r) for r in stuck],
        "failure_alerts": bool(os.getenv("FOLLOWUP_ALERT_MOBILE")),
    }


# ---------- one-vendor test (test environment) ----------

def whatsapp_ready() -> bool:
    s = Settings.from_env()
    return bool(s.ultramsg_base_url and s.ultramsg_token)


def start_vendor_test(vendor_code: str, mobile: str, minutes: float, mode: str, auto: bool) -> int:
    """Walk one vendor through Scenario 1 -> 2 -> 3 -> meeting in the test environment, `minutes` apart.

    Resets the vendor's test tracker/logs/tasks, sends everything to `mobile` (pilot mode, this vendor only),
    runs Scenario 1 now and, with auto, lets the scheduler run every `minutes` for the next steps.
    Returns the id of the first run."""
    from .escalation import is_valid_mobile, normalize_mobile
    from .trace import index_vendors

    env = "test"
    if mode not in ("dry", "live"):
        raise ValueError("Mode must be dry or live")
    if not is_valid_mobile(normalize_mobile(mobile, True, mobile)):
        raise ValueError("Enter your WhatsApp number, e.g. 9876543210 or +919876543210")
    if mode == "live" and not whatsapp_ready():
        raise ValueError("Live test needs ULTRAMSG_BASE_URL and ULTRAMSG_TOKEN in .env (or use a dry run)")
    values = validate_settings({
        "pilot_mode": True, "pilot_vendor_codes": vendor_code, "pilot_mobile": mobile,
        "intervals_minutes": [minutes] * 3, "auto_enabled": False, "auto_mode": mode,
        "schedule_type": "interval", "every_minutes": max(1, minutes),
    })
    # Take the run lock first, so no automatic run can start between the reset and Scenario 1
    held = _acquire(env)
    try:
        vendor = index_vendors(store.PgStore(env).read_records("Master")).get(vendor_code)
        if not vendor or not vendor["lines"]:
            raise ValueError(f"Vendor {vendor_code} has no pending lines (OD Days > 0) in the test data")
        store.reset_vendor(env, vendor_code)
        store.save_settings(env, {**values, "vendor_test": {
            "vendor_code": vendor_code, "vendor_name": vendor["name"], "mobile": mobile, "minutes": minutes,
            "mode": mode, "auto": auto, "started_at": datetime.now(ZONE).isoformat(), "stopped_at": None}})
    except Exception:
        _release(env, held)
        raise
    # Scenario 1 now. Started as 'auto' so the automatic schedule counts its next run from this one.
    run_id = start_run(env, mode, "auto" if auto else "manual", held=held)
    if auto:
        store.save_settings(env, {"auto_enabled": True})
    return run_id


def call_vendor_now(vendor_code: str, mobile: str) -> dict:
    """Ring `mobile` right away with the voice agent reading this vendor's overdue lines from the test data,
    without going through Scenarios 1-2. Nothing is written to the tracker or logs."""
    from .agent_call import AgentCaller, call_script
    from .escalation import build_groups, is_valid_mobile, normalize_mobile

    number = normalize_mobile(mobile, True, mobile)
    if not is_valid_mobile(number):
        raise ValueError("Enter the number to ring, e.g. 9876543210 or +919876543210")
    groups = build_groups(store.PgStore("test").read_records("Master"), [], datetime.now(ZONE))
    g = next((g for g in groups if g["vendorCode"] == vendor_code), None)
    if not g:
        raise ValueError(f"Vendor {vendor_code} has no pending lines (OD Days > 0) in the test data")
    try:
        call_id = AgentCaller().call(number, call_script(g))
    except Exception as e:  # not configured, tunnel down, FreJun error: show the reason on the page
        log.warning("Direct agent call for %s failed: %s", vendor_code, e)
        raise ValueError(f"Call not placed: {getattr(e, 'message', None) or e}") from e
    log.info("Direct agent call for %s placed, FreJun call id %s", g["vendorLabel"], call_id)
    return {"call_id": call_id, "vendor": g["vendorLabel"], "lines": g["overdueLines"], "to": number}


def stop_vendor_test(reason: str | None = None) -> None:
    env = "test"
    test = get_settings(env)["vendor_test"]
    values = {"auto_enabled": False}
    if test:
        values["vendor_test"] = {**test, "stopped_at": datetime.now(ZONE).isoformat(), "stop_reason": reason}
    store.save_settings(env, values)


# A running vendor test whose last step is older than this (and 3 of its waits) is stopped, not resumed:
# after the server was down, nobody expects the remaining scenarios to suddenly fire hours later
VENDOR_TEST_STALE = timedelta(minutes=30)


def stale_vendor_test(s: dict, last_auto: datetime | None, now: datetime) -> bool:
    vt = s.get("vendor_test")
    if not vt or vt.get("stopped_at") or not s["auto_enabled"]:
        return False
    last = last_auto or datetime.fromisoformat(vt["started_at"])
    return now - last > max(VENDOR_TEST_STALE, timedelta(minutes=3 * float(vt["minutes"])))


# ---------- automatic scheduler ----------

# A failed daily run is retried this often, at most this many attempts per day (then it waits for tomorrow)
AUTO_RETRY_DELAY = timedelta(minutes=10)
AUTO_MAX_ATTEMPTS = 3


def recent_auto_runs(env: str, limit: int = 10) -> list[dict]:
    """Latest automatic runs, newest first: [{"id", "started_at", "status", "error"}]."""
    with store.connect(env) as conn:
        rows = conn.execute("SELECT id, started_at, status, error FROM followup.runs WHERE trigger = 'auto' "
                            "ORDER BY id DESC LIMIT %s", (limit,)).fetchall()
    return [{**r, "started_at": r["started_at"].astimezone(ZONE)} for r in rows]


def next_auto_run(env: str, s: dict | None = None, now: datetime | None = None) -> datetime | None:
    s = s or get_settings(env)
    if not s["auto_enabled"]:
        return None
    now = now or datetime.now(ZONE)
    runs = recent_auto_runs(env)
    if s["schedule_type"] == "interval":
        return now if not runs else max(now, runs[0]["started_at"] + timedelta(minutes=s["every_minutes"]))
    hh, mm = (int(x) for x in s["daily_time"].split(":"))
    slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    today = [r for r in runs if r["started_at"] >= slot]
    if any(r["status"] != "failed" for r in today):
        return slot + timedelta(days=1)  # today's run succeeded (or is running)
    if today:  # only failed attempts so far: retry a few times, then give up for today
        if len(today) >= AUTO_MAX_ATTEMPTS:
            return slot + timedelta(days=1)
        return max(now, today[0]["started_at"] + AUTO_RETRY_DELAY)
    if now < slot or now - slot <= DAILY_CATCH_UP:
        return slot  # later today, or missed by a little (PC was asleep): run now
    return slot + timedelta(days=1)  # missed by too much: wait for tomorrow


def is_due(env: str, s: dict, now: datetime) -> bool:
    nxt = next_auto_run(env, s, now)
    return nxt is not None and nxt <= now


_heartbeat: datetime | None = None  # last scheduler check in this process


class Scheduler:
    """Checks both environments every `tick` seconds and starts automatic runs when due.
    Safe to run in several processes: the run lock lets only one of them run an environment at a time."""

    # every this many ticks, runs left 'running' by a dead process are marked failed
    CLEANUP_EVERY = 4

    def __init__(self, tick: float = 15):
        self.tick = tick
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True, name="followup-scheduler")

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def _loop(self):
        global _heartbeat
        ticks = 0
        failing: dict[str, int] = {}
        while not self.stop_event.wait(self.tick):
            _heartbeat = datetime.now(ZONE)
            ticks += 1
            for env in store.ENVIRONMENTS:
                try:
                    if ticks % self.CLEANUP_EVERY == 0:
                        mark_interrupted_runs([env])
                    s = get_settings(env)
                    now = datetime.now(ZONE)
                    if env == "test" and s.get("vendor_test"):
                        runs = recent_auto_runs(env, 1)
                        if stale_vendor_test(s, runs[0]["started_at"] if runs else None, now):
                            stop_vendor_test("Stopped automatically: no step ran for a long time (was the server off?). "
                                             "Press Start over to run the test again.")
                            log.warning("[test] stale vendor test stopped instead of resumed")
                            continue
                    if is_due(env, s, now) and not is_busy(env):
                        run_id = start_run(env, s["auto_mode"], "auto")
                        log.info("[%s] automatic %s run %s started", env, s["auto_mode"], run_id)
                    if failing.pop(env, 0):
                        log.info("[%s] scheduler checks work again", env)
                except Busy:
                    pass
                except Exception:
                    # Log the first failure in full, then once every ~5 minutes, not every tick
                    n = failing[env] = failing.get(env, 0) + 1
                    if n == 1 or n % 20 == 0:
                        log.exception("[%s] scheduler check failed (%d in a row)", env, n)
