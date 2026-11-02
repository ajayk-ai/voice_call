"""SCM user page and API (frontend/pages/dashboard.html). Needs a session in the X-Session header.

    POST /dashboard/auth/login                    sign in as an SCM user or the admin (the page at / posts here)
    GET  /dashboard/api/vendor-numbers            every vendor: chosen? Scenario 1/2/3 numbers and e-mail ids
    PUT  /dashboard/api/vendor-numbers/{code}     add or change one vendor (written to the Google Sheet)
    POST /dashboard/api/vendor-numbers/bulk       many vendors from an upload, checked first
    PUT  /dashboard/api/vendor-selection          which vendors the follow-up runs for
    GET  /dashboard/api/control                   is the follow-up running, its schedule, next run, recent runs
    POST /dashboard/api/control/start|stop|run-now|restart
    POST /dashboard/api/control/emergency-stop|emergency-clear
    GET  /dashboard/api/trace[/{code}]            the chosen vendors' scenario, one vendor's full trace
    GET  /dashboard/api/calls[/insights|/{key}]   Scenario 3 calls: transcript, delay reasons, committed dates
"""

from typing import Literal

import psycopg
from fastapi import APIRouter, Depends, Header, HTTPException, Path as PathParam, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from backend.core import frontend
from backend.po_followup.api import admin
from backend.po_followup.models.dashboard import (
    BulkResult, BulkUpdate, ControlRestart, ControlStart, RunNow, VendorNumbers, VendorNumbersList, VendorNumbersUpdate, VendorSelection,
)
from backend.po_followup.services import calls, control, trace, users, vendor_numbers

PAGE = frontend.page("dashboard.html")


def require_user(x_session: str | None = Header(None)) -> dict:
    try:
        user = users.current(x_session)
    except psycopg.OperationalError:
        raise HTTPException(503, "Database not reachable right now; retrying…")
    if not user:
        raise HTTPException(401, "Your session has ended. Sign in again.")
    return user


page_router = APIRouter(tags=["pages"])
auth_router = APIRouter(prefix="/dashboard/auth", tags=["dashboard"], dependencies=[Depends(admin.require_ready)])
router = APIRouter(prefix="/dashboard/api", tags=["dashboard"],
                   dependencies=[Depends(admin.require_ready), Depends(require_user)])


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["scm", "admin"] = "scm"
    username: str = Field(max_length=60)
    password: str = Field(max_length=200)


@auth_router.post("/login", summary="Sign in as an SCM user or as the admin")
async def login(body: Login):
    try:
        return await admin.call(users.admin_login if body.role == "admin" else users.login, body.username, body.password)
    except users.AuthError as e:
        raise HTTPException(401, str(e))


@page_router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def page():
    return HTMLResponse(PAGE.read_text(encoding="utf-8"))


async def sheet_call(fn, *args):
    """Vendor changes are written to the Google Sheet; a failed write is the gateway's fault, not the user's."""
    try:
        return await admin.call(fn, *args)
    except vendor_numbers.SheetWriteFailed as e:
        raise HTTPException(502, str(e))


# ---------- Vendor Master ----------

@router.get("/vendor-numbers", response_model=VendorNumbersList, summary="Every vendor's numbers and e-mail ids")
async def numbers():
    return await admin.call(vendor_numbers.list_numbers)


@router.post("/vendor-numbers/bulk", response_model=BulkResult, summary="Numbers and e-mail ids of many vendors")
async def bulk_numbers(body: BulkUpdate):
    return await sheet_call(vendor_numbers.bulk, [r.model_dump() for r in body.rows], body.apply)


@router.put("/vendor-numbers/{code}", response_model=VendorNumbers, summary="Add or change one vendor")
async def save_numbers(body: VendorNumbersUpdate, code: str = PathParam(max_length=40)):
    return await sheet_call(vendor_numbers.save_one, code, body.name, body.numbers, body.emails)


@router.put("/vendor-selection", summary="Which vendors the follow-up runs for")
async def vendor_selection(body: VendorSelection):
    return await admin.call(vendor_numbers.set_selection, body.codes, body.scope, body.confirm)


# ---------- Vendor Control ----------

@router.get("/control", summary="Is the automatic follow-up running, and when does it run next")
async def control_status():
    return await admin.call(control.status)


@router.post("/control/start", summary="Start the automatic follow-up")
async def control_start(body: ControlStart, user: dict = Depends(require_user)):
    return await admin.call(control.start, body.schedule_type, body.every_minutes, body.daily_time, body.mode,
                            body.confirm, body.scope, body.codes, body.confirm_scope, user["username"])


@router.post("/control/stop", summary="Stop the automatic follow-up")
async def control_stop(user: dict = Depends(require_user)):
    return await admin.call(control.stop, user["username"])


@router.post("/control/restart", status_code=202, summary="Every vendor back to Scenario 1, then run")
async def control_restart(body: ControlRestart, user: dict = Depends(require_user)):
    return await admin.call(control.restart, body.schedule_type, body.every_minutes, body.daily_time, body.mode, body.confirm,
                            body.scope, body.codes, body.confirm_scope, body.confirm_restart, user["username"])


@router.post("/control/emergency-stop", summary="Stop everything now, including the run and calls in progress")
async def control_emergency_stop(user: dict = Depends(require_user)):
    return await admin.call(control.emergency_stop, user["username"])


@router.post("/control/emergency-clear", summary="Allow runs again after an emergency stop")
async def control_emergency_clear(user: dict = Depends(require_user)):
    return await admin.call(control.emergency_clear, user["username"])


@router.post("/control/run-now", status_code=202, summary="Run the follow-up once, now")
async def control_run_now(body: RunNow, user: dict = Depends(require_user)):
    return {"ok": True, "run_id": await admin.call(control.run_now, body.mode, body.confirm, user["username"])}


# ---------- Trace ----------

@router.get("/trace", summary="The vendors chosen for follow-up and the scenario each one is at")
async def chosen_trace():
    return await admin.call(control.chosen_vendors)


@router.get("/trace/{code}", summary="One vendor's trace: steps, contacts, lines, call and meeting tasks")
async def vendor_trace(code: str = PathParam(max_length=200)):
    t = await admin.call(trace.vendor_trace, code)
    if t is None:
        raise HTTPException(404, f"No vendor {code}")
    return t


# ---------- Call Results ----------

@router.get("/calls", summary="Agent calls, newest first, with what was understood from each")
async def call_list(vendor: str | None = Query(None, max_length=40), limit: int = Query(200, ge=1, le=1000)):
    return await admin.call(calls.list_calls, vendor, limit)


@router.get("/calls/insights", summary="Outcomes and delay reasons of the last days' calls")
async def call_insights(days: int = Query(30, ge=1, le=365)):
    return await admin.call(calls.insights, days)


@router.get("/calls/{key}", summary="One call: transcript, per-line status / committed date / delay reason")
async def call_detail(key: str = PathParam(max_length=40)):
    c = await admin.call(calls.get_call, key)
    if not c:
        raise HTTPException(404, "No such call")
    return c


@router.post("/calls/{key}/analyse", summary="Read the transcript again")
async def call_analyse(key: str = PathParam(max_length=40)):
    if not await admin.call(calls.get_call, key):
        raise HTTPException(404, "No such call")
    await admin.call(calls.analyse, key)
    return await admin.call(calls.get_call, key)
