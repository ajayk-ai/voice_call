"""Web page, health check and what the server has configured."""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, PlainTextResponse

from backend.core import frontend
from backend.po_followup.api import admin

from backend.voice_agent import call_context
from backend.voice_agent.config import APP_PASSWORD, FREJUN_PHONE_NUMBER, GEMINI_API_KEY, TELER_API_KEY

router = APIRouter(tags=["pages"])

INDEX_PAGE = frontend.page("index.html").read_text(encoding="utf-8")


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
@router.get("/test", response_class=HTMLResponse, include_in_schema=False)
async def index():
    """Web page: place a phone call with the agent, or talk to it in the browser."""
    return HTMLResponse(INDEX_PAGE)


@router.get("/health", response_class=PlainTextResponse)
async def health():
    # Tells a caller this server understands signed vendor call scripts (see backend/po_followup/integrations/agent_call.py)
    return PlainTextResponse("Voice agent server is running", headers={"X-Call-Script": call_context.VERSION})


@router.get("/api/status", summary="Which features this server has configured")
async def status():
    """Booleans only (no secrets), so the pages can explain what is switched off."""
    return {
        "voice_agent": bool(GEMINI_API_KEY),
        "phone_calls": bool(GEMINI_API_KEY and TELER_API_KEY and FREJUN_PHONE_NUMBER),
        "access_code_required": bool(APP_PASSWORD),
        "admin": admin.available(),
    }
