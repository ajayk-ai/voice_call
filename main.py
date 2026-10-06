import asyncio
import base64
import json
import os
import re
import sys
from pathlib import Path

import numpy as np
import truststore

# Use the OS certificate store, so HTTPS works behind corporate SSL inspection (e.g. Sophos)
truststore.inject_into_ssl()

from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from google import genai
from google.genai import types
from teler import AsyncClient as AsyncTelerClient
from teler.exceptions import TelerException

load_dotenv()

# Print transcripts immediately (Render logs pipe stdout, which is otherwise block-buffered)
sys.stdout.reconfigure(line_buffering=True)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise RuntimeError("Missing GEMINI_API_KEY in .env")

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-live")
GEMINI_VOICE = os.getenv("GEMINI_VOICE", "Achird")

TELER_API_KEY = os.getenv("TELER_API_KEY")
FREJUN_PHONE_NUMBER = os.getenv("FREJUN_PHONE_NUMBER")
# Optional: if set, the web page must send this code before it can place a phone call
APP_PASSWORD = os.getenv("APP_PASSWORD")

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


app = FastAPI()


INDEX_PAGE = (Path(__file__).parent / "index.html").read_text(encoding="utf-8")


@app.get("/")
@app.get("/test")
async def index():
    """Web page: place a phone call with the agent, or talk to it in the browser."""
    return HTMLResponse(INDEX_PAGE)


@app.get("/health")
async def health():
    return PlainTextResponse("Voice agent server is running")


def public_url(request: Request) -> str:
    """Base URL FreJun uses to reach this server. Render sets RENDER_EXTERNAL_URL automatically."""
    url = os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or f"https://{request.headers.get('host')}"
    return url.rstrip("/")


@app.post("/api/call")
async def place_call(request: Request):
    """Web page 'Normal call' button: the agent rings the given phone number via FreJun."""
    body = await request.json()
    if APP_PASSWORD and body.get("password") != APP_PASSWORD:
        return JSONResponse({"error": "Wrong access code."}, status_code=401)

    to_number = re.sub(r"[\s()-]", "", str(body.get("to", "")))
    if not re.fullmatch(r"\+\d{8,15}", to_number):
        return JSONResponse({"error": "Enter the number with country code, e.g. +919876543210."}, status_code=400)

    if not (TELER_API_KEY and FREJUN_PHONE_NUMBER):
        return JSONResponse({"error": "TELER_API_KEY / FREJUN_PHONE_NUMBER are not set on the server."}, status_code=500)

    base = public_url(request)
    try:
        async with AsyncTelerClient(api_key=TELER_API_KEY) as client:
            call = await client.voice.calls.create(
                from_number=FREJUN_PHONE_NUMBER,
                to_number=to_number,
                flow_url=f"{base}/flow",
                status_callback_url=f"{base}/call-status",
                record=True,
            )
    except TelerException as e:
        print("Call failed:", repr(e), e.details)
        return JSONResponse({"error": f"FreJun error: {e.message}"}, status_code=502)
    print("Calling", to_number, "call id:", call.id)
    return {"ok": True, "call_id": call.id, "to": to_number}


@app.post("/flow")
async def flow(request: Request):
    """FreJun (Teler) calls this when the call is answered. Returns a stream action to our websocket."""
    host = request.headers.get("host")
    return JSONResponse(
        {
            "action": "stream",
            "ws_url": f"wss://{host}/media",
            "sample_rate": "16k",  # caller audio arrives as 16 kHz PCM16, which Gemini takes directly
            "chunk_size": 500,
            "record": True,
        }
    )


@app.websocket("/test-ws")
async def test_ws(browser_ws: WebSocket):
    """Browser test bridge: mic PCM16 16 kHz in (binary), agent PCM16 24 kHz out (binary),
    transcripts and control messages as JSON text."""
    await browser_ws.accept()
    print("Browser test connected")

    async def send_json(data: dict):
        await browser_ws.send_text(json.dumps(data, ensure_ascii=False))

    try:
        async with gemini_client.aio.live.connect(model=GEMINI_MODEL, config=LIVE_CONFIG) as session:
            await send_json({"type": "ready"})
            await session.send_realtime_input(text=GREETING_TRIGGER)

            async def browser_to_gemini():
                while True:
                    msg = await browser_ws.receive()
                    if msg["type"] == "websocket.disconnect":
                        return
                    if msg.get("bytes"):
                        await session.send_realtime_input(
                            audio=types.Blob(data=msg["bytes"], mime_type="audio/pcm;rate=16000")
                        )

            async def gemini_to_browser():
                while True:
                    async for response in session.receive():
                        if response.data:
                            await browser_ws.send_bytes(response.data)
                        sc = response.server_content
                        if not sc:
                            continue
                        if sc.input_transcription and sc.input_transcription.text:
                            await send_json({"type": "transcript", "role": "user", "text": sc.input_transcription.text})
                        if sc.output_transcription and sc.output_transcription.text:
                            await send_json(
                                {"type": "transcript", "role": "assistant", "text": sc.output_transcription.text}
                            )
                        if sc.interrupted:
                            await send_json({"type": "clear"})
                        if sc.turn_complete:
                            await send_json({"type": "turn_complete"})

            tasks = [asyncio.create_task(browser_to_gemini()), asyncio.create_task(gemini_to_browser())]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    if t.exception():
                        print("Test stream error:", repr(t.exception()))
            finally:
                for t in tasks:
                    t.cancel()
    except Exception as e:
        print("Test session error:", repr(e))
        try:
            await send_json({"type": "error", "text": str(e)})
            await browser_ws.close()
        except Exception:
            pass
    print("Browser test closed")


@app.post("/call-status")
async def call_status(request: Request):
    """FreJun status callback (ringing, answered, completed, ...)."""
    print("Call status:", await request.json())
    return {"ok": True}


# Agent audio is buffered before sending to FreJun; 3200 bytes = 200 ms of 8 kHz PCM16.
AUDIO_CHUNK_BYTES = 3200


@app.websocket("/media")
async def media(call_ws: WebSocket):
    await call_ws.accept()
    print("FreJun stream connected")

    async with gemini_client.aio.live.connect(model=GEMINI_MODEL, config=LIVE_CONFIG) as session:
        print("Gemini Live connected:", GEMINI_MODEL)
        await session.send_realtime_input(text=GREETING_TRIGGER)

        async def call_to_gemini():
            async for text in call_ws.iter_text():
                msg = json.loads(text)
                if msg.get("type") == "audio":
                    # caller audio: base64 PCM16 16 kHz
                    await session.send_realtime_input(
                        audio=types.Blob(
                            data=base64.b64decode(msg["data"]["audio_b64"]),
                            mime_type="audio/pcm;rate=16000",
                        )
                    )
                else:
                    print("FreJun event:", msg.get("type"))

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

            # session.receive() ends after each model turn; loop to keep the conversation going
            while True:
                async for response in session.receive():
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
                            print("user:", heard.strip())
                        if said.strip():
                            print("assistant:", said.strip())
                        heard, said = "", ""

        tasks = [
            asyncio.create_task(call_to_gemini()),
            asyncio.create_task(gemini_to_call()),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if t.exception():
                    print("Stream error:", repr(t.exception()))
        finally:
            for t in tasks:
                t.cancel()
            print("Session closed")
