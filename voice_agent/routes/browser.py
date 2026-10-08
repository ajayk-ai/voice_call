"""Browser 'Voice agent' tab: talk to the agent with the mic, no phone call.

Protocol: mic PCM16 16 kHz in (binary), agent PCM16 24 kHz out (binary),
transcripts and control messages as JSON text."""

import asyncio
import json
import logging
from contextlib import suppress

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..bridge import new_session_id, run_bridge, safe_close
from ..config import GEMINI_MODEL
from ..gemini import LIVE_CONFIG, gemini_client, pcm16k, receive_turns
from ..prompts import GREETING_TRIGGER
from ..security import check_password

log = logging.getLogger("voice_agent.browser")
router = APIRouter()

# Largest mic frame accepted from the browser (the page sends ~3.2 KB every 100 ms)
MAX_BROWSER_FRAME_BYTES = 64 * 1024
# Websocket close code for a wrong access code
WS_CLOSE_UNAUTHORIZED = 4401
AUTH_TIMEOUT_SECONDS = 10


async def read_auth_code(ws: WebSocket) -> str | None:
    """The page sends {"type": "auth", "code": ...} as its first message.
    The code is not put in the URL, because URLs end up in access logs."""
    try:
        first = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=AUTH_TIMEOUT_SECONDS))
    except (asyncio.TimeoutError, ValueError, KeyError, WebSocketDisconnect):
        return None
    if isinstance(first, dict) and first.get("type") == "auth":
        return first.get("code")
    return None


@router.websocket("/test-ws")
async def test_ws(browser_ws: WebSocket):
    sid = new_session_id()
    await browser_ws.accept()

    async def send_json(data: dict):
        await browser_ws.send_text(json.dumps(data, ensure_ascii=False))

    if not check_password(await read_auth_code(browser_ws)):
        log.warning("[%s] browser session rejected: wrong access code", sid)
        with suppress(Exception):
            await send_json({"type": "error", "text": "Wrong access code."})
        await safe_close(browser_ws, WS_CLOSE_UNAUTHORIZED)
        return

    log.info("[%s] browser session started", sid)
    try:
        async with gemini_client.aio.live.connect(model=GEMINI_MODEL, config=LIVE_CONFIG) as session:
            await send_json({"type": "ready"})
            await session.send_realtime_input(text=GREETING_TRIGGER)

            async def browser_to_gemini():
                while True:
                    msg = await browser_ws.receive()
                    if msg["type"] == "websocket.disconnect":
                        return
                    data = msg.get("bytes")
                    if not data:
                        continue
                    if len(data) > MAX_BROWSER_FRAME_BYTES:
                        log.warning("[%s] dropped oversized mic frame (%d bytes)", sid, len(data))
                        continue
                    await session.send_realtime_input(audio=pcm16k(data))

            async def gemini_to_browser():
                async for response in receive_turns(session):
                    if response.data:
                        await browser_ws.send_bytes(response.data)
                    sc = response.server_content
                    if not sc:
                        continue
                    if sc.input_transcription and sc.input_transcription.text:
                        await send_json({"type": "transcript", "role": "user", "text": sc.input_transcription.text})
                    if sc.output_transcription and sc.output_transcription.text:
                        await send_json({"type": "transcript", "role": "assistant", "text": sc.output_transcription.text})
                    if sc.interrupted:
                        await send_json({"type": "clear"})
                    if sc.turn_complete:
                        await send_json({"type": "turn_complete"})

            await run_bridge(sid, browser_to_gemini(), gemini_to_browser())
    except WebSocketDisconnect:
        log.info("[%s] browser disconnected", sid)
    except Exception:
        log.exception("[%s] browser session failed", sid)
        with suppress(Exception):
            await send_json({"type": "error", "text": "The agent is unavailable right now. Please try again."})
    finally:
        await safe_close(browser_ws)
        log.info("[%s] browser session closed", sid)
