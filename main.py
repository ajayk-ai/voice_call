"""Entry point kept at the repo root so `uvicorn main:app` (the Render start command) keeps working.

The application lives in the voice_agent package; see voice_agent/app.py."""

from voice_agent.app import app  # noqa: F401
