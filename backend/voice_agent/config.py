"""Settings from the environment (.env locally, Render environment variables in production)."""

import logging
import os
import sys

from backend.core.env import env_int

# Log lines go out immediately (Render logs pipe stdout, which is otherwise block-buffered)
sys.stdout.reconfigure(line_buffering=True)
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("voice_agent")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-live")
# Female voices: Aoede, Kore, Leda, Sulafat, Despina (male: Achird, Puck, Charon)
GEMINI_VOICE = os.getenv("GEMINI_VOICE", "Aoede")
# Optional accent hint, e.g. en-IN. Leave unset so the agent can switch to Hindi or Tamil freely.
GEMINI_LANGUAGE = os.getenv("GEMINI_LANGUAGE") or None
# How long the caller must be silent before the agent answers. Lower = snappier replies,
# too low = the agent cuts in when someone pauses mid-sentence.
GEMINI_SILENCE_MS = env_int("GEMINI_SILENCE_MS", 300)

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

if not GEMINI_API_KEY:
    log.warning("GEMINI_API_KEY not set: the voice agent (phone and browser) is disabled")
if not (TELER_API_KEY and FREJUN_PHONE_NUMBER):
    log.warning("TELER_API_KEY / FREJUN_PHONE_NUMBER not set: phone calls are disabled")
if not APP_PASSWORD:
    log.warning("APP_PASSWORD not set: anyone with the URL can place calls and use the agent")
if not os.getenv("ULTRAMSG_WEBHOOK_SECRET"):
    log.warning("ULTRAMSG_WEBHOOK_SECRET not set: anyone can post fake vendor replies to /webhooks/ultramsg")
