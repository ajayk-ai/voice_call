"""HTTP-level checks that never reach FreJun or Gemini."""

import pytest
from fastapi.testclient import TestClient

from backend.voice_agent import security
from backend.voice_agent.app import app

client = TestClient(app)


@pytest.fixture
def password(monkeypatch):
    monkeypatch.setattr(security, "APP_PASSWORD", "secret")
    return "secret"


def test_health_and_page():
    assert client.get("/health").status_code == 200
    page = client.get("/")
    assert page.status_code == 200 and "Normal call" in page.text


def test_call_rejects_bad_json():
    r = client.post("/api/call", content="xx", headers={"Content-Type": "application/json"})
    assert r.status_code == 400


def test_call_rejects_wrong_code(password):
    r = client.post("/api/call", json={"to": "+919999999999", "password": "nope"})
    assert r.status_code == 401


def test_call_rejects_bad_number(password):
    r = client.post("/api/call", json={"to": "12345", "password": password})
    assert r.status_code == 400


def test_flow_returns_stream_action():
    body = client.post("/flow", json={}).json()
    assert body["action"] == "stream" and body["ws_url"].endswith("/media")


def test_browser_ws_rejects_wrong_code(password):
    with client.websocket_connect("/test-ws") as ws:
        ws.send_json({"type": "auth", "code": "nope"})
        assert ws.receive_json() == {"type": "error", "text": "Wrong access code."}


def test_ultramsg_webhook_secret_and_filtering(monkeypatch):
    monkeypatch.setenv("ULTRAMSG_WEBHOOK_SECRET", "hook")
    group_msg = {"event_type": "message_received", "data": {"from": "123@g.us", "body": "hi"}}
    assert client.post("/webhooks/ultramsg?secret=bad", json=group_msg).status_code == 403
    r = client.post("/webhooks/ultramsg?secret=hook", json=group_msg)
    assert r.status_code == 200 and r.json() == {"ok": True, "stored": False}


def test_flow_passes_call_script_token_to_media():
    body = client.post("/flow?ctx=abc123", json={}).json()
    assert body["ws_url"].endswith("/media?ctx=abc123")


def test_call_script_token_round_trip_and_tamper_proof():
    from backend.voice_agent import call_context

    token = call_context.encode("ACME (400200)", "ACME", "400200", [
        {"po": "4500000001", "item": "10", "desc": "Gear", "due": "01-Oct-2026", "pending": "5.000", "uom": "EA", "od": 8}])
    script = call_context.decode(token)
    assert script["vendor"] == "ACME (400200)" and "ACME" in script["greeting"]
    assert "VENDOR: ACME (vendor code 4 0 0 2 0 0)" in script["prompt"]
    assert "PO 4 5 0 0 0 0 0 0 0 1, line 10 - Gear - due 01-Oct-2026 - 5 EA pending - 8 days late" in script["prompt"]
    body, sig = token.rsplit(".", 1)
    forged = call_context.encode("X", "X", "1", []).rsplit(".", 1)[0] + "." + sig
    assert call_context.decode(forged) is None
    assert call_context.decode("garbage") is None and call_context.decode(None) is None


@pytest.fixture
def teler_key(monkeypatch):
    from backend.voice_agent import call_context

    monkeypatch.setattr(call_context, "TELER_API_KEY", "teler-test-key")
    return call_context


def test_call_ticket_signed_fresh_and_tamper_proof(teler_key, monkeypatch):
    t = teler_key.ticket()
    assert teler_key.ticket_valid(t)
    issued, sig = t.split(".")
    assert not teler_key.ticket_valid(f"{int(issued) + 1}.{sig}")
    assert not teler_key.ticket_valid(None) and not teler_key.ticket_valid("") and not teler_key.ticket_valid("x.y")
    # a vendor script token is signed with the same key but is not a ticket
    assert not teler_key.ticket_valid(teler_key.encode("A", "A", "1", []))
    monkeypatch.setattr(teler_key.time, "time", lambda: int(issued) + teler_key.MAX_AGE_SECONDS + 1)
    assert not teler_key.ticket_valid(t)


def test_no_tickets_without_teler_key(monkeypatch):
    from backend.voice_agent import call_context

    monkeypatch.setattr(call_context, "TELER_API_KEY", None)
    assert not call_context.ticket_valid(call_context.ticket())


def test_media_refuses_streams_without_a_call_ticket(teler_key, monkeypatch):
    from starlette.websockets import WebSocketDisconnect

    from backend.voice_agent.routes import media

    monkeypatch.setattr(media, "gemini_client", lambda: pytest.fail("Gemini must not be reached"))
    for url in ("/media", "/media?ticket=123.forged"):
        with pytest.raises(WebSocketDisconnect) as e:
            with client.websocket_connect(url):
                pass
        assert e.value.code == media.WS_CLOSE_POLICY_VIOLATION


def test_flow_passes_call_ticket_to_media():
    body = client.post("/flow?ticket=1.abc&ctx=xyz", json={}).json()
    assert body["ws_url"].endswith("/media?ticket=1.abc&ctx=xyz")


def test_health_says_it_understands_call_scripts():
    from backend.voice_agent import call_context

    assert client.get("/health").headers["x-call-script"] == call_context.VERSION


def test_phone_audio_goes_out_in_whole_20ms_packets(teler_key, monkeypatch):
    """A partial packet at the end of a message is padded with silence by the phone side: audible holes."""
    import base64
    import json
    from contextlib import asynccontextmanager
    from types import SimpleNamespace as NS

    from backend.voice_agent.routes import media

    def sc(**k):
        base = dict(interrupted=False, generation_complete=False, turn_complete=False,
                    input_transcription=None, output_transcription=None)
        return NS(**{**base, **k})

    # odd chunk sizes (24 kHz PCM16 from Gemini), then the end of the reply
    replies = [NS(data=b"\x10\x00" * n, server_content=None, usage_metadata=None) for n in (1001, 517, 4099, 2333)]
    replies.append(NS(data=None, server_content=sc(generation_complete=True, turn_complete=True), usage_metadata=None))

    class Session:
        def __init__(self):
            self.passes = 0

        async def send_realtime_input(self, **k):
            pass

        async def receive(self):
            self.passes += 1
            if self.passes == 1:
                for r in replies:
                    yield r

    @asynccontextmanager
    async def connect(**k):
        yield Session()

    monkeypatch.setattr(media, "gemini_client", lambda: NS(aio=NS(live=NS(connect=connect))))
    sizes = []
    with client.websocket_connect(f"/media?ticket={teler_key.ticket()}") as ws:
        try:
            while True:
                msg = json.loads(ws.receive_text())
                if msg["type"] == "audio":
                    sizes.append(len(base64.b64decode(msg["audio_b64"])))
        except Exception:
            pass
    assert sizes and all(s % media.FRAME_BYTES == 0 for s in sizes), sizes
    total_in = sum((1001, 517, 4099, 2333)) / 3 * 2  # 24 kHz -> 8 kHz bytes
    assert abs(sum(sizes) - total_in) < media.FRAME_BYTES + 10  # nothing lost, at most one packet of padding
