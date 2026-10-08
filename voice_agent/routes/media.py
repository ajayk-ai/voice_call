"""FreJun media stream: a live phone call bridged to Gemini Live."""

import base64
import binascii
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..audio import Downsampler
from ..bridge import new_session_id, run_bridge, safe_close
from ..config import GEMINI_MODEL
from ..gemini import LIVE_CONFIG, gemini_client, pcm16k, receive_turns
from ..prompts import GREETING_TRIGGER

log = logging.getLogger("voice_agent.media")
router = APIRouter()

# 3200 bytes = 200 ms of 8 kHz PCM16: agent audio is buffered to this size before going to FreJun
AUDIO_CHUNK_BYTES = 3200


@router.websocket("/media")
async def media(call_ws: WebSocket):
    sid = new_session_id()
    await call_ws.accept()
    log.info("[%s] FreJun stream connected", sid)

    try:
        async with gemini_client.aio.live.connect(model=GEMINI_MODEL, config=LIVE_CONFIG) as session:
            log.info("[%s] Gemini Live connected: %s", sid, GEMINI_MODEL)
            await session.send_realtime_input(text=GREETING_TRIGGER)

            async def call_to_gemini():
                bad_messages = 0
                async for text in call_ws.iter_text():
                    try:
                        msg = json.loads(text)
                        if msg.get("type") != "audio":
                            log.info("[%s] FreJun event: %s", sid, msg.get("type"))
                            continue
                        # caller audio: base64 PCM16 16 kHz
                        audio = base64.b64decode(msg["data"]["audio_b64"], validate=True)
                    except (ValueError, KeyError, TypeError, AttributeError, binascii.Error):
                        bad_messages += 1
                        if bad_messages <= 3:
                            log.warning("[%s] skipped malformed FreJun message: %.200s", sid, text)
                        continue
                    await session.send_realtime_input(audio=pcm16k(audio))

            async def gemini_to_call():
                downsampler = Downsampler()
                buffer = b""
                chunk_id = 0
                heard, said = "", ""

                async def flush():
                    nonlocal buffer, chunk_id
                    if buffer:
                        await call_ws.send_text(
                            json.dumps(
                                {"type": "audio", "audio_b64": base64.b64encode(buffer).decode(), "chunk_id": chunk_id}
                            )
                        )
                        chunk_id += 1
                        buffer = b""

                async for response in receive_turns(session):
                    if response.data:
                        # agent speech: 24 kHz PCM16 -> 8 kHz PCM16 -> FreJun
                        buffer += downsampler.process(response.data)
                        if len(buffer) >= AUDIO_CHUNK_BYTES:
                            await flush()

                    sc = response.server_content
                    if not sc:
                        continue
                    if sc.input_transcription and sc.input_transcription.text:
                        heard += sc.input_transcription.text
                    if sc.output_transcription and sc.output_transcription.text:
                        said += sc.output_transcription.text
                    if sc.interrupted:
                        # barge-in: drop pending audio and clear what FreJun has queued
                        buffer = b""
                        downsampler.reset()
                        await call_ws.send_text(json.dumps({"type": "clear"}))
                    if sc.generation_complete or sc.turn_complete:
                        await flush()
                    if sc.turn_complete:
                        if heard.strip():
                            log.info("[%s] user: %s", sid, heard.strip())
                        if said.strip():
                            log.info("[%s] assistant: %s", sid, said.strip())
                        heard, said = "", ""

            await run_bridge(sid, call_to_gemini(), gemini_to_call())
    except WebSocketDisconnect:
        log.info("[%s] FreJun stream disconnected", sid)
    except Exception:
        log.exception("[%s] call session failed", sid)
    finally:
        await safe_close(call_ws)
        log.info("[%s] call session closed", sid)
