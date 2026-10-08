# FreJun + Gemini Live Voice Agent (Python) - End-to-End VS Code Setup

Phone call -> FreJun (Teler) Media Stream -> FastAPI server (`voice_agent/`) -> Gemini Live (native audio: listens, thinks, speaks in English / Hindi / Tamil) -> back to the caller.

## Project layout
```
main.py                  # entry point for `uvicorn main:app` (Render start command); imports voice_agent.app
voice_agent/             # the voice agent web service
  app.py                 #   FastAPI app: wires routers, global error handler
  config.py              #   settings from .env / Render env vars, logging
  prompts.py             #   what the agent says (system prompt, greeting)
  gemini.py              #   Gemini Live client and session config
  audio.py               #   24 kHz -> 8 kHz downsampler for the phone line
  security.py            #   access code, rate limit, number masking
  bridge.py              #   shared websocket bridge helpers, public URL
  routes/
    pages.py             #   GET /, /test, /health
    calls.py             #   POST /api/call (place call), /flow, /call-status (FreJun webhooks)
    media.py             #   WS /media  (FreJun phone audio <-> Gemini)
    browser.py           #   WS /test-ws (browser mic <-> Gemini)
  static/index.html      #   the web page
po_sync/                 # local-only: live Google Sheet -> PostgreSQL mirror (python -m po_sync)
scripts/make_call.py     # place a test call from your machine
tests/                   # pytest: `.venv/Scripts/python -m pytest`
```

## 1. Prerequisites
- Python 3.10+ (`python --version`)
- VS Code with the **Python** extension
- A GitHub account and a free Render account (https://render.com)
- Gemini API key (Google AI Studio -> Get API key)
- FreJun Teler API key and a FreJun virtual phone number (FreJun dashboard)

## 2. Open and set up in VS Code
1. File -> Open Folder -> `voice_call`
2. Open a terminal (Ctrl+`) and create a virtual environment:
   ```
   python -m venv .venv
   ```
3. Activate it:
   - Windows PowerShell: `.venv\Scripts\Activate.ps1`
   - Windows cmd: `.venv\Scripts\activate.bat`
   - Mac/Linux: `source .venv/bin/activate`
4. Install packages:
   ```
   pip install -r requirements.txt
   ```
5. Press Ctrl+Shift+P -> "Python: Select Interpreter" -> choose the `.venv` one.
6. Create your env file:
   - Windows: `copy .env.example .env`
   - Mac/Linux: `cp .env.example .env`
   Then fill in all values (leave `PUBLIC_URL` for step 4).

## 3. Start the server (Terminal 1)
```
uvicorn main:app --port 5050 --reload
```
Open http://localhost:5050 - you should see "Voice agent server is running".

## 4. Deploy to Render
FreJun needs a public URL, so the server runs on Render (free plan).
1. Push this repo to GitHub (`.env` is gitignored - never commit it).
2. Render dashboard -> New -> Blueprint -> pick this repo. `render.yaml` sets the build/start commands.
3. Fill in the env vars when asked, then Apply:
   - `GEMINI_API_KEY`
   - `TELER_API_KEY`
   - `FREJUN_PHONE_NUMBER` (+91... format)
   - `APP_PASSWORD`: the access code the web page asks for, for both tabs. Set one. Otherwise anyone
     with the URL can make calls on your FreJun account and use your Gemini credits.
   - Optional: `MAX_SESSION_SECONDS` (default 900, hard stop per call), `CALL_RATE_LIMIT` / `CALL_RATE_WINDOW`
     (default 5 calls per 600 s per visitor), `LOG_LEVEL` (default `INFO`).
4. Free services sleep when idle. Open the URL first to wake the server.

## 5. Use the web page
Open `https://<name>.onrender.com`. It has two tabs:
- **Normal call**: enter a phone number (with country code, e.g. `+919876543210`) and the access code, then click
  **Call this number**. The phone rings from your FreJun number and the voice agent handles the call.
- **Voice agent**: click **Start test call**, allow the microphone and talk to the agent in the browser
  (no phone call). Use headphones so the agent doesn't hear itself.

Transcripts print in the Render logs (service -> Logs).

You can still place a call from your machine with `python scripts/make_call.py` (uses `MY_PHONE_NUMBER` and `PUBLIC_URL` from `.env`).

## Troubleshooting
- No ring: check `TELER_API_KEY`, and that `FREJUN_PHONE_NUMBER` / `MY_PHONE_NUMBER` are in +91... format.
- Call connects then drops ("Failed to fetch call flow" in FreJun call logs): the server was asleep or FreJun was given a wrong URL. On Render the server always uses its own `RENDER_EXTERNAL_URL`; do not set `PUBLIC_URL` there.
- "Wrong access code": the code must match `APP_PASSWORD` on Render.
- Silence from the agent: check `GEMINI_API_KEY` and look for "Stream error" in the logs.
- Agent logs/transcripts on Render: open the service -> Logs.

## Customize
Edit `PO_FOLLOWUP_PROMPT` and `GREETING_TRIGGER` in `voice_agent/prompts.py` to change what the agent says. Set `GEMINI_MODEL` or `GEMINI_VOICE` env vars to change the model or voice.
## PO sheet live sync (local)
`po_sync/` mirrors every tab of the "CE - Pending PO" Google Sheet into its own PostgreSQL database
(`pending_po`), separate from other projects. It authenticates with a GCP service account using
domain-wide delegation (impersonating `GOOGLE_DELEGATED_USER`).

1. Put the service account key at `service.json` in this folder (gitignored).
2. Fill the `GOOGLE_*` and `PO_*` values in `.env` (see `.env.example`).
3. Install: `uv pip install --python .venv/Scripts/python.exe -r po_sync/requirements.txt`
4. Run once to test: `.venv/Scripts/python -m po_sync --once`
5. Keep it live: `.venv/Scripts/python -m po_sync` (re-syncs every `PO_SYNC_INTERVAL_SECONDS`, default 60)

What you get:
- One table per tab in the `sheets` schema, e.g. `sheets.master`, `sheets.phone_master`, `sheets.vendor_replies`.
  Column names are cleaned-up headers (`Mat. Desc` -> `mat_desc`); the original header is the column comment.
  All values are TEXT, plus `sheet_row` (the row number in the sheet).
- `public.sync_state`: per tab, row count, last sync time and last error.
- Only changed tabs are rewritten, each in one transaction, so queries never see a half-written tab.
  Tabs deleted from the sheet are dropped from the mirror.
