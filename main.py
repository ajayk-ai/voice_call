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

AGENT_SETTINGS = {
    "type": "Settings",
    "audio": {
        "input": {"encoding": "mulaw", "sample_rate": 8000},
        "output": {"encoding": "mulaw", "sample_rate": 8000, "container": "none"},
    },
    "agent": {
        "language": "en",
        "listen": {"provider": {"type": "deepgram", "model": "nova-3"}},
        "think": {
            "provider": {"type": "open_ai", "model": "gpt-4o-mini"},
            "prompt": (
                "You are a friendly phone assistant. Keep replies short (1-2 sentences) "
                "because this is a voice call. Ask how you can help and answer simply."
            ),
        },
        "speak": {"provider": {"type": "deepgram", "model": "aura-2-thalia-en"}},
        "greeting": "Hello! This is a test call from your voice agent. How can I help you today?",
    },
}

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