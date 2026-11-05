"""Where the frontend lives. The backend serves these files; nothing here is built or bundled.

    frontend/pages/   one HTML page per screen (/, /admin, /dashboard)
    frontend/assets/  shared static files, served at /assets (app.css, app.js)
"""

from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
PAGES = FRONTEND / "pages"
ASSETS = FRONTEND / "assets"


def page(name: str) -> Path:
    return PAGES / name
