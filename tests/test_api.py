"""HTTP-level checks that never reach FreJun or Gemini."""

import pytest
from fastapi.testclient import TestClient

from voice_agent import security
from voice_agent.app import app

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
    from voice_agent import call_context

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


def test_health_says_it_understands_call_scripts():
    from voice_agent import call_context

    assert client.get("/health").headers["x-call-script"] == call_context.VERSION
