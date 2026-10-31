import os

# Tests never send traces to LangSmith, even when .env enables tracing (load_dotenv doesn't override this)
os.environ["LANGSMITH_TRACING"] = "false"

import pytest


@pytest.fixture(autouse=True)
def no_call_rows(monkeypatch):
    """Placing an agent call saves a row in followup.call_results; tests must not write to a real database."""
    from backend.po_followup.services import calls

    for name in ("register_call", "attach_frejun_id", "fail_call"):
        monkeypatch.setattr(calls, name, lambda *a, **k: None)


@pytest.fixture(autouse=True)
def no_audit_rows(monkeypatch):
    """The audit log is a production table; tests must not write to it."""
    from backend.po_followup.services import users

    monkeypatch.setattr(users, "audit", lambda *a, **k: None)
