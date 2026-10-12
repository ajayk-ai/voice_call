"""FreJun media stream: a live phone call bridged to Gemini Live."""

import base64
import binascii
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from monitoring.tracing import SessionTrace, mask_phones

from .. import call_context
from ..audio import Downsampler
from ..bridge import new_session_id, run_bridge, safe_close
from ..config import GEMINI_MODEL, GEMINI_VOICE
from ..gemini import LIVE_CONFIG, gemini_client, live_config, pcm16k, receive_turns
from ..prompts import GREETING_TRIGGER
from ..turns import TurnRecorder

log = logging.getLogger("voice_agent.media")
router = APIRouter()

# 1600 bytes = 100 ms of 8 kHz PCM16: agent audio is buffered to this size before going to FreJun.
# The first audio of each reply goes out at once, so buffering never delays the start of a reply.
AUDIO_CHUNK_BYTES = 1600
# Caller audio from FreJun is PCM16 at 16 kHz: 32 bytes per millisecond
CALLER_BYTES_PER_MS = 32


@router.websocket("/media")
async def media(call_ws: WebSocket):
    sid = new_session_id()
    await call_ws.accept()
    ctx_token = call_ws.query_params.get("ctx")
    context = call_context.decode(ctx_token)
    if ctx_token and not context:
        log.warning("[%s] call script token invalid or expired, using the sample script", sid)
    greeting = context["greeting"] if context else GREETING_TRIGGER
    config = live_config(context["prompt"]) if context else LIVE_CONFIG
    log.info("[%s] FreJun stream connected, vendor: %s", sid, context.get("vendor") if context else "sample")
    trace = SessionTrace(
        "phone_call",
        inputs={"greeting": greeting},
        metadata={"session_id": sid, "model": GEMINI_MODEL, "voice": GEMINI_VOICE, "channel": "frejun",
                  "vendor": context.get("vendor") if context else "sample"},
        tags=["voice-agent", "phone"],
    )
    recorder = TurnRecorder(trace)
    stats = {"frejun_events": [], "malformed_messages": 0, "caller_frame_ms": None}
    error = None

    try:
        async with gemini_client().aio.live.connect(model=GEMINI_MODEL, config=config) as session:
            log.info("[%s] Gemini Live connected: %s", sid, GEMINI_MODEL)
            await session.send_realtime_input(text=greeting)

            async def call_to_gemini():
                async for text in call_ws.iter_text():
                    try:
                        msg = json.loads(text)
                        if msg.get("type") != "audio":
                            log.info("[%s] FreJun event: %s", sid, msg.get("type"))
                            stats["frejun_events"].append(msg.get("type"))
                            if msg.get("type") == "start":  # call details (ids) for finding this call in FreJun
                                trace.add_metadata({"frejun_start": mask_phones(json.dumps(msg))[:1000]})
                            continue
                        # caller audio: base64 PCM16 16 kHz
                        audio = base64.b64decode(msg["data"]["audio_b64"], validate=True)
                    except (ValueError, KeyError, TypeError, AttributeError, binascii.Error):
                        stats["malformed_messages"] += 1
                        if stats["malformed_messages"] <= 3:
                            log.warning("[%s] skipped malformed FreJun message: %.200s", sid, text)
                        continue
                    if stats["caller_frame_ms"] is None:  # how much audio FreJun batches per message (adds delay)
                        stats["caller_frame_ms"] = round(len(audio) / CALLER_BYTES_PER_MS)
                        log.info("[%s] caller audio arrives in %d ms frames", sid, stats["caller_frame_ms"])
                    await session.send_realtime_input(audio=pcm16k(audio))

            async def gemini_to_call():
                downsampler = Downsampler()
                buffer = b""
                chunk_id = 0
                replying = False  # True once this reply's first audio went out

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
                        if not replying or len(buffer) >= AUDIO_CHUNK_BYTES:
                            replying = True
                            await flush()

                    completed = recorder.observe(response)
                    sc = response.server_content
                    if not sc:
                        continue
                    if sc.interrupted:
                        # barge-in: drop pending audio and clear what FreJun has queued
                        buffer = b""
                        replying = False
                        downsampler.reset()
                        await call_ws.send_text(json.dumps({"type": "clear"}))
                    if sc.generation_complete or sc.turn_complete:
                        await flush()
                        replying = False
                    if completed:
                        user, assistant = completed
                        if user:
                            log.info("[%s] user: %s", sid, user)
                        if assistant:
                            log.info("[%s] assistant (replied after %s ms): %s", sid, recorder.last_reply_ms, assistant)

            await run_bridge(sid, call_to_gemini(), gemini_to_call())
    except WebSocketDisconnect:
        log.info("[%s] FreJun stream disconnected", sid)
    except Exception as e:
        log.exception("[%s] call session failed", sid)
        error = repr(e)
    finally:
        await safe_close(call_ws)
        trace.end(outputs={"interruptions": recorder.interruptions, **stats}, error=error)
        log.info("[%s] call session closed", sid)
