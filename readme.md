# FreJun + Gemini Live Voice Agent (Python) - End-to-End VS Code Setup

Phone call -> FreJun (Teler) Media Stream -> FastAPI server (`voice_agent/`) -> Gemini Live (native audio: listens, thinks, speaks in English / Hindi / Tamil) -> back to the caller.

## Project layout
```
main.py                  # entry point for `uvicorn main:app`; imports voice_agent.app
voice_agent/             # the web service: voice agent + PO follow-up admin, one FastAPI app
  app.py                 #   FastAPI app: wires routers (incl. po_followup admin), error handlers, lifespan
  config.py              #   settings from .env / Render env vars, logging
  prompts.py             #   what the agent says (system prompt, greeting)
  gemini.py              #   Gemini Live client and session config
  audio.py               #   24 kHz -> 8 kHz downsampler for the phone line
  security.py            #   access code, rate limit, number masking
  bridge.py              #   shared websocket bridge helpers, public URL
  routes/
    pages.py             #   GET /, /test, /health, /api/status
    calls.py             #   POST /api/call (place call), /flow, /call-status (FreJun webhooks)
    media.py             #   WS /media  (FreJun phone audio <-> Gemini)
    browser.py           #   WS /test-ws (browser mic <-> Gemini)
  static/index.html      #   the web page
po_followup/             # daily WhatsApp escalation of overdue POs (port of the n8n workflow)
  escalation.py          #   the rules: pending lines, levels, cleared lines, Phone_Master, dedupe
  messages.py            #   WhatsApp texts, recipients, Call/Meeting task rows, Alert_Log rows
  run.py                 #   one daily run, end to end
  sheets.py / whatsapp.py#   Google Sheets (delegation) and UltraMsg clients
  replies.py             #   POST /webhooks/ultramsg: stores vendor replies (served by the web app)
  store.py               #   PostgreSQL storage, one database per environment (prod / test)
  service.py             #   runs, run history, settings, automatic scheduler
  admin.py + static/     #   admin page GET /admin + API /admin/api/* (router mounted in voice_agent/app.py)
  cli.py                 #   python -m po_followup admin | run | scheduler | copy-prod-to-test | ...
monitoring/tracing.py    # LangSmith tracing shared by voice_agent and po_followup
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

## PO follow-up workflow (WhatsApp escalation)
`po_followup/` is the Python version of the n8n workflow "scm po follow up agent". Same sheet, same tabs, same rules:
a Master row is pending when **OD Days > 0**; each line escalates one level per day, tracked in `Line_Tracker`:

| Level | Who gets WhatsApp | Also |
|---|---|---|
| Day 0 | vendor planner + coordinator (Phone_Master Scenario 1, else Master) | |
| Day 1 | vendor management (Scenario 2) | |
| Day 2 | buyer | row in `Call_Tasks` (buyer calls Scenario 3 number) |
| Day 3 | buyer + SCM HOD | row in `Meeting_Tasks` |

A line that leaves Master (or OD Days <= 0) is marked CLEARED. One message per vendor per level per day;
`Alert_Log` Dedupe Keys stop repeats even if the run is started twice.

### Admin page, production and test
The admin page is part of the main web app, next to the call and voice agent pages (shared top menu):

| URL | What |
|---|---|
| `/` | Normal call / Voice agent tabs |
| `/admin` | PO follow-up admin |
| `/docs` | API reference (Swagger UI), `/admin/api/*` included |

```
.venv/Scripts/python -m po_followup admin      # whole app on http://127.0.0.1:8100 (this PC only) + scheduler
```
Or `uvicorn main:app --port 5050`: the scheduler runs there too (set `FOLLOWUP_SCHEDULER=false` to turn it off).

Reliability:
- **One run at a time per environment, across processes.** A run holds a PostgreSQL advisory lock, so two servers
  (or the server and the CLI) can never run the same environment together and double-send WhatsApp.
- **Failed daily runs are retried** up to 3 times, 10 minutes apart. Set `FOLLOWUP_ALERT_MOBILE` to get a WhatsApp
  when an automatic run fails.
- **Alert_Log is written after each vendor**, so a crash mid-run keeps the record of what was already sent.
- **Runs left "running"** by a crashed process are marked failed within about a minute.
- **Timeouts** on PostgreSQL (10 s connect, 5 min per statement), Google Sheets (60 s) and UltraMsg (20 s), so a hung
  call cannot block every later run.
- The Overview's **Health** card (and a banner on every tab) shows a stalled scheduler, failing automatic runs and stuck runs.
- A vendor test that has not moved for 30 minutes (e.g. the server was off) is stopped instead of resumed.

Access: the admin API needs `ADMIN_PASSWORD` (falls back to `APP_PASSWORD`), sent by the page as `X-Admin-Code`.
With neither set, only this PC (127.0.0.1, no proxy) can open it. The admin needs PostgreSQL (`psycopg`);
without it, e.g. on Render, the calls and voice agent still work and the admin page says it is unavailable.
Two PostgreSQL databases, switched at the top of the page:

| | Production (`pending_po`) | Test (`pending_po_test`) |
|---|---|---|
| PO data | mirror of the Google Sheet (refreshed before each run, or "Sync Google Sheet now") | copy of production ("Copy production data into test"), edit freely in pgAdmin |
| Follow-up state | schema `followup`: line_tracker, alert_log, call_tasks, meeting_tasks, runs, settings | same tables, separate data ("Clear test tracker & logs") |

Per environment you control:
- **Escalation intervals**: wait after Day 0 -> Day 1, Day 1 -> Day 2, Day 2 -> Day 3 (minutes, hours or days).
  Production default 1 day each; test default 5 minutes, so a full escalation can be watched in minutes.
- **Manual trigger**: Preview (writes nothing) / Dry run (no WhatsApp; in test it still escalates the tracker and
  writes tasks) / Live (sends WhatsApp; pilot mode sends everything to the pilot number).
- **Automatic trigger**: on/off, dry or live, once a day at a time (IST) or every N minutes.
- **Sending**: pilot mode, pilot number and vendors, SCM HOD number, lines per message.
- Run history (click a run for every message), line tracker, alert log, call and meeting tasks.

Admin page sections: **Overview** (how the escalation works, run now, run history), **Vendor trace** (per vendor:
Scenario 1 -> 2 -> 3 -> meeting timeline, countdown to the next step, every message sent, numbers used, tasks, PO lines),
**Test a vendor**, **Settings**, **Data & logs**.

**Test a vendor** (test environment): pick a vendor with pending lines, enter your WhatsApp number and a wait
(e.g. 1 minute), press Start test. It resets that vendor's test history, turns on pilot mode for that vendor only
with your number, sends Scenario 1 at once, and the scheduler sends Scenario 2, Scenario 3 and the Day 3 meeting one
wait apart (about 4 minutes for 1-minute waits). Each WhatsApp starts with "PILOT TEST – real recipient would be: ...".
Automatic runs stop when the vendor reaches the meeting step. The server must keep running for the next steps.

Live production runs and automatic live sending in production must be confirmed by typing LIVE.
The scheduler runs while the admin page process is running (`python -m po_followup scheduler` runs it without the page).

The first time, `store.import_sheet_state()` copied the tracker rows n8n wrote in the Google Sheet into production,
so escalation continues where n8n left off.

Command line equivalents: `python -m po_followup run --env test --mode dry`, `copy-prod-to-test`,
`setup-tabs`, `build-phone-master`.

Safety: keep the n8n workflow off, or vendors get messages from both.

Vendor replies: the web app serves `POST /webhooks/ultramsg`. On Render set `GOOGLE_SERVICE_ACCOUNT_JSON`,
`GOOGLE_DELEGATED_USER`, `PO_SHEET_ID` and `ULTRAMSG_WEBHOOK_SECRET`, then set the UltraMsg instance webhook URL to
`https://<app>.onrender.com/webhooks/ultramsg?secret=<ULTRAMSG_WEBHOOK_SECRET>` with webhook_message_received on.

## Monitoring (LangSmith)
Set `LANGSMITH_TRACING=true` and `LANGSMITH_API_KEY` (smith.langchain.com -> Settings -> API Keys) in `.env`,
and on Render for live calls. Traces go to the `LANGSMITH_PROJECT` project (default `po-voice-agent`).
Tracing is off without a key, and a LangSmith outage never affects calls or runs.

| Trace | Children | Shows |
|---|---|---|
| `phone_call` / `browser_session` (tags `voice-agent`) | `turn N` (llm) | vendor said -> agent said, Gemini tokens, `first_audio_ms` (agent response latency), interruptions, call duration, errors |
| `place_call`, `call_status` | | FreJun call creation and status callbacks (hang-up reasons) |
| `po_followup_daily_run` (tag `po-followup`) | `vendor_alert` -> `whatsapp_send`, `sheet_write` | per vendor/level: lines, POs, recipient role, send status; run totals by status |

Phone numbers are masked (`98765*****`) in traces. Call transcripts and WhatsApp texts (vendor names, PO numbers,
quantities) are sent to LangSmith as-is.
