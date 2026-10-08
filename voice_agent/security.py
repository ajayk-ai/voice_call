"""Access code, client identification, rate limiting and log masking."""

import secrets
import time
from collections import defaultdict, deque

from fastapi import Request

from .config import APP_PASSWORD, CALL_RATE_LIMIT, CALL_RATE_WINDOW


def check_password(given) -> bool:
    """Constant-time compare, so the access code can't be guessed from response timing."""
    if not APP_PASSWORD:
        return True
    return isinstance(given, str) and secrets.compare_digest(given.encode(), APP_PASSWORD.encode())


def client_ip(request: Request) -> str:
    # Render sits behind a proxy; the first X-Forwarded-For entry is the real client
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def mask_number(number: str) -> str:
    """Keep full phone numbers out of the logs."""
    return number[:4] + "*" * max(len(number) - 7, 0) + number[-3:]


class RateLimiter:
    """Sliding-window limit per key. In memory: fine for a single Render instance."""

    def __init__(self, limit: int, window_seconds: int):
        self.limit = limit
        self.window = window_seconds
        self.hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        hits = self.hits[key]
        while hits and now - hits[0] > self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True


call_limiter = RateLimiter(CALL_RATE_LIMIT, CALL_RATE_WINDOW)
