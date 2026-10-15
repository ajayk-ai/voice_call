"""Admin API checks that never reach PostgreSQL."""

import pytest
from fastapi.testclient import TestClient

from backend.po_followup.api import admin
from backend.voice_agent.app import app

client = TestClient(app)
CODE = {"X-Admin-Code": "adm"}


@pytest.fixture
def code(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "adm")


@pytest.fixture
def ready(monkeypatch):
    monkeypatch.setattr(admin, "_ready", True)


def test_admin_page_and_status():
    assert "PO Follow-up Admin" in client.get("/admin").text
    st = client.get("/api/status").json()
    assert set(st) == {"voice_agent", "phone_calls", "access_code_required", "admin"}


def test_wrong_or_missing_code(code, ready):
    assert client.get("/admin/api/session").status_code == 401
    r = client.get("/admin/api/session", headers={"X-Admin-Code": "nope"})
    assert r.status_code == 401 and r.json() == {"error": "Wrong admin code."}
    assert client.get("/admin/api/session", headers=CODE).status_code == 200


def test_no_code_configured_blocks_remote(monkeypatch, ready):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    assert client.get("/admin/api/session").status_code == 403  # TestClient is not 127.0.0.1


def test_database_down_is_503(code, monkeypatch):
    monkeypatch.setattr(admin, "_ready", False)
    monkeypatch.setattr(admin, "_init", lambda: (_ for _ in ()).throw(OSError("connection refused")))
    r = client.get("/admin/api/test/state", headers=CODE)
    assert r.status_code == 503 and "connection refused" in r.json()["error"]


def test_request_validation(code, ready):
    assert client.post("/admin/api/test/run", json={"mode": "send"}, headers=CODE).status_code == 422
    assert client.get("/admin/api/staging/state", headers=CODE).status_code == 422
    assert client.get("/admin/api/test/table/Nope", headers=CODE).status_code == 422
    r = client.put("/admin/api/test/settings", json={"nope": 1}, headers=CODE)
    assert r.status_code == 422 and "nope" in r.json()["error"]


def test_live_prod_run_needs_confirm(code, ready):
    r = client.post("/admin/api/prod/run", json={"mode": "live"}, headers=CODE)
    assert r.status_code == 400 and "LIVE" in r.json()["error"]


def test_invalid_setting_value_is_400(code, ready):
    r = client.put("/admin/api/test/settings", json={"daily_time": "25:00"}, headers=CODE)
    assert r.status_code == 400 and r.json() == {"error": "Daily time must be HH:MM"}


def test_direct_vendor_call(code, ready, monkeypatch):
    from backend.po_followup.integrations import agent_call
    from backend.po_followup.db import store

    class Master:
        def __init__(self, env):
            pass

        def read_records(self, title):
            return [{"PO No": "4500466172", "PO Item No": "10", "Del.Sch Line": "1", "Vendor Code": "802515",
                     "Vendor Name": "PARAGON ENGINEERING", "Material": "M1", "Mat. Desc": "PILLAR_SUPPORT_PLATE",
                     "Pending Qty": "78", "UOM": "NOS", "OD Days": "499"}]

    placed = []
    monkeypatch.setattr(store, "PgStore", Master)
    monkeypatch.setattr(agent_call.AgentCaller, "call", lambda self, mobile, script: placed.append(mobile) or "call-9")
    r = client.post("/admin/api/test/vendor-call", headers=CODE, json={"vendor_code": "802515", "mobile": "7200017957"})
    assert r.status_code == 200 and r.json()["call_id"] == "call-9" and r.json()["lines"] == 1
    assert placed == ["917200017957"]

    assert client.post("/admin/api/test/vendor-call", headers=CODE,
                       json={"vendor_code": "999999", "mobile": "7200017957"}).status_code == 400

    def refuse(self, mobile, script):
        raise agent_call.AgentCallNotConfigured("tunnel down")
    monkeypatch.setattr(agent_call.AgentCaller, "call", refuse)
    r = client.post("/admin/api/test/vendor-call", headers=CODE, json={"vendor_code": "802515", "mobile": "7200017957"})
    assert r.status_code == 400 and "tunnel down" in r.json()["error"]
