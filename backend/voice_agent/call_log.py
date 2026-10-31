"""Hands a vendor call's transcript to the follow-up database (services.calls), which saves it and works out what
the vendor committed to. Best effort: a server without the database (or with it down) still runs the call, it just
doesn't record it, and nothing here may break or delay the call itself."""

import logging

from fastapi.concurrency import run_in_threadpool

log = logging.getLogger("voice_agent.call_log")


def _calls():
    from backend.po_followup.services import calls

    return calls


async def call_started(context: dict | None) -> None:
    if not (context and context.get("key")):
        return
    try:
        await run_in_threadpool(_calls().start_call, context["key"])
    except Exception:
        log.exception("Could not mark call %s as started", context["key"])


async def call_ended(context: dict | None, transcript: list[dict], error: str | None) -> None:
    """Save the transcript and analyse it (a few seconds of Gemini time; the phone line is already closed)."""
    if not (context and context.get("key")):
        return
    try:
        status = await run_in_threadpool(_calls().finish_call, context["key"], transcript, error)
        log.info("Call %s saved: %s, %d turn(s)", context["key"], status, len(transcript))
    except Exception:
        log.exception("Could not save the transcript of call %s", context["key"])


async def status_event(payload: dict) -> None:
    """FreJun's status callback: keeps it (and the recording link, when it carries one) on the call's row."""
    try:
        await run_in_threadpool(_calls().record_event, payload)
    except Exception:
        log.exception("Could not store the FreJun status event")
