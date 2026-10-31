"""Admin page and API, served by the main web app (backend/voice_agent/app.py).

    GET  /admin          the admin page
    /admin/api/...       system tests and SCM users (needs an admin session in the X-Session header)

Also prepares the database at startup and runs the automatic scheduler (FOLLOWUP_SCHEDULER=false turns it off).
"""

import logging
import threading
from typing import Callable

import psycopg
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse

from backend.core import frontend
from backend.po_followup.config import env_bool
from backend.po_followup.db import store
from backend.po_followup.models.admin import CallCheck, MeetingCheck, UserCreate, UserUpdate, WhatsAppCheck
from backend.po_followup.services import followup as service
from backend.po_followup.services import system_tests, users

log = logging.getLogger("po_followup.admin")

PAGE = frontend.page("admin.html")


def require_admin(x_session: str | None = Header(None)) -> None:
    user = users.read_token(x_session)
    if not user or user["role"] != users.ADMIN:
        raise HTTPException(401, "Sign in as admin.")


_ready = False
_ready_lock = threading.Lock()
_scheduler = None


def _init() -> None:
    """Create the follow-up tables once; retried on the next request if PostgreSQL was down."""
    global _ready
    with _ready_lock:
        if _ready:
            return
        store.init()
        service.mark_interrupted_runs()
        _ready = True


async def require_ready() -> None:
    if _ready:
        return
    try:
        await run_in_threadpool(_init)
    except Exception as e:
        log.warning("Database not ready: %s", e)
        raise HTTPException(503, f"Database not reachable ({e})")


async def startup() -> None:
    """Called from the app lifespan: prepare the database and start the scheduler if enabled."""
    global _scheduler
    try:
        await run_in_threadpool(_init)
    except Exception as e:
        log.warning("Database not ready yet (%s); will retry on first use", e)
    # On by default: the run lock (store.RunLock) makes several schedulers safe
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
        log.warning("Database error: %s", e)
        raise HTTPException(503, "Database not reachable right now; retrying…")


# ---------- routes ----------

page_router = APIRouter(tags=["pages"])
router = APIRouter(prefix="/admin/api", tags=["admin"], dependencies=[Depends(require_admin), Depends(require_ready)])


@page_router.get("/admin", response_class=HTMLResponse, include_in_schema=False)
async def page():
    return HTMLResponse(PAGE.read_text(encoding="utf-8"))


# ---------- system tests: is WhatsApp, the voice call and the meeting invite working? ----------

@router.get("/checks", summary="Live connection check of WhatsApp, the voice call and meeting invites")
async def checks():
    return await call(system_tests.status)


@router.post("/checks/whatsapp", summary="Send a test WhatsApp message")
async def check_whatsapp(body: WhatsAppCheck):
    return await call(system_tests.whatsapp, body.mobile, body.message)


@router.post("/checks/call", summary="Ring a number with the voice agent and a sample vendor script")
async def check_call(body: CallCheck):
    return await call(system_tests.call, body.mobile)


@router.post("/checks/meeting", summary="Create a test Google Meet invite")
async def check_meeting(body: MeetingCheck):
    return await call(system_tests.meeting, body.email)


# ---------- SCM users ----------

@router.get("/users", summary="SCM users")
async def list_users():
    return await call(users.list_users)


@router.post("/users", status_code=201, summary="Create an SCM user (user id + password)")
async def create_user(body: UserCreate):
    return await call(users.create_user, body.username, body.password, body.display_name)


@router.patch("/users/{username}", summary="Reset the password, switch a user on or off")
async def update_user(username: str, body: UserUpdate):
    return await call(lambda: users.update_user(username, users.ADMIN, body.password, body.active, body.display_name))


@router.delete("/users/{username}", summary="Delete an SCM user")
async def delete_user(username: str):
    await call(users.delete_user, username)
    return {"ok": True}


@router.get("/audit", summary="Who did what")
async def audit(limit: int = Query(100, ge=1, le=500)):
    return await call(users.recent_audit, limit)
