import asyncio
import base64
import json
import os

import websockets
from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import PlainTextResponse, Response

load_dotenv()

DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
if not DEEPGRAM_API_KEY:
    raise RuntimeError("Missing DEEPGRAM_API_KEY in .env")

DEEPGRAM_URL = "wss://agent.deepgram.com/v1/agent/converse"

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
- Start in English. You can speak English and Hindi.
- If the vendor speaks Hindi (or asks for Hindi), FIRST call switch_language with "hi", then continue in Hindi.
  If they switch back to English, call switch_language with "en".
- When speaking Hindi, write in Devanagari script. Keep PO numbers, dates and quantities as digits.
- Tamil is not supported yet. If the vendor speaks Tamil or asks for Tamil, politely apologise and ask
  if they can continue in English or Hindi.
"""

# Cartesia TTS (managed by Deepgram, no extra API key) - one voice, language switched mid-call.
CARTESIA_VOICE_ID = os.getenv("CARTESIA_VOICE_ID", "a167e0f3-df7e-4d52-a9c3-f949145efdab")
SUPPORTED_LANGUAGES = {"en": "English", "hi": "Hindi"}


def speak_settings(language: str) -> dict:
    return {
        "provider": {
            "type": "cartesia",
            "model_id": "sonic-2",
            "voice": {"mode": "id", "id": CARTESIA_VOICE_ID},
            "language": language,
        }
    }


SWITCH_LANGUAGE_FUNCTION = {
    "name": "switch_language",
    "description": "Switch the language of your voice. Call this before replying in a different language.",
    "parameters": {
        "type": "object",
        "properties": {
            "language": {
                "type": "string",
                "enum": list(SUPPORTED_LANGUAGES),
                "description": "en = English, hi = Hindi",
            }
        },
        "required": ["language"],
    },
}

AGENT_SETTINGS = {
    "type": "Settings",
    "audio": {
        "input": {"encoding": "mulaw", "sample_rate": 8000},
        "output": {"encoding": "mulaw", "sample_rate": 8000, "container": "none"},
    },
    "agent": {
        # nova-3 "multi" transcribes English and Hindi (including mixed Hinglish)
        "listen": {"provider": {"type": "deepgram", "model": "nova-3", "language": "multi"}},
        "think": {
            "provider": {"type": "open_ai", "model": "gpt-4o-mini"},
            "prompt": PO_FOLLOWUP_PROMPT,
            "functions": [SWITCH_LANGUAGE_FUNCTION],
        },
        "speak": speak_settings("en"),
        "greeting": (
            "Hello, this is the Bull Machines supply chain assistant, calling Super Springs "
            "Private Limited about some overdue purchase order dispatches. "
            "Am I speaking with someone from dispatch or planning?"
        ),
    },
}

async def handle_function_call(dg_ws, call: dict):
    """Run a client-side function requested by the agent and send the result back."""
    args = call.get("arguments") or {}
    if isinstance(args, str):
        args = json.loads(args or "{}")

    if call.get("name") == "switch_language":
        language = args.get("language")
        if language in SUPPORTED_LANGUAGES:
            await dg_ws.send(json.dumps({"type": "UpdateSpeak", "speak": speak_settings(language)}))
            print("Switched voice language to", language)
            content = f"Voice switched to {SUPPORTED_LANGUAGES[language]}. Continue in that language."
        else:
            content = "Unsupported language. Continue in English or Hindi."
    else:
        content = f"Unknown function {call.get('name')}"

    await dg_ws.send(
        json.dumps(
            {"type": "FunctionCallResponse", "id": call.get("id"), "name": call.get("name"), "content": content}
        )
    )


app = FastAPI()


@app.get("/")
async def root():
    return PlainTextResponse("Voice agent server is running")


@app.api_route("/incoming-call", methods=["GET", "POST"])
async def incoming_call(request: Request):
    """Twilio hits this when the call connects. Returns TwiML that starts a media stream."""
    host = request.headers.get("host")
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="wss://{host}/media" />
  </Connect>
</Response>"""
    return Response(content=twiml, media_type="text/xml")


@app.websocket("/media")
async def media(twilio_ws: WebSocket):
    await twilio_ws.accept()
    print("Twilio stream connected")
    state = {"stream_sid": None}

    async with websockets.connect(
        DEEPGRAM_URL, additional_headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"}
    ) as dg_ws:
        print("Deepgram agent connected")
        await dg_ws.send(json.dumps(AGENT_SETTINGS))

        async def twilio_to_deepgram():
            async for text in twilio_ws.iter_text():
                msg = json.loads(text)
                event = msg.get("event")
                if event == "start":
                    state["stream_sid"] = msg["start"]["streamSid"]
                    print("Call started:", state["stream_sid"])
                elif event == "media":
                    await dg_ws.send(base64.b64decode(msg["media"]["payload"]))
                elif event == "stop":
                    print("Call ended")
                    break

        async def deepgram_to_twilio():
            async for message in dg_ws:
                if isinstance(message, bytes):
                    # agent speech audio (mulaw 8k) -> Twilio
                    if state["stream_sid"]:
                        await twilio_ws.send_text(
                            json.dumps(
                                {
                                    "event": "media",
                                    "streamSid": state["stream_sid"],
                                    "media": {"payload": base64.b64encode(message).decode()},
                                }
                            )
                        )
                    continue

                msg = json.loads(message)
                mtype = msg.get("type")
                if mtype == "UserStartedSpeaking" and state["stream_sid"]:
                    # barge-in: clear audio already queued on Twilio
                    await twilio_ws.send_text(
                        json.dumps({"event": "clear", "streamSid": state["stream_sid"]})
                    )
                elif mtype == "ConversationText":
                    print(f"{msg.get('role')}: {msg.get('content')}")
                elif mtype == "FunctionCallRequest":
                    for call in msg.get("functions", []):
                        await handle_function_call(dg_ws, call)
                elif mtype == "Error":
                    print("Deepgram error:", msg)

        tasks = [
            asyncio.create_task(twilio_to_deepgram()),
            asyncio.create_task(deepgram_to_twilio()),
        ]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            print("Session closed")