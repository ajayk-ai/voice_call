"""Settings from the environment (.env)."""

import logging
import os
import sys

from backend.core.env import env_int

# Log lines go out immediately (docker/journald logs pipe stdout, which is otherwise block-buffered)
sys.stdout.reconfigure(line_buffering=True)
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logging.getLogger("httpx").setLevel(logging.WARNING)  # its request lines would put the UltraMsg token in the logs
log = logging.getLogger("voice_agent")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

GEMINI_MODEL = os.getenv("GEMINI_MODEL")
GEMINI_VOICE = os.getenv("GEMINI_VOICE")
# Optional accent hint, e.g. en-IN. Leave unset so the agent can switch to Hindi or Tamil freely.
GEMINI_LANGUAGE = os.getenv("GEMINI_LANGUAGE") or None
# How long the caller must be silent before the agent answers. Lower = snappier replies,
# too low = the agent cuts in when someone pauses mid-sentence.
GEMINI_SILENCE_MS = env_int("GEMINI_SILENCE_MS", 300)

TELER_API_KEY = os.getenv("TELER_API_KEY")
FREJUN_PHONE_NUMBER = os.getenv("FREJUN_PHONE_NUMBER")
TELER_TIMEOUT_SECONDS = env_int("TELER_TIMEOUT_SECONDS", 20)

# Hard stop for any single call, so a stuck call can't burn credits
MAX_SESSION_SECONDS = env_int("MAX_SESSION_SECONDS", 15 * 60)

if not GEMINI_API_KEY:
    log.warning("GEMINI_API_KEY not set: the voice agent is disabled")
if not (GEMINI_MODEL and GEMINI_VOICE):
    log.warning("GEMINI_MODEL / GEMINI_VOICE not set: voice calls will fail")
if not os.getenv("CALL_ANALYSIS_MODEL"):
    log.warning("CALL_ANALYSIS_MODEL not set: call transcripts will not be analysed")
if not (TELER_API_KEY and FREJUN_PHONE_NUMBER):
    log.warning("TELER_API_KEY / FREJUN_PHONE_NUMBER not set: phone calls are disabled")
if not os.getenv("APP_PASSWORD"):
    log.warning("APP_PASSWORD not set: the admin cannot sign in")
if not os.getenv("ULTRAMSG_WEBHOOK_SECRET"):
    log.warning("ULTRAMSG_WEBHOOK_SECRET not set: anyone can post fake vendor replies to /webhooks/ultramsg")
