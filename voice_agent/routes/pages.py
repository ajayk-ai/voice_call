"""Web page and health check."""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, PlainTextResponse

router = APIRouter()

INDEX_PAGE = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text(encoding="utf-8")


@router.get("/")
@router.get("/test")
async def index():
    """Web page: place a phone call with the agent, or talk to it in the browser."""
    return HTMLResponse(INDEX_PAGE)


@router.get("/health")
async def health():
    return PlainTextResponse("Voice agent server is running")
