# Twilio + Deepgram Voice Agent (Python) - End-to-End VS Code Setup

Phone call -> Twilio Media Stream -> FastAPI server (`main.py`) -> Deepgram Voice Agent (STT + LLM + TTS) -> back to the caller.

## 1. Prerequisites
- Python 3.10+ (`python --version`)
- VS Code with the **Python** extension
- A GitHub account and a free Render account (https://render.com)
- Deepgram API key (Console -> API Keys)
- Twilio Account SID, Auth Token and a phone number (Console)
- Trial Twilio: add your own mobile under Phone Numbers -> Verified Caller IDs

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
Twilio needs a public URL, so the server runs on Render (free plan).
1. Push this repo to GitHub (`.env` is gitignored - never commit it).
2. Render dashboard -> New -> Blueprint -> pick this repo. `render.yaml` sets the build/start commands.
3. Enter `DEEPGRAM_API_KEY` when asked, then Apply.
4. Copy the `https://<name>.onrender.com` URL into `PUBLIC_URL` in your local `.env` (no trailing slash).
5. Free services sleep when idle - open the URL in a browser to wake it before each call.

## 5. Place the test call (Terminal 3, venv activated)
```
python make_call.py
```
Your phone rings. Answer (on trial, press a key if Twilio asks) and talk.
The transcript prints in Terminal 1.

## 6. Test inbound (optional)
Twilio Console -> Phone Numbers -> your number -> Voice Configuration:
"A call comes in" = Webhook, `https://<name>.onrender.com/incoming-call`, HTTP POST. Then dial your Twilio number.

## Troubleshooting
- No ring / error 21219: your number isn't a Verified Caller ID (trial account).
- Call connects then drops: check `PUBLIC_URL` matches your Render URL and the service is awake (open it in a browser first).
- Silence from the agent: check `DEEPGRAM_API_KEY` and look for "Deepgram error" in Terminal 1.
- `additional_headers` error: run `pip install -U "websockets>=14"`.
- Agent logs/transcripts on Render: open the service -> Logs.

## Customize
Edit `AGENT_SETTINGS` in `main.py` to change the prompt, greeting, voice (`speak.provider.model`) or LLM (`think.provider`).