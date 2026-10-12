"""Per-call vendor data for calls placed with a script, e.g. the PO follow-up's Scenario 3 call.

The data travels inside the call itself: a signed, compressed token on the flow URL (?ctx=...), which /flow
passes on to /media. So the server that streams the audio does not have to be the one that placed the call
(a local follow-up run can use the Render server for the audio). The signature (keyed on TELER_API_KEY,
which both servers share) stops anyone from making the agent run a script of their own."""

import base64
import binascii
import hashlib
import hmac
import json
import time
import zlib

from .config import TELER_API_KEY
from .prompts import greeting, po_prompt

# /health advertises this, so a caller can check the streaming server understands these tokens
VERSION = "v1"
MAX_AGE_SECONDS = 60 * 60  # a call is answered within minutes
MAX_TOKEN_CHARS = 4000
MAX_LINES = 10  # keeps the flow URL short; the agent goes through the worst lines first
LINE_FIELDS = ("po", "item", "desc", "due", "pending", "uom", "od")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(body: str) -> str:
    key = hashlib.sha256(f"call-script|{TELER_API_KEY or ''}".encode()).digest()
    return _b64(hmac.new(key, body.encode(), hashlib.sha256).digest()[:16])


def encode(vendor: str, name: str, code: str, lines: list[dict]) -> str:
    """Token for one call. `lines`: dicts with LINE_FIELDS, worst first."""
    data = {"v": vendor, "n": name, "c": code, "t": int(time.time()),
            "l": [[str(l.get(f) or "") for f in LINE_FIELDS] for l in lines[:MAX_LINES]]}
    body = _b64(zlib.compress(json.dumps(data, separators=(",", ":")).encode(), 9))
    return f"{body}.{_sign(body)}"


def decode(token: str | None) -> dict | None:
    """{"vendor", "prompt", "greeting"} for a valid, fresh token, else None."""
    if not token or len(token) > MAX_TOKEN_CHARS or "." not in token:
        return None
    body, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(body)):
        return None
    try:
        data = json.loads(zlib.decompress(_unb64(body)))
    except (ValueError, zlib.error, binascii.Error):
        return None
    if time.time() - data.get("t", 0) > MAX_AGE_SECONDS:
        return None
    lines = [dict(zip(LINE_FIELDS, row)) for row in data["l"]]
    return {"vendor": data["v"], "prompt": po_prompt(data["n"], data["c"], lines), "greeting": greeting(data["n"])}
