FROM python:3.14-slim

# Extra CA certificates: any *.crt in certs/ (e.g. an office SSL-inspection proxy). Empty on the server.
COPY certs/ /usr/local/share/ca-certificates/
RUN update-ca-certificates
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

# uv from PyPI (Docker Hub + PyPI only; ghcr.io is not needed)
RUN pip install --no-cache-dir uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_HTTP_TIMEOUT=300 \
    UV_HTTP_RETRIES=10 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Locked versions from uv.lock; the `sync` extra brings psycopg (admin/dashboard API and the sheet sync)
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --extra sync

# App layout in the image (paths are fixed in backend/core/frontend.py, relative to /app):
#   /app/main.py             FastAPI entry point (uvicorn main:app)
#   /app/backend/            API, voice agent, sheet sync
#   /app/frontend/pages/     HTML pages for /, /admin, /dashboard (static: no build step)
#   /app/frontend/assets/    served at /assets
COPY main.py ./
COPY backend ./backend
COPY frontend/pages ./frontend/pages
COPY frontend/assets ./frontend/assets

RUN useradd --system --no-create-home app
USER app

ENV PORT=8000
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://localhost:%s%s/health' % (os.environ['PORT'], os.environ.get('BASE_PATH', '').rstrip('/')))"

# Default start command (docker-compose.yml sets the same one explicitly for web)
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
