"""Admin page and API for the PO follow-up workflow, served by the main web app (backend/voice_agent/app.py).

    GET  /admin                         the admin page
    /admin/api/...                      JSON API (needs the admin code in the X-Admin-Code header)

Access: ADMIN_PASSWORD (falls back to APP_PASSWORD). With neither set, only requests from this PC are allowed.
Needs PostgreSQL (psycopg); without it the rest of the app still runs and the admin API answers 503.
The automatic scheduler runs inside the web app (set FOLLOWUP_SCHEDULER=false to turn it off).
"""

import logging
import os
import secrets
import threading
from datetime import datetime
from typing import Callable

from fastapi import APIRouter, Depends, Header, HTTPException, Path as PathParam, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from backend.core import frontend
from backend.po_followup.config import env_bool
from backend.po_followup.models.admin import Env, RunRequest, SettingsUpdate, Table, VendorCall, VendorTest

try:
    import psycopg

    from backend.po_followup.db import store
    from backend.po_followup.services import followup as service
    from backend.po_followup.services import trace
except ImportError as e:  # psycopg not installed (e.g. on Render): admin unavailable, the rest of the app runs
    psycopg = service = store = trace = None
    IMPORT_ERROR = f"PostgreSQL driver not installed ({e.name}); install psycopg[binary]"
else:
    IMPORT_ERROR = None

log = logging.getLogger("po_followup.admin")

PAGE = frontend.page("admin.html")

# ---------- access and readiness ----------

def admin_code() -> str | None:
    return os.getenv("ADMIN_PASSWORD") or os.getenv("APP_PASSWORD") or None


def is_local(request: Request) -> bool:
    # Behind Render's proxy every request carries X-Forwarded-For, so it never counts as local
    host = request.client.host if request.client else ""
    return "x-forwarded-for" not in request.headers and host in ("127.0.0.1", "::1", "localhost")


def require_admin(request: Request, x_admin_code: str | None = Header(None)) -> None:
    expected = admin_code()
    if not expected:
        if is_local(request):
            return
        raise HTTPException(403, "Set ADMIN_PASSWORD on the server to use the admin page from another machine.")
    if not (x_admin_code and secrets.compare_digest(x_admin_code.encode(), expected.encode())):
        raise HTTPException(401, "Wrong admin code.")


_ready = False
_ready_lock = threading.Lock()
_scheduler = None


def _init() -> None:
    """Create both databases' follow-up tables once; retried on the next request if PostgreSQL was down."""
    global _ready
    if IMPORT_ERROR:
        raise RuntimeError(IMPORT_ERROR)
    with _ready_lock:
        if _ready:
            return
        for env in store.ENVIRONMENTS:
            store.init_environment(env)
        service.mark_interrupted_runs()
        _ready = True


async def require_ready() -> None:
    if _ready:
        return
    try:
        await run_in_threadpool(_init)
    except Exception as e:
        log.warning("Admin unavailable: %s", e)
        raise HTTPException(503, f"Admin unavailable: {IMPORT_ERROR or f'database not reachable ({e})'}")


def available() -> bool:
    return IMPORT_ERROR is None


def scheduler_running() -> bool:
    return _scheduler is not None


async def startup() -> None:
    """Called from the app lifespan: prepare the databases and start the scheduler if enabled."""
    global _scheduler
    if IMPORT_ERROR:
        log.info("Admin page disabled: %s", IMPORT_ERROR)
        return
    try:
        await run_in_threadpool(_init)
    except Exception as e:
        log.warning("Admin database not ready yet (%s); will retry on first use", e)
    # On by default: the run lock (store.RunLock) makes several schedulers safe; FOLLOWUP_SCHEDULER=false turns it off
    if env_bool("FOLLOWUP_SCHEDULER", True):
        _scheduler = service.Scheduler()
        _scheduler.start()
        log.info("PO follow-up scheduler running")


def shutdown() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.stop()
        _scheduler = None


async def call(fn: Callable, *args):
    """Run blocking service/store code off the event loop, mapping its errors to HTTP errors."""
    try:
        return await run_in_threadpool(fn, *args)
    except service.Busy as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except psycopg.OperationalError as e:  # database down / restarting: the page retries by itself
        log.warning("Database error on admin request: %s", e)
        raise HTTPException(503, "Database not reachable right now; retrying…")


# ---------- routes ----------

page_router = APIRouter(tags=["pages"])
router = APIRouter(prefix="/admin/api", tags=["admin"], dependencies=[Depends(require_admin), Depends(require_ready)])


@page_router.get("/admin", response_class=HTMLResponse, include_in_schema=False)
async def page():
    return HTMLResponse(PAGE.read_text(encoding="utf-8"))


@router.get("/session", summary="Check the admin code")
async def session():
    return {"ok": True, "scheduler": scheduler_running()}


@router.get("/{env}/state", summary="Settings, stats and recent runs of one environment")
async def state(env: Env):
    def load():
        s = service.get_settings(env)
        nxt = service.next_auto_run(env, s)
        return {
            "env": env,
            "settings": s,
            "stats": store.stats(env),
            "runs": service.list_runs(env),
            "busy": service.is_busy(env),
            "next_auto_run": nxt.isoformat() if nxt else None,
            "now": datetime.now(service.ZONE).isoformat(),
            "scheduler": scheduler_running(),
            "whatsapp_ready": service.whatsapp_ready(),
            "health": service.health(env),
        }

    return await call(load)


@router.put("/{env}/settings", summary="Save settings")
async def update_settings(env: Env, body: SettingsUpdate):
    values = await call(service.validate_settings, body.model_dump(exclude_unset=True, exclude={"confirm"}))
    current = await call(service.get_settings, env)
    after = {**current, **values}
    turning_on_live = after["auto_enabled"] and after["auto_mode"] == "live" and not (
        current["auto_enabled"] and current["auto_mode"] == "live")
    if env == "prod" and turning_on_live and body.confirm != "LIVE":
        raise HTTPException(400, "Automatic live sending in production needs confirm = LIVE")
    await call(store.save_settings, env, values)
    return {"ok": True, "settings": await call(service.get_settings, env)}


@router.post("/{env}/run", status_code=202, summary="Start a run now (runs in the background)")
async def run_now(env: Env, body: RunRequest):
    if env == "prod" and body.mode == "live" and body.confirm != "LIVE":
        raise HTTPException(400, "Live production runs need confirm = LIVE")
    run_id = await call(service.start_run, env, body.mode, "manual")
    return {"ok": True, "run_id": run_id}


@router.get("/{env}/runs/{run_id}", summary="One run with every message")
async def run_detail(env: Env, run_id: int):
    r = await call(service.get_run, env, run_id)
    if not r:
        raise HTTPException(404, "No such run")
    return r


@router.get("/{env}/table/{title}", summary="Rows of a follow-up state table")
async def table(env: Env, title: Table, limit: int = Query(200, ge=1, le=1000)):
    return await call(store.table_rows, env, title, limit)


@router.get("/{env}/vendors", summary="Vendors with pending or escalated lines, furthest escalated first")
async def vendors(env: Env):
    return await call(trace.vendor_list, env)


@router.get("/{env}/vendors/{code}", summary="One vendor's Scenario 1 -> 2 -> 3 -> meeting trace")
async def vendor_trace(env: Env, code: str = PathParam(max_length=200)):
    t = await call(trace.vendor_trace, env, code)
    if t is None:
        raise HTTPException(404, f"No vendor {code} in the {env} data")
    return t


@router.post("/test/vendor-test", status_code=202, summary="Start a one-vendor escalation test (test data only)")
async def start_vendor_test(body: VendorTest):
    run_id = await call(service.start_vendor_test, body.vendor_code.strip(), body.mobile.strip(), body.minutes,
                        body.mode, body.auto)
    return {"ok": True, "run_id": run_id, "scheduler": scheduler_running()}


@router.post("/test/vendor-call", summary="Ring a number now with the agent reading one vendor's overdue lines")
async def vendor_call(body: VendorCall):
    return {"ok": True, **await call(service.call_vendor_now, body.vendor_code.strip(), body.mobile.strip())}


@router.post("/test/vendor-test/stop", summary="Stop the one-vendor test's automatic runs")
async def stop_vendor_test():
    await call(service.stop_vendor_test)
    return {"ok": True}


@router.post("/test/copy-from-prod", summary="Replace test data with a copy of production")
async def copy_from_prod():
    if service.is_busy("test"):
        raise HTTPException(409, "A test run is in progress")
    copied = await call(store.copy_prod_to_test)
    trace.clear_cache()
    return {"ok": True, "copied": copied}


@router.post("/test/clear", summary="Clear the test tracker, logs, tasks and run history")
async def clear_test():
    if service.is_busy("test"):
        raise HTTPException(409, "A test run is in progress")
    await call(store.clear_state, "test")
    return {"ok": True}


@router.post("/prod/sync-sheet", summary="Pull the Google Sheet into production now")
async def sync_sheet():
    result = await call(service.sync_sheet_to_prod)
    trace.clear_cache()
    return {"ok": True, "result": result}
