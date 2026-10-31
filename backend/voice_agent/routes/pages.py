"""Login page and health check."""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, PlainTextResponse

from backend.core import frontend

from backend.voice_agent import call_context

router = APIRouter(tags=["pages"])

LOGIN_PAGE = frontend.page("login.html")


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def login():
    """The app's first page: choose SCM user or Admin and sign in."""
    return HTMLResponse(LOGIN_PAGE.read_text(encoding="utf-8"))


@router.get("/health", response_class=PlainTextResponse)
async def health():
    # Tells a caller this server understands signed vendor call scripts (see backend/po_followup/integrations/agent_call.py)
    return PlainTextResponse("Voice agent server is running", headers={"X-Call-Script": call_context.VERSION})
