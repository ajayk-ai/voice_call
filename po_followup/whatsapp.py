"""UltraMsg WhatsApp API ('08 UltraMsg – Send WhatsApp' in n8n)."""

import re

import httpx

TIMEOUT_SECONDS = 20


def mask_token(text: str) -> str:
    return re.sub(r"token=[^&\s\"]+", "token=***", text, flags=re.I)


class UltraMsg:
    def __init__(self, base_url: str, token: str):
        if not (base_url and token):
            raise ValueError("Set ULTRAMSG_BASE_URL and ULTRAMSG_TOKEN")
        self.base_url = base_url
        self.token = token

    def send_text(self, mobile_digits: str, body: str) -> dict:
        """Send one WhatsApp text. Returns UltraMsg's JSON (sent == 'true' on success).
        Raises httpx.HTTPError on network or HTTP errors."""
        with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
            res = client.post(
                f"{self.base_url}/messages/chat",
                params={"token": self.token},
                data={"to": "+" + mobile_digits, "body": body},
            )
            res.raise_for_status()
            return res.json()
