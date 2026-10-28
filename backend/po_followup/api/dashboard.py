"""Business dashboard, HTTP layer.

    GET /dashboard                                  the page (frontend/pages/dashboard.html)
    GET /dashboard/api/{env}                        the data (code in X-Access-Code: DASHBOARD_PASSWORD, or the admin code)
    GET  /dashboard/api/{env}/vendor-numbers         every vendor: selected for follow-up? Scenario 1/2/3 numbers
    PUT  /dashboard/api/{env}/vendor-numbers/{code}  change one vendor's numbers (production: written to the Google Sheet)
    POST /dashboard/api/{env}/vendor-numbers/bulk    numbers / follow-up for many vendors (CSV upload), checked first
    PUT  /dashboard/api/{env}/vendor-selection       which vendors the follow-up runs for

The report itself is built in services.dashboard; its shape is models.dashboard.DashboardData.
Vendors are in services.vendor_numbers.
"""

import hashlib
import os
import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, Path as PathParam, Request
from fastapi.responses import HTMLResponse, Response

from backend.core import frontend
from backend.po_followup.api import admin
from backend.po_followup.models.admin import Env
from backend.po_followup.models.dashboard import (
    BulkResult, BulkUpdate, DashboardData, VendorNumbers, VendorNumbersList, VendorNumbersUpdate, VendorSelection,
)
from backend.po_followup.services import dashboard as report
from backend.po_followup.services import vendor_numbers

PAGE = frontend.page("dashboard.html")


def require_viewer(request: Request, x_access_code: str | None = Header(None)) -> None:
    """DASHBOARD_PASSWORD lets business users in without the admin code; the admin code works too."""
    codes = [c for c in (os.getenv("DASHBOARD_PASSWORD"), admin.admin_code()) if c]
    if not codes:
        if admin.is_local(request):
            return
        raise HTTPException(403, "Set DASHBOARD_PASSWORD on the server to open the dashboard from another machine.")
    given = (x_access_code or "").encode()
    if not any(secrets.compare_digest(given, c.encode()) for c in codes):
        raise HTTPException(401, "Wrong access code.")


page_router = APIRouter(tags=["pages"])
router = APIRouter(prefix="/dashboard/api", tags=["dashboard"],
                   dependencies=[Depends(require_viewer), Depends(admin.require_ready)])


@page_router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def page():
    return HTMLResponse(PAGE.read_text(encoding="utf-8"))


def etag(body: str, now: str) -> str:
    """Fingerprint of the report without its `now` stamp, so a refresh with nothing new answers 304."""
    return '"' + hashlib.blake2b(body.replace(now, "", 1).encode(), digest_size=16).hexdigest() + '"'


@router.get("/{env}", response_model=DashboardData, summary="Overdue PO lines and their Scenario 1 / 2 / 3 status",
            responses={304: {"description": "Not modified since the ETag in If-None-Match"}})
async def data(env: Env, if_none_match: str | None = Header(None)):
    # Validate + serialise with pydantic directly: FastAPI's generic encoder is ~10x slower on ~2 MB of lines
    model = DashboardData.model_validate(await admin.call(report.overview, env))
    body = model.model_dump_json()
    tag = etag(body, model.now)
    headers = {"ETag": tag, "Cache-Control": "no-cache"}
    if if_none_match == tag:  # the page already shows this: skip ~2 MB of download, parse and re-render
        return Response(status_code=304, headers=headers)
    return Response(body, media_type="application/json", headers=headers)


@router.get("/{env}/vendor-numbers", response_model=VendorNumbersList, summary="Every vendor's Scenario 1 / 2 / 3 numbers")
async def numbers(env: Env):
    return await admin.call(vendor_numbers.list_numbers, env)


@router.post("/{env}/vendor-numbers/bulk", response_model=BulkResult, summary="Numbers / follow-up for many vendors")
async def bulk_numbers(env: Env, body: BulkUpdate):
    try:
        return await admin.call(vendor_numbers.bulk, env, [r.model_dump() for r in body.rows], body.apply)
    except vendor_numbers.SheetWriteFailed as e:
        raise HTTPException(502, str(e))


@router.put("/{env}/vendor-numbers/{code}", response_model=VendorNumbers, summary="Change one vendor's Scenario numbers")
async def save_numbers(env: Env, body: VendorNumbersUpdate, code: str = PathParam(max_length=40)):
    try:
        return await admin.call(vendor_numbers.save_one, env, code, body.name, body.numbers)
    except vendor_numbers.SheetWriteFailed as e:
        raise HTTPException(502, str(e))


@router.put("/{env}/vendor-selection", summary="Which vendors the follow-up runs for")
async def vendor_selection(env: Env, body: VendorSelection):
    return await admin.call(vendor_numbers.set_selection, env, body.codes, body.scope, body.confirm)
