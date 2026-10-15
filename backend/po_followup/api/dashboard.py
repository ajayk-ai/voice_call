"""Business dashboard (read-only), HTTP layer.

    GET /dashboard              the page (frontend/pages/dashboard.html)
    GET /dashboard/api/{env}    the data (code in X-Access-Code: DASHBOARD_PASSWORD, or the admin code)

The report itself is built in services.dashboard; its shape is models.dashboard.DashboardData.
"""

import os
import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from backend.core import frontend
from backend.po_followup.api import admin
from backend.po_followup.models.admin import Env
from backend.po_followup.models.dashboard import DashboardData
from backend.po_followup.services import dashboard as report

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


@router.get("/{env}", response_model=DashboardData, summary="Overdue PO lines and their Scenario 1 / 2 / 3 status")
async def data(env: Env):
    # Validate + serialise with pydantic directly: FastAPI's generic encoder is ~10x slower on ~2 MB of lines
    body = DashboardData.model_validate(await admin.call(report.overview, env)).model_dump_json()
    return Response(body, media_type="application/json")
