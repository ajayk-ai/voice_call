"""Scenario 3: the voice agent rings the vendor through FreJun."""

import asyncio
import os

from backend.core.env import env_int


class AgentCallNotConfigured(RuntimeError):
    pass


def server_url() -> str:
    """Base URL FreJun calls back on when the call is answered (/flow, /media); same as bridge.public_url."""
    url = os.getenv("PUBLIC_URL")
    if not url:
        raise AgentCallNotConfigured("Set PUBLIC_URL so FreJun can reach the voice agent")
    return url.rstrip("/")


def script_lines(g: dict, max_lines: int = 15) -> list[dict]:
    """The overdue lines the agent is given, worst first."""
    return [{"po": l["poNumber"], "item": l["poItem"], "desc": l["desc"], "due": l["dueText"],
             "pending": l["pending"], "uom": l["uom"], "od": l["od"]}
            for l in sorted(g["lines"], key=lambda l: -l["od"])[:max_lines]]


def call_script(g: dict, max_lines: int = 15, key: str = "") -> str:
    """Signed call_context token with the vendor's name and its overdue lines (worst first).
    `key`: the call's row, where its transcript and analysis are saved (services.calls)."""
    from backend.voice_agent import call_context

    return call_context.encode(g["vendorLabel"], g["vendorName"], g["vendorCode"], script_lines(g, max_lines), key)


# Time allowed for the server to answer the wake-up request before the call is placed
WAKE_TIMEOUT_SECONDS = env_int("CALL_WAKE_TIMEOUT_SECONDS", 90)


def check_stream_server(base: str) -> None:
    """Before ringing: make sure the server FreJun will stream to is awake and understands vendor call
    scripts (older code would play the fixed sample script instead)."""
    import httpx

    from backend.voice_agent import call_context

    try:
        r = httpx.get(f"{base}/health", timeout=WAKE_TIMEOUT_SECONDS)
    except httpx.HTTPError as e:
        raise AgentCallNotConfigured(f"Voice agent server {base} not reachable ({e!r}); call not placed") from e
    if r.status_code != 200:
        raise AgentCallNotConfigured(f"Voice agent server {base} answered HTTP {r.status_code}; call not placed")
    if r.headers.get("x-call-script") != call_context.VERSION:
        raise AgentCallNotConfigured(
            f"The voice agent server at PUBLIC_URL ({base}) runs older code without vendor call scripts, so the "
            "agent would read the fixed sample script. Deploy the latest code there. Call not placed.")


def hang_up(call_id: str) -> None:
    """End a ringing or connected call."""
    from teler import AsyncClient

    from backend.voice_agent.config import TELER_API_KEY, TELER_TIMEOUT_SECONDS

    async def end():
        async with AsyncClient(api_key=TELER_API_KEY, timeout=TELER_TIMEOUT_SECONDS) as client:
            await client.voice.mutations.hangup(call_id)

    asyncio.run(end())


class AgentCaller:
    def call(self, mobile: str, script: str) -> str:
        """Ring `mobile` (digits with country code, e.g. 917200081289) with `script` (call_script's token).
        Returns FreJun's call id."""
        from backend.voice_agent.config import FREJUN_PHONE_NUMBER, TELER_API_KEY
        from backend.voice_agent.routes.calls import create_call

        if not (TELER_API_KEY and FREJUN_PHONE_NUMBER):
            raise AgentCallNotConfigured("Set TELER_API_KEY and FREJUN_PHONE_NUMBER to place agent calls")
        base = server_url()
        check_stream_server(base)
        # Runs in the follow-up worker thread, which has no event loop of its own
        return asyncio.run(create_call(f"+{mobile}", base, script))["call_id"]
