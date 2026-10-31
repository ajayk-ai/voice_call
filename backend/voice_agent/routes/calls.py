"""Placing phone calls through FreJun (Teler), and FreJun's webhooks."""

import json
import logging
from contextlib import suppress
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from teler import AsyncClient as AsyncTelerClient

from backend.core.tracing import mask_phones, traceable

from backend.voice_agent import call_context, call_log
from backend.voice_agent.bridge import public_url
from backend.voice_agent.config import FREJUN_PHONE_NUMBER, TELER_API_KEY, TELER_TIMEOUT_SECONDS
from backend.voice_agent.security import mask_number

log = logging.getLogger("voice_agent.calls")
router = APIRouter()


@traceable(
    name="place_call",
    run_type="tool",
    tags=["voice-agent", "frejun"],
    process_inputs=lambda i: {"to": mask_number(i["to_number"]), "flow_url": f"{i['base']}/flow"},
)
async def create_call(to_number: str, base: str, ctx: str | None = None):
    """Ask FreJun to ring `to_number`; FreJun then calls /flow when it is answered.
    `ctx` is a call_context token: the call then uses that vendor's script instead of the sample one."""
    params = {"ticket": call_context.ticket(), **({"ctx": ctx} if ctx else {})}
    async with AsyncTelerClient(api_key=TELER_API_KEY, timeout=TELER_TIMEOUT_SECONDS) as client:
        call = await client.voice.calls.create(
            from_number=FREJUN_PHONE_NUMBER,
            to_number=to_number,
            flow_url=f"{base}/flow?{urlencode(params)}",
            status_callback_url=f"{base}/call-status",
            record=True,
        )
    return {"call_id": call.id}


@traceable(name="call_status", run_type="tool", tags=["voice-agent", "frejun"])
def record_call_status(payload: dict) -> dict:
    """FreJun status callback, traced so hang-up reasons are visible next to the calls."""
    return {"status": payload.get("status") or payload.get("event") or payload.get("type")}


@router.post("/flow")
async def flow(request: Request):
    """FreJun calls this when the call is answered. Returns a stream action to our websocket."""
    with suppress(Exception):
        log.info("Call answered: %s", await request.json())
    ws_base = public_url(request).replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    # The call ticket and vendor script travel on to /media (see call_context)
    params = {k: request.query_params[k] for k in ("ticket", "ctx") if request.query_params.get(k)}
    return JSONResponse(
        {
            "action": "stream",
            "ws_url": f"{ws_base}/media" + (f"?{urlencode(params)}" if params else ""),
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
    if isinstance(payload, dict):
        record_call_status(json.loads(mask_phones(json.dumps(payload))))
        await call_log.status_event(payload)
    return {"ok": True}
