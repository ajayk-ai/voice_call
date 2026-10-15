"""Scenario 3: the voice agent rings the vendor through FreJun (same call path as the web page's 'Normal call')."""

import asyncio
import os


class AgentCallNotConfigured(RuntimeError):
    pass


def server_url() -> str:
    """Base URL FreJun calls back on when the call is answered (/flow, /media); same order as bridge.public_url."""
    url = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("PUBLIC_URL")
    if not url:
        raise AgentCallNotConfigured("Set PUBLIC_URL (or run on Render) so FreJun can reach the voice agent")
    return url.rstrip("/")


def call_script(g: dict, max_lines: int = 15) -> str:
    """Signed call_context token with the vendor's name and its overdue lines (worst first)."""
    from backend.voice_agent import call_context

    lines = [{"po": l["poNumber"], "item": l["poItem"], "desc": l["desc"], "due": l["dueText"],
              "pending": l["pending"], "uom": l["uom"], "od": l["od"]}
             for l in sorted(g["lines"], key=lambda l: -l["od"])[:max_lines]]
    return call_context.encode(g["vendorLabel"], g["vendorName"], g["vendorCode"], lines)


# A sleeping Render free instance takes ~40 s to wake; FreJun would otherwise ring with nobody on the line
WAKE_TIMEOUT_SECONDS = 90


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
