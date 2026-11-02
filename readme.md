# PO Agent

Follows up overdue purchase orders with vendors: WhatsApp, a voice agent call, then a management meeting. The PO data
comes from a Google Sheet; everything the app does is stored in PostgreSQL.

## How it works

Each run is one overdue check of the PO Master, for the vendors chosen in Vendor Control:

| Check in a row with OD Days > 0 | Scenario | What happens |
|---|---|---|
| 1st | Scenario 1 | WhatsApp to the vendor planner and coordinator |
| 2nd | Scenario 2 | WhatsApp to vendor management |
| 3rd | Scenario 3 | The voice agent calls the vendor. The call is transcribed, and the delay reason and committed dates are extracted. |
| 4th | Scenario 4 | Google Meet invite to the buyer, SCM head and the vendor's e-mail ids |

A line that is no longer overdue is cleared and starts again at Scenario 1 if it becomes overdue later.

The red **Emergency stop** button, on every tab, stops everything at once: automatic runs switch off, the run in progress ends before its next message, and ringing or connected calls are hung up. It stays on until someone clears it, and the follow-up then stays off until someone presses Start.

7 days (`FOLLOWUP_CYCLE_DAYS`) after a vendor reaches Scenario 4, all its open lines go back to Scenario 1 automatically. A vendor that has not reached Scenario 4 keeps climbing. Once a vendor has spoken on a Scenario 3 call, the voice agent does not call it again until it goes back to Scenario 1.

**Restart from Scenario 1** (Vendor Control) does this for every vendor at once and then runs the follow-up immediately. Earlier messages and calls stop counting but stay in the history.

## Using it

The app opens at `/`: choose **SCM User** or **Admin** and sign in.

**SCM User** (`/dashboard`)

| Tab | What it is for |
|---|---|
| 1 Vendor Master | Add vendors one at a time or upload a CSV (checked first; nothing is saved if a row is wrong). Numbers are 10-digit Indian mobiles. Scenario 4 holds the meeting e-mail ids. |
| 2 Vendor Control | Tick particular vendors or choose All vendors, pick Live or Trial, set the repeat (default 1 day) or a daily time, and Start. Also Stop, Run once now and recent runs. |
| 3 Trace | Only the chosen vendors: the scenario each is at and when it goes back to Scenario 1, purchasing groups, last call, failed deliveries, and the full timeline on click. Filter by purchasing group, buyer, days late, call result or delivery, and download the filtered list. |
| 4 Call Results | Scenario 3 calls: transcript, delay reasons, committed dates, CSV download. |

**Admin** (`/admin`), user id `ADMIN_USERNAME` and password `APP_PASSWORD`

- **System tests**: a live connection check, then a real WhatsApp message, voice call and Google Meet invite to you.
- **Users**: create SCM users, reset passwords, switch users off, activity log.

## Setup

Needs Python 3.14, PostgreSQL, a Google service account with domain-wide delegation (Sheets and
`https://www.googleapis.com/auth/calendar.events`), Gemini, FreJun (Teler) and UltraMsg accounts.

```bash
cp .env.example .env      # fill in the first block
uv sync --all-extras
uv run uvicorn main:app --port 8082
uv run python -m backend.po_sync          # the Google Sheet mirror, in a second terminal
uv run pytest -q
```

First time only, if the sheet has no tracking tabs: `python -m backend.po_followup setup-tabs`, and
`python -m backend.po_followup build-phone-master` if it has no Phone_Master tab.

FreJun must reach the app over https, so `PUBLIC_URL` is your domain or a tunnel:
`cloudflared tunnel --url http://localhost:8082`. A quick tunnel's address changes on every restart.

Deployment with Docker: see [DEPLOY.md](DEPLOY.md). Every setting is listed in [.env.example](.env.example).

## Project layout

```
main.py                       entry point (uvicorn main:app)
backend/core/                 .env loading, Google credentials, tracing, page files
backend/po_sync/              Google Sheet -> PostgreSQL mirror
backend/po_followup/
  api/                        admin, dashboard and vendor reply routes
  domain/                     escalation rules and messages (no I/O)
  integrations/               WhatsApp, voice call, Google Meet, Google Sheet
  services/                   run, scheduler, vendor master, trace, calls, users, system tests
  db/store.py                 PostgreSQL storage
backend/voice_agent/          FreJun call handling and the Gemini Live bridge
frontend/pages/               login, SCM user and admin pages
frontend/assets/              app.css, app.js and one script per page
```
