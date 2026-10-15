"""LangSmith tracing, shared by the voice agent and the PO follow-up workflow.

Off unless LANGSMITH_TRACING=true and LANGSMITH_API_KEY are set (LANGSMITH_PROJECT picks the project,
default "po-voice-agent"). Tracing must never break a call or a run: every LangSmith call here is
wrapped, and failures are only logged.

Two ways to trace:
- `traceable(...)` decorator for ordinary functions (nesting follows the call stack).
- `SessionTrace` for long streaming sessions (a phone call): one root run, with child runs added
  as conversation turns complete.
"""

import logging
import os
import re
import time
from datetime import datetime, timezone

from backend.core import env  # noqa: F401 - loads .env before LANGSMITH_* is read

log = logging.getLogger("monitoring.tracing")

os.environ.setdefault("LANGSMITH_PROJECT", "po-voice-agent")

try:
    from langsmith import traceable as _ls_traceable
    from langsmith.run_trees import RunTree, get_cached_client
except ImportError:  # tracing is optional; the app runs without the package
    _ls_traceable = None
    RunTree = None
    get_cached_client = None


def enabled() -> bool:
    return (
        _ls_traceable is not None
        and os.getenv("LANGSMITH_TRACING", "").lower() == "true"
        and bool(os.getenv("LANGSMITH_API_KEY"))
    )


# Indian mobile numbers (10 digits starting 6-9, optional +91/91/0). PO numbers start with 45, so they are kept.
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?91[\s-]?|0)?([6-9]\d{4})[\s-]?(\d{5})(?!\d)")


def mask_phones(text) -> str:
    """'+91 98765 43210' -> '98765*****'. Keeps phone numbers out of traces."""
    return _PHONE_RE.sub(lambda m: m.group(1) + "*****", str(text or ""))


def traceable(*args, **kwargs):
    """langsmith.traceable when tracing is available, otherwise a no-op decorator.
    The real decorator is cheap when tracing is switched off at runtime."""
    if _ls_traceable is None:
        if args and callable(args[0]) and len(args) == 1 and not kwargs:
            return args[0]
        return lambda fn: fn
    return _ls_traceable(*args, **kwargs)


def flush() -> None:
    """Send queued traces now (call before a CLI process exits or the server shuts down)."""
    if not enabled():
        return
    try:
        get_cached_client().flush()
    except Exception as e:
        log.warning("LangSmith flush failed: %s", e)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SessionTrace:
    """One root run for a streaming session, with a child run per conversation turn.

    Usage:
        trace = SessionTrace("phone_call", inputs={...}, metadata={...}, tags=[...])
        trace.turn(user_text, assistant_text, started_at=..., usage=..., metadata=...)
        trace.end(outputs={...}) or trace.end(error="...")
    """

    def __init__(self, name: str, inputs: dict | None = None, metadata: dict | None = None, tags: list[str] | None = None):
        self.root = None
        self.turns = 0
        self.started = time.monotonic()
        if not enabled():
            return
        try:
            self.root = RunTree(
                name=name,
                run_type="chain",
                inputs=inputs or {},
                extra={"metadata": metadata or {}},
                tags=tags or [],
            )
            self.root.post()
        except Exception as e:
            log.warning("LangSmith: could not start trace %s: %s", name, e)
            self.root = None

    @property
    def active(self) -> bool:
        return self.root is not None

    def add_metadata(self, metadata: dict) -> None:
        if self.root is None:
            return
        try:
            self.root.add_metadata(metadata)
        except Exception as e:
            log.debug("LangSmith add_metadata failed: %s", e)

    def turn(self, user: str, assistant: str, *, started_at: datetime | None = None,
             usage: dict | None = None, metadata: dict | None = None, error: str | None = None) -> None:
        """Record one completed conversation turn as an 'llm' child run."""
        self.turns += 1
        if self.root is None:
            return
        try:
            outputs = {"messages": [{"role": "assistant", "content": assistant}]}
            if usage:
                outputs["usage_metadata"] = usage
            child = self.root.create_child(
                name=f"turn {self.turns}",
                run_type="llm",
                inputs={"messages": [{"role": "user", "content": user}]},
                start_time=started_at,
                extra={"metadata": metadata or {}},
            )
            child.end(outputs=outputs, error=error)
            child.post()
        except Exception as e:
            log.debug("LangSmith turn failed: %s", e)

    def event(self, name: str, inputs: dict | None = None, outputs: dict | None = None, error: str | None = None) -> None:
        """Record a point-in-time event (e.g. barge-in, call status) as a short 'tool' child run."""
        if self.root is None:
            return
        try:
            child = self.root.create_child(name=name, run_type="tool", inputs=inputs or {})
            child.end(outputs=outputs or {}, error=error)
            child.post()
        except Exception as e:
            log.debug("LangSmith event failed: %s", e)

    def end(self, outputs: dict | None = None, error: str | None = None) -> None:
        if self.root is None:
            return
        try:
            outputs = {"turns": self.turns, "duration_s": round(time.monotonic() - self.started, 1), **(outputs or {})}
            self.root.end(outputs=outputs, error=error)
            self.root.patch()
        except Exception as e:
            log.warning("LangSmith: could not finish trace: %s", e)
        finally:
            self.root = None


def usage_from_gemini(usage_metadata) -> dict | None:
    """Gemini usage_metadata -> LangSmith usage_metadata (input/output/total tokens)."""
    if usage_metadata is None:
        return None
    prompt = getattr(usage_metadata, "prompt_token_count", None) or 0
    response = getattr(usage_metadata, "response_token_count", None) or 0
    total = getattr(usage_metadata, "total_token_count", None) or (prompt + response)
    if not total:
        return None
    return {"input_tokens": prompt, "output_tokens": response, "total_tokens": total}
