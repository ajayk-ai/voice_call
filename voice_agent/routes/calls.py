"""Placing phone calls through FreJun (Teler), and FreJun's webhooks."""

import logging
import re
from contextlib import suppress

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from teler import AsyncClient as AsyncTelerClient
from teler.exceptions import TelerException

from ..bridge import public_url
from ..config import FREJUN_PHONE_NUMBER, TELER_API_KEY, TELER_TIMEOUT_SECONDS
from ..security import call_limiter, check_password, client_ip, mask_number

log = logging.getLogger("voice_agent.calls")
router = APIRouter()

PHONE_RE = re.compile(r"\+\d{8,15}")


def error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


@router.post("/api/call")
async def place_call(request: Request):
    """Web page 'Normal call' button: the agent rings the given phone number via FreJun."""
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("body is not an object")
    except ValueError:  # includes json.JSONDecodeError
        return error("Invalid request.", 400)

    ip = client_ip(request)
    if not check_password(body.get("password")):
        log.warning("Rejected call request from %s: wrong access code", ip)
        return error("Wrong access code.", 401)

    to_number = re.sub(r"[\s()-]", "", str(body.get("to", "")))
    if not PHONE_RE.fullmatch(to_number):
        return error("Enter the number with country code, e.g. +919876543210.", 400)

    if not (TELER_API_KEY and FREJUN_PHONE_NUMBER):
        return error("Phone calls are not configured on the server (TELER_API_KEY / FREJUN_PHONE_NUMBER).", 503)

    if not call_limiter.allow(ip):
        log.warning("Rate limit hit for %s", ip)
        return error("Too many calls. Please wait a few minutes and try again.", 429)

    base = public_url(request)
    try:
        async with AsyncTelerClient(api_key=TELER_API_KEY, timeout=TELER_TIMEOUT_SECONDS) as client:
            call = await client.voice.calls.create(
                from_number=FREJUN_PHONE_NUMBER,
                to_number=to_number,
                flow_url=f"{base}/flow",
                status_callback_url=f"{base}/call-status",
                record=True,
            )
    except TelerException as e:
        log.error("FreJun rejected call to %s: %s (%s) %s", mask_number(to_number), e.message, e.code, e.details)
        return error(f"FreJun error: {e.message}", 502)
    except httpx.TimeoutException:
        log.error("FreJun timed out placing call to %s", mask_number(to_number))
        return error("FreJun did not respond in time. Please try again.", 504)
    except httpx.HTTPError as e:
        log.error("Could not reach FreJun: %r", e)
        return error("Could not reach FreJun. Please try again.", 502)

    log.info("Calling %s, call id %s, flow %s/flow", mask_number(to_number), call.id, base)
    return {"ok": True, "call_id": call.id, "to": to_number}


@router.post("/flow")
async def flow(request: Request):
    """FreJun calls this when the call is answered. Returns a stream action to our websocket."""
    with suppress(Exception):
        log.info("Call answered: %s", await request.json())
    ws_base = public_url(request).replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    return JSONResponse(
        {
            "action": "stream",
            "ws_url": f"{ws_base}/media",
            "sample_rate": "16k",  # caller audio arrives as 16 kHz PCM16, which Gemini takes directly
            "chunk_size": 500,
            "record": True,
        }
    )


@router.post("/call-status")
async def call_status(request: Request):
    """FreJun status callback (ringing, answered, completed, ...)."""
    try:
        payload = await request.json()
    except ValueError:
        payload = (await request.body())[:500]
    log.info("Call status: %s", payload)
    return {"ok": True}
