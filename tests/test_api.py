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
