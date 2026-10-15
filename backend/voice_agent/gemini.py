"""Gemini Live client and session config."""

import logging
from functools import cache

from google import genai
from google.genai import types

from backend.voice_agent.config import GEMINI_API_KEY, GEMINI_LANGUAGE, GEMINI_SILENCE_MS, GEMINI_VOICE
from backend.voice_agent.prompts import PO_FOLLOWUP_PROMPT

log = logging.getLogger("voice_agent.gemini")

def live_config(system_instruction: str) -> types.LiveConnectConfig:
    """Session config for one call; the prompt differs per vendor."""
    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=system_instruction,
        speech_config=types.SpeechConfig(
            language_code=GEMINI_LANGUAGE,
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=GEMINI_VOICE)
            )
        ),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(
                start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_LOW,
                end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_HIGH,
                prefix_padding_ms=150,
                silence_duration_ms=GEMINI_SILENCE_MS,
            )
        ),
    )


LIVE_CONFIG = live_config(PO_FOLLOWUP_PROMPT)


@cache
def gemini_client() -> genai.Client:
    """Created on first use, so the server (and the admin page) starts without GEMINI_API_KEY."""
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not set")
    return genai.Client(api_key=GEMINI_API_KEY)


def pcm16k(data: bytes) -> types.Blob:
    """Wrap 16 kHz PCM16 audio for send_realtime_input."""
    return types.Blob(data=data, mime_type="audio/pcm;rate=16000")


async def receive_turns(session):
    """Yield every Gemini Live message for the whole session.

    session.receive() ends after each model turn, so it is called again in a loop.
    If a pass yields nothing, the connection has closed: stop instead of spinning."""
    while True:
        got_any = False
        async for response in session.receive():
            got_any = True
            yield response
        if not got_any:
            log.info("Gemini session ended")
            return
