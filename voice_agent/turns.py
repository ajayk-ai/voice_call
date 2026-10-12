"""Per-turn bookkeeping for a Gemini Live session: transcripts, timing, token usage, LangSmith turn runs."""

import time
from datetime import datetime, timezone

from monitoring.tracing import SessionTrace, usage_from_gemini


class TurnRecorder:
    """Feed every Gemini response to `observe()`. On turn_complete it records the turn
    (user transcript -> agent transcript) in the session trace and returns it."""

    def __init__(self, trace: SessionTrace):
        self.trace = trace
        self.interruptions = 0
        self.last_reply_ms: int | None = None
        self._reset()

    def _reset(self):
        self.heard = ""
        self.said = ""
        self.usage = None
        self.started_at: datetime | None = None
        self._t0: float | None = None
        self.first_audio_ms: int | None = None
        self.heard_at: float | None = None  # when the caller's latest words were transcribed
        self.reply_ms: int | None = None  # caller's last words -> agent's first audio (what the caller feels as lag)
        self.interrupted = False

    def observe(self, response) -> tuple[str, str] | None:
        """Returns (user, assistant) text when a turn completes, else None."""
        if self._t0 is None:
            self._t0 = time.monotonic()
            self.started_at = datetime.now(timezone.utc)
        if response.data and self.first_audio_ms is None:
            self.first_audio_ms = round((time.monotonic() - self._t0) * 1000)
            if self.heard_at is not None:
                self.reply_ms = round((time.monotonic() - self.heard_at) * 1000)
        if getattr(response, "usage_metadata", None):
            self.usage = usage_from_gemini(response.usage_metadata)

        sc = response.server_content
        if not sc:
            return None
        if sc.input_transcription and sc.input_transcription.text:
            self.heard += sc.input_transcription.text
            if self.first_audio_ms is None:
                self.heard_at = time.monotonic()
        if sc.output_transcription and sc.output_transcription.text:
            self.said += sc.output_transcription.text
        if sc.interrupted:
            self.interrupted = True
            self.interruptions += 1
        if not sc.turn_complete:
            return None

        user, assistant = self.heard.strip(), self.said.strip()
        self.trace.turn(
            user,
            assistant,
            started_at=self.started_at,
            usage=self.usage,
            metadata={
                "first_audio_ms": self.first_audio_ms,
                "reply_ms": self.reply_ms,
                "turn_ms": round((time.monotonic() - self._t0) * 1000),
                "interrupted": self.interrupted,
            },
        )
        self.last_reply_ms = self.reply_ms
        self._reset()
        return user, assistant
