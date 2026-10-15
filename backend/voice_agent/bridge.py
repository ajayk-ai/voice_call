"""Helpers shared by the websocket audio bridges (phone call and browser)."""

import asyncio
import logging
import os
import uuid
from contextlib import suppress

from fastapi import Request, WebSocket, WebSocketDisconnect

from .config import MAX_SESSION_SECONDS

log = logging.getLogger("voice_agent.bridge")


def new_session_id() -> str:
    return uuid.uuid4().hex[:8]


def public_url(request: Request) -> str:
    """Base URL FreJun uses to reach this server.

    On Render, RENDER_EXTERNAL_URL is always the live service URL, so it wins over PUBLIC_URL
    (a stale PUBLIC_URL pointing at a deleted service makes FreJun hang up on answer)."""
    url = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("PUBLIC_URL") or f"https://{request.headers.get('host')}"
    return url.rstrip("/")


async def run_bridge(session_id: str, *coros) -> None:
    """Run the two halves of an audio bridge until either one ends, one fails,
    or the session hits MAX_SESSION_SECONDS. Always cancels and awaits both."""
    tasks = [asyncio.create_task(c) for c in coros]
    try:
        done, _ = await asyncio.wait(tasks, timeout=MAX_SESSION_SECONDS, return_when=asyncio.FIRST_COMPLETED)
        if not done:
            log.warning("[%s] max session length (%ss) reached, hanging up", session_id, MAX_SESSION_SECONDS)
        for task in done:
            if task.cancelled():
                continue
            exc = task.exception()
            if isinstance(exc, WebSocketDisconnect):
                log.info("[%s] client disconnected", session_id)
            elif exc:
                log.error("[%s] stream error: %r", session_id, exc, exc_info=exc)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def safe_close(ws: WebSocket, code: int = 1000) -> None:
    with suppress(Exception):
        await ws.close(code=code)
