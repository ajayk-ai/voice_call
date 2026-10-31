"""The admin's WhatsApp, call and meeting checks, with the outside services replaced."""

import pytest

from backend.po_followup.services import system_tests


def test_a_bad_number_or_email_is_refused_before_anything_is_sent():
    for fn in (system_tests.whatsapp, system_tests.call):
        with pytest.raises(ValueError, match="10-digit"):
            fn("12345")
    with pytest.raises(ValueError, match="one e-mail"):
        system_tests.meeting("a@b.com, c@d.com")


def test_whatsapp_reports_what_ultramsg_said(monkeypatch):
    monkeypatch.setenv("ULTRAMSG_BASE_URL", "http://x")
    monkeypatch.setenv("ULTRAMSG_TOKEN", "t")
    sent = []
    monkeypatch.setattr(system_tests.UltraMsg, "send_text", lambda self, to, body: sent.append(to) or {"sent": "true"})
    assert system_tests.whatsapp("9876543210")["ok"] and sent == ["919876543210"]
    monkeypatch.setattr(system_tests.UltraMsg, "send_text", lambda self, to, body: {"sent": "false", "error": "instance offline"})
    with pytest.raises(ValueError, match="instance offline"):
        system_tests.whatsapp("9876543210")


def test_call_shows_why_it_was_not_placed(monkeypatch):
    def boom(self, mobile, script):
        raise RuntimeError("tunnel down")
    monkeypatch.setattr(system_tests.AgentCaller, "call", boom)
    with pytest.raises(ValueError, match="tunnel down"):
        system_tests.call("9876543210")


def test_connection_problems_are_reported_not_raised(monkeypatch):
    def down(*a, **k):
        raise RuntimeError("no route token=abc")
    monkeypatch.setenv("ULTRAMSG_BASE_URL", "http://x")
    monkeypatch.setenv("ULTRAMSG_TOKEN", "t")
    monkeypatch.setattr(system_tests.httpx, "get", down)
    out = system_tests._safe(system_tests._check_whatsapp)
    assert not out["ok"] and "abc" not in out["detail"]
