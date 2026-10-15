"""Shared setup: env readers and Google credentials."""

import pytest

from common import env, google_auth

GOOGLE_VARS = ("GOOGLE_DELEGATED_USER", "GOOGLE_SERVICE_ACCOUNT_JSON", "GOOGLE_SERVICE_ACCOUNT_FILE")


def test_env_readers(monkeypatch):
    monkeypatch.setenv("X_FLAG", " Yes ")
    monkeypatch.setenv("X_NUM", "oops")
    assert env.env_bool("X_FLAG", False) is True
    assert env.env_bool("X_MISSING", True) is True
    assert env.env_int("X_NUM", 7) == 7


def test_google_credentials_need_user_and_key(monkeypatch):
    for v in GOOGLE_VARS:
        monkeypatch.delenv(v, raising=False)
    assert not google_auth.configured()
    with pytest.raises(google_auth.GoogleNotConfigured):
        google_auth.delegated_credentials(["scope"])
    monkeypatch.setenv("GOOGLE_DELEGATED_USER", "bot@example.com")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
    assert google_auth.configured()


def test_sheet_refresh_needs_sheet_id_not_a_key_file(monkeypatch):
    """Servers with only GOOGLE_SERVICE_ACCOUNT_JSON used to fail with KeyError: 'GOOGLE_SERVICE_ACCOUNT_FILE'."""
    pytest.importorskip("psycopg")
    from po_followup import service

    monkeypatch.delenv("PO_SHEET_ID", raising=False)
    with pytest.raises(RuntimeError, match="PO_SHEET_ID"):
        service.sync_sheet_to_prod()
