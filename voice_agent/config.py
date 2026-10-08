"""Settings from the environment (.env locally, Render environment variables in production)."""

import logging
import os
import sys

import truststore

# Use the OS certificate store, so HTTPS works behind corporate SSL inspection (e.g. Sophos)
truststore.inject_into_ssl()

from dotenv import load_dotenv

load_dotenv()

# Log lines go out immediately (Render logs pipe stdout, which is otherwise block-buffered)
sys.stdout.reconfigure(line_buffering=True)
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("voice_agent")


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        log.warning("Invalid %s=%r, using %d", name, os.getenv(name), default)
        return default


GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise RuntimeError("Missing GEMINI_API_KEY in .env")

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-live")
GEMINI_VOICE = os.getenv("GEMINI_VOICE", "Achird")

TELER_API_KEY = os.getenv("TELER_API_KEY")
FREJUN_PHONE_NUMBER = os.getenv("FREJUN_PHONE_NUMBER")
TELER_TIMEOUT_SECONDS = 20

# Optional but recommended: if set, the web page must send this code to place calls or use the browser agent
APP_PASSWORD = os.getenv("APP_PASSWORD")

# Hard stop for any single call or browser session, so a stuck call can't burn credits
MAX_SESSION_SECONDS = env_int("MAX_SESSION_SECONDS", 15 * 60)
# Phone calls each client IP may place per CALL_RATE_WINDOW seconds
CALL_RATE_LIMIT = env_int("CALL_RATE_LIMIT", 5)
CALL_RATE_WINDOW = env_int("CALL_RATE_WINDOW", 10 * 60)

if not (TELER_API_KEY and FREJUN_PHONE_NUMBER):
    log.warning("TELER_API_KEY / FREJUN_PHONE_NUMBER not set: phone calls are disabled")
if not APP_PASSWORD:
    log.warning("APP_PASSWORD not set: anyone with the URL can place calls and use the agent")
