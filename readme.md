# FreJun + Gemini Live Voice Agent (Python) - End-to-End VS Code Setup

Phone call -> FreJun (Teler) Media Stream -> FastAPI server (`main.py`) -> Gemini Live (native audio: listens, thinks, speaks in English / Hindi / Tamil) -> back to the caller.

## 1. Prerequisites
- Python 3.10+ (`python --version`)
- VS Code with the **Python** extension
- A GitHub account and a free Render account (https://render.com)
- Gemini API key (Google AI Studio -> Get API key)
- FreJun Teler API key and a FreJun virtual phone number (FreJun dashboard)

## 2. Open and set up in VS Code
1. File -> Open Folder -> `voice-agent-python`
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

You can still place a call from your machine with `python make_call.py` (uses `MY_PHONE_NUMBER` and `PUBLIC_URL` from `.env`).

## Troubleshooting
- No ring: check `TELER_API_KEY`, and that `FREJUN_PHONE_NUMBER` / `MY_PHONE_NUMBER` are in +91... format.
- Call connects then drops ("Failed to fetch call flow" in FreJun call logs): the server was asleep or FreJun was given a wrong URL. On Render the server always uses its own `RENDER_EXTERNAL_URL`; do not set `PUBLIC_URL` there.
- "Wrong access code": the code must match `APP_PASSWORD` on Render.
- Silence from the agent: check `GEMINI_API_KEY` and look for "Stream error" in the logs.
- Agent logs/transcripts on Render: open the service -> Logs.

## Customize
Edit `PO_FOLLOWUP_PROMPT` and `GREETING_TRIGGER` in `main.py` to change what the agent says. Set `GEMINI_MODEL` or `GEMINI_VOICE` env vars to change the model or voice.