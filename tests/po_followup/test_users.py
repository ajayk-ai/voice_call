"""SCM users and the admin: password rules, session tokens, what each may open (no database needed)."""

import time

import pytest
from fastapi.testclient import TestClient

from backend.po_followup.api import admin
from backend.po_followup.services import users
from backend.voice_agent.app import app

client = TestClient(app)


def test_password_hash_roundtrip_and_rules(monkeypatch):
    h = users.hash_password("Secret123")
    assert users.verify_password("Secret123", h) and not users.verify_password("secret123", h)
    assert h != users.hash_password("Secret123")  # salted
    monkeypatch.setenv("PASSWORD_MIN_LENGTH", "10")
    with pytest.raises(ValueError, match="at least 10"):
        users.check_new_password("abc12345")
    with pytest.raises(ValueError, match="letters and numbers"):
        users.check_new_password("abcdefghijkl")
    users.check_new_password("abcdefghi1")


def test_usernames_are_normalised_and_checked():
    assert users.clean_username("  Ravi.Kumar ") == "ravi.kumar"
    for bad in ("ab", "has space", "-start", "x" * 41):
        with pytest.raises(ValueError):
            users.clean_username(bad)


def test_session_tokens_are_signed_and_expire(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "s1")
    t = users.make_token("ravi")
    assert users.read_token(t) == {"username": "ravi", "role": "scm"}
    body, sig = t.rsplit(".", 1)
    assert users.read_token(body + "." + "0" * len(sig)) is None
    monkeypatch.setenv("SESSION_SECRET", "s2")
    assert users.read_token(t) is None  # other secret
    monkeypatch.setenv("SESSION_HOURS", "1")
    real = time.time()
    token = users.make_token("a")
    monkeypatch.setattr(time, "time", lambda: real + 3500)
    assert users.read_token(token) is not None
    monkeypatch.setattr(time, "time", lambda: real + 3700)
    assert users.read_token(token) is None


def test_the_dashboard_api_needs_a_live_session(monkeypatch):
    monkeypatch.setattr(admin, "_ready", True)
    monkeypatch.setenv("SESSION_SECRET", "s1")
    monkeypatch.setattr(users, "current", lambda tok: users.read_token(tok))
    assert client.post("/dashboard/api/control/stop").status_code == 401
    assert client.post("/dashboard/api/control/stop", headers={"X-Session": "junk"}).status_code == 401


def test_login_by_role_and_admin_session(monkeypatch):
    monkeypatch.setattr(admin, "_ready", True)
    monkeypatch.setenv("APP_PASSWORD", "boss")
    monkeypatch.setenv("SESSION_SECRET", "s1")
    monkeypatch.setitem(users._admin_failures, "count", 0)
    monkeypatch.setitem(users._admin_failures, "locked_until", 0.0)
    login = "/dashboard/auth/login"
    assert client.post(login, json={"role": "admin", "username": "admin", "password": "no"}).status_code == 401
    assert client.post(login, json={"role": "admin", "username": "ravi", "password": "boss"}).status_code == 401
    r = client.post(login, json={"role": "admin", "username": "Admin", "password": "boss"})
    assert r.status_code == 200 and r.json()["role"] == "admin"
    h = {"X-Session": r.json()["token"]}
    monkeypatch.setattr(users, "list_users", lambda: [])
    assert client.get("/admin/api/users", headers=h).status_code == 200
    # an SCM user's session does not open the admin API
    assert client.get("/admin/api/users", headers={"X-Session": users.make_token("ravi")}).status_code == 401
