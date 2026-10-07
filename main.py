import asyncio
import base64
import binascii
import json
import logging
import os
import re
import secrets
import sys
import time
import uuid
from collections import defaultdict, deque
from contextlib import suppress
from pathlib import Path

import httpx
import numpy as np
import truststore

# Use the OS certificate store, so HTTPS works behind corporate SSL inspection (e.g. Sophos)
truststore.inject_into_ssl()

from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from google import genai
from google.genai import types
from teler import AsyncClient as AsyncTelerClient
from teler.exceptions import TelerException

load_dotenv()

# Log lines go out immediately (Render logs pipe stdout, which is otherwise block-buffered)
sys.stdout.reconfigure(line_buffering=True)
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("voice_agent")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise RuntimeError("Missing GEMINI_API_KEY in .env")

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-live")
GEMINI_VOICE = os.getenv("GEMINI_VOICE", "Achird")

TELER_API_KEY = os.getenv("TELER_API_KEY")
FREJUN_PHONE_NUMBER = os.getenv("FREJUN_PHONE_NUMBER")
# Optional but recommended: if set, the web page must send this code to place calls or use the browser agent
APP_PASSWORD = os.getenv("APP_PASSWORD")


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        log.warning("Invalid %s=%r, using %d", name, os.getenv(name), default)
        return default


# Hard stop for any single call or browser session, so a stuck call can't burn credits
MAX_SESSION_SECONDS = env_int("MAX_SESSION_SECONDS", 15 * 60)
# Phone calls each client IP may place per CALL_RATE_WINDOW seconds
CALL_RATE_LIMIT = env_int("CALL_RATE_LIMIT", 5)
CALL_RATE_WINDOW = env_int("CALL_RATE_WINDOW", 10 * 60)
TELER_TIMEOUT_SECONDS = 20

if not (TELER_API_KEY and FREJUN_PHONE_NUMBER):
    log.warning("TELER_API_KEY / FREJUN_PHONE_NUMBER not set: phone calls are disabled")
if not APP_PASSWORD:
    log.warning("APP_PASSWORD not set: anyone with the URL can place calls and use the agent")

PO_FOLLOWUP_PROMPT = """\
You are the Bull Machines Supply Chain (SCM) assistant, calling a vendor about overdue purchase orders.
This is a voice call: keep every reply short (1-2 sentences), polite and professional. Ask one question at a time.

VENDOR: Super Springs Private Limited (vendor code 4 0 0 1 8 6)

OVERDUE PO LINES (all for item "Bonnet Assembly, 76 HP"):
1. PO 4 5 0 0 5 6 7 1 0 1, line 10 - due 7 September 2026 - 3 pieces pending - 16 days late
2. PO 4 5 0 0 5 6 8 0 5 2, line 10 - due 11 September 2026 - 8 pieces pending - 12 days late
3. PO 4 5 0 0 5 6 8 0 5 2, line 20 - due 11 September 2026 - 8 pieces pending - 12 days late
Summary: 3 overdue lines across 2 POs, maximum 16 days overdue.

Always say PO numbers digit by digit exactly as written above.

CALL FLOW:
1. Confirm you are speaking with someone from Super Springs who handles dispatch or planning. Ask for their name.
   If they are not the right person, ask who to contact and their phone number, thank them, and end politely.
2. Briefly explain: 3 PO lines for Bonnet Assembly 76 HP are overdue for dispatch, up to 16 days late.
3. Go through the PO lines ONE AT A TIME. For each line ask:
   a. Current dispatch status: already dispatched, ready to dispatch, or still in production?
   b. If dispatched: dispatch date and vehicle / LR / invoice number.
   c. If not dispatched: the expected dispatch date, and whether the full pending quantity will go or only part of it.
   d. The reason for the delay (material shortage, capacity, quality issue, payment, etc.).
   Repeat each answer back briefly to confirm before moving to the next line.
4. Ask if there is any support needed from Bull Machines to speed up dispatch.
5. Read back a short summary: for each PO line, the status and committed date. Ask them to confirm it is correct.
6. Ask them to also reply by email mentioning the PO numbers, thank them, and say goodbye.

RULES:
- Never invent dates, quantities or statuses; only record what the vendor says.
- If an answer is vague (for example "soon" or "next week"), politely ask for a specific date.
- If they ask something you do not know (prices, new orders, payments), say the SCM team will follow up by email.

LANGUAGE:
- Start in English. You can speak English, Hindi and Tamil.
- Reply in the language the vendor speaks. If they switch language or ask for one, switch with them.
- Keep PO numbers, dates and quantities clear in every language.
"""

# Sent as the first user turn so the agent speaks first when the call connects.
GREETING_TRIGGER = (
    "The call has just connected. Greet them in English: say you are the Bull Machines supply chain "
    "assistant calling Super Springs Private Limited about overdue purchase order dispatches, "
    "and ask if you are speaking with someone from dispatch or planning."
)

LIVE_CONFIG = types.LiveConnectConfig(
    response_modalities=["AUDIO"],
    system_instruction=PO_FOLLOWUP_PROMPT,
    speech_config=types.SpeechConfig(
        voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=GEMINI_VOICE)
        )
    ),
    input_audio_transcription=types.AudioTranscriptionConfig(),
    output_audio_transcription=types.AudioTranscriptionConfig(),
    realtime_input_config=types.RealtimeInputConfig(
        automatic_activity_detection=types.AutomaticActivityDetection(
            start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_LOW,
            end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_HIGH,
            prefix_padding_ms=150,
            silence_duration_ms=500,
        )
    ),
)

gemini_client = genai.Client(api_key=GEMINI_API_KEY)


class Downsampler:
    """24 kHz PCM16 (Gemini output) -> 8 kHz PCM16 (what FreJun plays).

    Low-pass filters before dropping samples so speech doesn't alias, and keeps
    filter state across chunks so there are no clicks at chunk boundaries.
    """

    FACTOR = 3
    TAPS = 63

    def __init__(self):
        n = np.arange(self.TAPS) - (self.TAPS - 1) / 2
        cutoff = 3400 / 24000  # just under the 4 kHz Nyquist of 8 kHz audio
        h = 2 * cutoff * np.sinc(2 * cutoff * n) * np.hamming(self.TAPS)
        self.kernel = h / h.sum()
        self.reset()

    def reset(self):
        self.history = np.zeros(self.TAPS - 1)
        self.phase = 0

    def process(self, pcm: bytes) -> bytes:
        x = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
        if not len(x):
            return b""
        buf = np.concatenate([self.history, x])
        y = np.convolve(buf, self.kernel, mode="valid")
        out = y[self.phase :: self.FACTOR]
        self.phase = (self.phase - len(y)) % self.FACTOR
        self.history = buf[-(self.TAPS - 1) :]
        return np.clip(np.round(out), -32768, 32767).astype(np.int16).tobytes()


app = FastAPI(title="PO Voice Agent")

INDEX_PAGE = (Path(__file__).parent / "index.html").read_text(encoding="utf-8")

# 3200 bytes = 200 ms of 8 kHz PCM16: agent audio is buffered to this size before going to FreJun
AUDIO_CHUNK_BYTES = 3200
# Largest mic frame accepted from the browser (the page sends ~3.2 KB every 100 ms)
MAX_BROWSER_FRAME_BYTES = 64 * 1024
# Browser websocket close code for a wrong access code
WS_CLOSE_UNAUTHORIZED = 4401

PHONE_RE = re.compile(r"\+\d{8,15}")


def new_session_id() -> str:
    return uuid.uuid4().hex[:8]


def error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def check_password(given) -> bool:
    """Constant-time compare, so the access code can't be guessed from response timing."""
    if not APP_PASSWORD:
        return True
    return isinstance(given, str) and secrets.compare_digest(given.encode(), APP_PASSWORD.encode())


def client_ip(request: Request) -> str:
    # Render sits behind a proxy; the first X-Forwarded-For entry is the real client
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class RateLimiter:
    """Sliding-window limit per key. In memory: fine for a single Render instance."""

    def __init__(self, limit: int, window_seconds: int):
        self.limit = limit
        self.window = window_seconds
        self.hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        hits = self.hits[key]
        while hits and now - hits[0] > self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True


call_limiter = RateLimiter(CALL_RATE_LIMIT, CALL_RATE_WINDOW)


def public_url(request: Request) -> str:
    """Base URL FreJun uses to reach this server. Render sets RENDER_EXTERNAL_URL automatically."""
    url = os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or f"https://{request.headers.get('host')}"
    return url.rstrip("/")


def mask_number(number: str) -> str:
    """Keep full phone numbers out of the logs."""
    return number[:4] + "*" * max(len(number) - 7, 0) + number[-3:]


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


async def receive_turns(session):
    """Yield every Gemini Live message for the whole session.

    session.receive() ends after each model turn, so it is called again in a loop.
    If a pass yields nothing, the connection has closed: stop instead of spinning."""
    while True:
        got_any = False
        async for response in session.receive():
            got_any = True
            yield response
        if not got_any:
            log.info("Gemini session ended")
            return


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception):
    log.exception("Unhandled error on %s %s", request.method, request.url.path)
    return error("Internal server error.", 500)


@app.get("/")
@app.get("/test")
async def index():
    """Web page: place a phone call with the agent, or talk to it in the browser."""
    return HTMLResponse(INDEX_PAGE)


@app.get("/health")
async def health():
    return PlainTextResponse("Voice agent server is running")


@app.post("/api/call")
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

    log.info("Calling %s, call id %s", mask_number(to_number), call.id)
    return {"ok": True, "call_id": call.id, "to": to_number}


@app.post("/flow")
async def flow(request: Request):
    """FreJun (Teler) calls this when the call is answered. Returns a stream action to our websocket."""
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


@app.post("/call-status")
async def call_status(request: Request):
    """FreJun status callback (ringing, answered, completed, ...)."""
    try:
        payload = await request.json()
    except ValueError:
        payload = (await request.body())[:500]
    log.info("Call status: %s", payload)
    return {"ok": True}


@app.websocket("/test-ws")
async def test_ws(browser_ws: WebSocket):
    """Browser test bridge: mic PCM16 16 kHz in (binary), agent PCM16 24 kHz out (binary),
    transcripts and control messages as JSON text."""
    sid = new_session_id()
    await browser_ws.accept()

    async def send_json(data: dict):
        await browser_ws.send_text(json.dumps(data, ensure_ascii=False))

    # The page sends {"type": "auth", "code": ...} as its first message. The code is not put in the URL,
    # because URLs end up in access logs.
    try:
        first = json.loads(await asyncio.wait_for(browser_ws.receive_text(), timeout=10))
        code = first.get("code") if isinstance(first, dict) and first.get("type") == "auth" else None
    except (asyncio.TimeoutError, ValueError, KeyError, WebSocketDisconnect):
        code = None
    if not check_password(code):
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
                    await session.send_realtime_input(audio=types.Blob(data=data, mime_type="audio/pcm;rate=16000"))

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


@app.websocket("/media")
async def media(call_ws: WebSocket):
    """FreJun media stream for a live phone call, bridged to Gemini Live."""
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
                    await session.send_realtime_input(audio=types.Blob(data=audio, mime_type="audio/pcm;rate=16000"))

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
