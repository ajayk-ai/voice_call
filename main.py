"""Entry point kept at the repo root so `uvicorn main:app` (the Render start command) keeps working.

The application lives in backend/voice_agent/app.py; pages and styles are in frontend/."""

from backend.voice_agent.app import app  # noqa: F401
