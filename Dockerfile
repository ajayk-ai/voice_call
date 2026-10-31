FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY main.py ./
COPY backend ./backend
COPY frontend ./frontend

RUN useradd -r -u 10001 poagent && chown -R poagent /app
USER poagent

# One web process (no --workers): call audio WebSockets live in the process.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8082"]
