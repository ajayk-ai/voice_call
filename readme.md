# PO Agent

Follows up overdue purchase-order lines automatically for the SCM team, from the "CE - Pending PO" Google Sheet:

- **Daily overdue check and escalation**: every overdue PO line (OD Days > 0) moves through Scenario 1 → 2 → 3 →
  management meeting, confirmed by how many daily checks in a row it has been overdue. WhatsApp messages
  (UltraMsg), a buyer call task with the **voice agent** (FreJun + Gemini Live, English / Hindi / Tamil), and a Google Meet.
- **Business dashboard** (`/dashboard`): overdue lines per scenario, and each vendor's Scenario 1/2/3 numbers (editable).
- **Admin page** (`/admin`): runs, schedule, settings, per-vendor trace, test environment.
- **Voice agent pages** (`/`): ring any number with the agent, or talk to it in the browser.

Contents: [How it works](#how-it-works) · [Development](#development) · [Ubuntu server (Docker)](#ubuntu-server-deployment-docker) ·
[Production](#production) · [Go-live checklist](#go-live-checklist) · [Configuration](#configuration) · [Using it](#using-it) ·
[Command line](#command-line) · [Project layout](#project-layout) · [Monitoring](#monitoring) ·
[Troubleshooting](#troubleshooting)

---

## How it works

Each run is one overdue check of the PO Master, for the vendors chosen in Vendor Control:

| Check in a row with OD Days > 0 | Scenario | What happens |
|---|---|---|
| PO data | the Google Sheet, synced | a copy of production you can edit freely (pgAdmin) |
| Check period | 1 day | 5 minutes, so a whole escalation can be watched quickly |
| Messages | real vendors (unless pilot mode) | pilot mode: everything to your own number |

---

## Development

### 1. Requirements
- **Python 3.14** (`.python-version`) and, recommended, [uv](https://docs.astral.sh/uv/)
- **PostgreSQL 15+** running locally (default `localhost:5432`, user `postgres`)
- For the parts you work on: Gemini API key, FreJun Teler API key + number, UltraMsg instance,
  Google service account with domain-wide delegation (`service.json`)
- VS Code with the Python extension (optional)

### 2. Install
```bash
git clone <repo> voice_call && cd voice_call

# with uv (same as CI): creates .venv with the web app, PostgreSQL driver and pytest
uv sync --all-extras

# or with pip
python -m venv .venv
.venv\Scripts\activate            # Windows   (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt "psycopg[binary]>=3.2" pytest
```
In VS Code: Ctrl+Shift+P → "Python: Select Interpreter" → `.venv`.

### 3. Configure
```bash
copy .env.example .env            # macOS/Linux: cp .env.example .env
```
Fill in at least `PO_DB_PASSWORD` and `GOOGLE_DELEGATED_USER`, and put the service account key at `service.json`
(both `.env` and `service.json` are gitignored). Everything else is optional for local work; see
[Configuration](#configuration). `.env` is read at startup; restart the server after changing it.

### 4. Load data
```bash
.venv\Scripts\python -m backend.po_sync --once                  # Google Sheet -> pending_po (creates the database)
.venv\Scripts\python -m backend.po_followup copy-prod-to-test   # production data -> pending_po_test
```
Tables in the `followup` schema are created automatically on first start.

### 5. Run
```bash
.venv\Scripts\uvicorn main:app --port 5050 --reload
```

| URL | What |
|---|---|
| http://localhost:5050/dashboard | business dashboard (`/dashboard?env=test` for test data) |
| http://localhost:5050/admin | admin page |
| http://localhost:5050/ | voice agent: phone call / browser test |
| http://localhost:5050/docs | API reference |

Locally, with no `ADMIN_PASSWORD` / `DASHBOARD_PASSWORD` set, only this PC can open the admin page and dashboard.

The frontend has **no build step and no npm**: `frontend/` is plain HTML/CSS served by this same server. Run
everything from the project root with the command above; after editing a page, just refresh the browser.

**Safe by default while developing.** Automatic runs are off until you turn them on in the admin page, and new
settings start dry (no WhatsApp). Work in the **Test** environment; pilot mode sends every message to your number.
To keep the scheduler out of a dev server entirely: `FOLLOWUP_SCHEDULER=false`.

### 6. Test phone calls locally
FreJun must reach your machine over HTTPS (call flow + `/media` WebSocket). Start a tunnel, e.g.
`cloudflared tunnel --url http://localhost:5050` or `ngrok http 5050`, put its URL in `PUBLIC_URL`, restart,
then use the **Normal call** tab or `python scripts/make_call.py` (rings `MY_PHONE_NUMBER`).
The browser **Voice agent** tab needs no tunnel, only `GEMINI_API_KEY` (use headphones).

### 7. Tests and lint
```bash
.venv\Scripts\python -m pytest -q        # never touches PostgreSQL, Google, WhatsApp or FreJun
uvx ruff check .
```
CI (`.github/workflows/ci.yml`) runs both on every push and pull request.

---

## Ubuntu server deployment (Docker)

One Ubuntu VM runs both projects. This project's containers sit behind the costing project's nginx on port 80:

```
ai.bullmachine.in/scm/ --> central nginx (costing project, :80) --> web as "voice-web" on network ai-proxy (127.0.0.1:8001)
```

This project has no nginx of its own; the costing project's nginx owns port 80. Run `docker network create ai-proxy` once before the first `docker compose up` of either project. Deploy it first if you have not already (costing README, "Deploy on an Ubuntu server (Docker)"). Local and architecture detail: [Option C](#option-c--docker-with-nginx-local-and-shared-server); troubleshooting: [DEPLOY.md](DEPLOY.md).

### 1. Prepare Ubuntu and install Docker

Ubuntu 22.04 or later with SSH access. The VM needs outbound internet access to Docker Hub (base images) and PyPI (Python packages). Install Docker Engine and the Compose plugin from Docker's apt repository:

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker $USER
```

Log out and back in (or run `newgrp docker`), then check:

```bash
docker --version
docker compose version
```

### 2. DNS and firewall

- An A record for `ai.bullmachine.in` pointing at the VM's public IP (shared with costing).
- Inbound **80** open in the cloud security group or NSG (443 too once HTTPS is added); 22 limited to known IPs.
- Keep 8001 and 5432 closed to the internet; only nginx is public.

On the VM, if using `ufw`:

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
```

### 3. Clone and configure

```bash
git clone <repository-url> ~/voice_call
cd ~/voice_call
cp .env.example .env
nano .env
```

Set at least:

```dotenv
APP_PASSWORD=<calls access code>
ADMIN_PASSWORD=<admin code>
DASHBOARD_PASSWORD=<dashboard code>
GEMINI_API_KEY=<google ai studio key>
TELER_API_KEY=<frejun key>
FREJUN_PHONE_NUMBER=+91...
PUBLIC_URL=https://ai.bullmachine.in/scm
GOOGLE_DELEGATED_USER=<workspace user>
PO_SHEET_ID=<spreadsheet id>
PO_DB_PASSWORD=<strong database password>
# BASE_PATH=/scm              # default; must equal SCM_PATH in costing
```

- Copy the Google service account key to the project root as `service.json` (`scp` from your PC); it is mounted read-only into the containers.
- `PUBLIC_URL` must include `/scm` and be HTTPS for FreJun calls. HTTPS is terminated on the costing nginx.
- `PO_DB_PASSWORD` is fixed when the `pgdata` volume is first created. Changing it later needs the volume recreated, which deletes the database.
- Do not set `PO_DB_HOST`, `PO_DB_USER` or `GOOGLE_SERVICE_ACCOUNT_FILE`; `docker-compose.yml` overrides them to the `db` container and the mounted `service.json`.
- Lock the secrets down: `chmod 600 .env service.json`.

### 4. Build and start

```bash
docker compose up -d --build
docker compose ps          # web (healthy), sync, db (healthy), nginx: Up
```

### 5. Load data

The `sync` container mirrors the Google Sheet automatically. On first start, optionally copy production data into the test database:

```bash
docker compose exec web python -m backend.po_followup copy-prod-to-test
```

Follow the sync: `docker compose logs -f sync`.

### 6. Verify

```bash
curl http://127.0.0.1:8001/scm/health                                   # the app
curl -H "Host: ai.bullmachine.in" http://localhost/scm/health          # through the central nginx on :80
```

Then open `http://ai.bullmachine.in/scm/admin`. `ADMIN_PASSWORD` and `DASHBOARD_PASSWORD` must be set, because every request through nginx counts as remote.

### 7. Update, logs, stop, backup

```bash
git pull && docker compose up -d --build
docker compose logs -f web          # also: sync, db, nginx
docker compose down                 # stop; database kept (down -v deletes it)
```

Back up PostgreSQL:

```bash
docker compose exec -T db pg_dump -U postgres -Fc pending_po > pending_po_$(date +%F).dump
```

FreJun calls need `PUBLIC_URL` on **HTTPS**, so add a TLS certificate to the costing project's nginx (port 443) before going live with Scenario 3 calls.

---

## Production

Every feature needs the web app; the admin page, dashboard and daily runs also need **PostgreSQL**, and phone calls
need a **public HTTPS URL**. Pick one of these setups.

### Option A – one server (recommended)

One Linux VM (2 vCPU / 4 GB is plenty) runs everything: PostgreSQL, the sheet sync and the web app behind HTTPS.

**1. Install**
```bash
sudo apt install -y postgresql caddy git           # plus Python 3.14 (deadsnakes PPA or uv)
sudo -u postgres psql -c "ALTER USER postgres PASSWORD '<strong password>';"
sudo useradd -r -m -d /opt/po-agent poagent
sudo -u poagent git clone <repo> /opt/po-agent/app
cd /opt/po-agent/app
sudo -u poagent python3.14 -m venv .venv
sudo -u poagent .venv/bin/pip install -r requirements.txt "psycopg[binary]>=3.2"
```

**2. Configure** `/opt/po-agent/app/.env` (owner `poagent`, `chmod 600`) and copy `service.json` next to it.
Production values: see the [checklist](#go-live-checklist). Set `PUBLIC_URL=https://po.example.com`.

**3. Services** – `/etc/systemd/system/po-agent.service`:
```ini
[Unit]
Description=PO Agent web app (dashboard, admin, scheduler, voice agent)
After=network-online.target postgresql.service

[Service]
User=poagent
WorkingDirectory=/opt/po-agent/app
ExecStart=/opt/po-agent/app/.venv/bin/uvicorn main:app --host 127.0.0.1 --port 5050 --proxy-headers --forwarded-allow-ips=127.0.0.1
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```
and `/etc/systemd/system/po-sync.service` (live sheet mirror; optional, since production runs also sync just before
they start):
```ini
[Unit]
Description=PO sheet -> PostgreSQL sync
After=network-online.target postgresql.service

[Service]
User=poagent
WorkingDirectory=/opt/po-agent/app
ExecStart=/opt/po-agent/app/.venv/bin/python -m backend.po_sync
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl daemon-reload && sudo systemctl enable --now po-agent po-sync
```
Run **one** web process (no `--workers`): call audio WebSockets and the call rate limiter live in the process.
(Several processes would not double-send – runs hold a PostgreSQL lock – but one is enough.)

**4. HTTPS** – `/etc/caddy/Caddyfile` (Caddy gets the certificate and proxies WebSockets by itself):
```
po.example.com {
    reverse_proxy 127.0.0.1:5050
}
```
`sudo systemctl reload caddy`. Behind the proxy every request counts as remote, so `ADMIN_PASSWORD` and
`DASHBOARD_PASSWORD` **must** be set.

**5. First start**
```bash
sudo -u poagent .venv/bin/python -m backend.po_sync --once
sudo -u poagent .venv/bin/python -m backend.po_followup copy-prod-to-test
curl https://po.example.com/health        # "Voice agent server is running"
```
Then open `/admin` → Production → Preview run, check the numbers, and follow the [go-live checklist](#go-live-checklist).

**Deploy a new version**
```bash
cd /opt/po-agent/app && sudo -u poagent git pull
sudo -u poagent .venv/bin/pip install -r requirements.txt "psycopg[binary]>=3.2"
sudo systemctl restart po-agent po-sync
```
New tables and columns are created on start; nothing to migrate by hand. A restart during a run marks that run
failed; the next automatic attempt retries it.

**Backups** – the follow-up state (what was sent, the daily checks) exists only in PostgreSQL:
```bash
# /etc/cron.d/po-agent-backup
30 2 * * * postgres pg_dump -Fc pending_po > /var/backups/pending_po_$(date +\%a).dump
```

**Logs**: `journalctl -u po-agent -f`.

On a Windows server instead of Linux: same commands with `.venv\Scripts\...`; keep both processes running with
Task Scheduler ("At startup", "Run whether user is logged on or not") or NSSM, and publish the app with a
Cloudflare Tunnel instead of Caddy.

### Option B – Render + office PC (current setup)

`render.yaml` deploys the web app to Render: phone calls, the browser voice agent and the vendor-reply webhook.
Render has no PostgreSQL in this setup (`psycopg` is not in `requirements.txt`), so there the admin page and
dashboard say *unavailable* and no daily runs happen. Those run on a PC on the office network with PostgreSQL:

| Where | What | How |
|---|---|---|
| Render | `/`, `/media`, FreJun webhooks, `/webhooks/ultramsg` | Blueprint from `render.yaml` |
| Office PC | `/admin`, `/dashboard`, scheduler, sheet sync | `python -m backend.po_followup admin` + `python -m backend.po_sync` |

**Render**
1. Push to GitHub (`.env` and `service.json` are gitignored – never commit them).
2. Render → New → Blueprint → this repo. Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`.
3. Set the env vars it asks for: `GEMINI_API_KEY`, `TELER_API_KEY`, `FREJUN_PHONE_NUMBER` (+91…), `APP_PASSWORD`,
   `ADMIN_PASSWORD`, `ULTRAMSG_WEBHOOK_SECRET`, `PO_SHEET_ID`, `GOOGLE_DELEGATED_USER`,
   `GOOGLE_SERVICE_ACCOUNT_JSON` (the whole key file's contents), optionally `LANGSMITH_*`.
   Do **not** set `PUBLIC_URL` there: Render's own `RENDER_EXTERNAL_URL` is used.
4. Free services sleep when idle: open the URL once to wake it before placing a call.

**Office PC** – install as in [Development](#development), production values in `.env`, `PUBLIC_URL` = the
Render URL (Scenario 3 calls are streamed by Render), then keep two processes running (Task Scheduler / NSSM):
```bash
.venv\Scripts\python -m backend.po_followup admin     # http://127.0.0.1:8100/admin + /dashboard + scheduler
.venv\Scripts\python -m backend.po_sync
```
The scheduler only runs while this PC is on. To give business users the dashboard, publish this PC (e.g.
Cloudflare Tunnel) or move to Option A.

### Option C – Docker with nginx (local and shared server)

This project and the costing project each have their own `docker-compose.yml` and are started separately. The only nginx is in the costing project. Both apps share one domain, each under its own **base path**: this one under `/scm/`, costing under `/costing/`. `/` redirects to the base path. The same files work on your PC and on the server:

```
:80 --> central nginx (costing project) --+-- DOMAIN/costing/ --> costing app
                                          +-- DOMAIN/scm/     --> web as "voice-web" (127.0.0.1:8001/scm/)
        shared external Docker network "ai-proxy" (create once: docker network create ai-proxy)
```

| | voice_call (this project) | costing |
|---|---|---|
| App port directly | http://127.0.0.1:8001/scm/ | http://127.0.0.1:8000/costing/ |
| Through nginx, local (default) | http://localhost/scm/ | http://localhost/costing/ |
| Through nginx, server | http://ai.bullmachine.in/scm/ | http://ai.bullmachine.in/costing/ |

Settings in `.env`:
- `BASE_PATH`: default `/scm`; leave it unset unless you want another path (then set `SCM_PATH` in costing's `.env` to match).
- The domain is set once, as `DOMAIN` in costing's `.env`.

`docker compose up` here starts:

| Service | What it runs |
|---|---|
| `web` | Built from the [Dockerfile](Dockerfile): Python deps installed with uv into `/opt/venv`, plus `main.py`, `backend/` and `frontend/` in `/app`. Started with `uvicorn main:app` on port 8000 under `BASE_PATH`. `frontend/` is plain HTML/CSS with no build step; the pages use relative URLs, so they work under any base path. |
| `sync` | Same image; runs `python -m backend.po_sync` (sheet → PostgreSQL). |
| `db` | PostgreSQL 17. |

`web` also joins the external network `ai-proxy` with the alias `voice-web`. Port 80 belongs to the costing project's nginx, which forwards `/scm/` (including the `/media` and `/test-ws` WebSockets) here, so **run costing as well** for public access (its README, "Docker (local and server)"). Without it, use http://127.0.0.1:8001/scm/.

Running outside Docker (`uvicorn main:app`, Render) with `BASE_PATH` unset still serves the app at `/`, as before.

**Run locally (Docker Desktop)**
1. Start Docker Desktop. `.env` and `service.json` are in the project root as for development. Leave `BASE_PATH` unset. Run `docker network create ai-proxy` once.
2. Behind the office SSL-inspection firewall (Fortinet), put the firewall's CA certificate in `certs/` as a `.crt` file (PEM), or builds fail with `CERTIFICATE_VERIFY_FAILED`. Export it from Windows (`certmgr.msc` → Trusted Root → the Fortinet `FG…` certificate → Export, Base-64). `certs/*.crt` is git-ignored. On the server, leave `certs/` empty.
3. `docker compose up -d --build`, then open http://127.0.0.1:8001/scm/, or http://localhost/scm/ (with costing running).
4. Known office-network limit: Python 3.14 rejects the Fortinet CA ("Basic Constraints of CA cert not marked critical"), so the sheet sync and Google/Gemini calls fail on the office network. The pages still load. This does not happen on the server or on a network without SSL inspection.

**Server deployment**: see [Ubuntu server deployment (Docker)](#ubuntu-server-deployment-docker) above for the full step-by-step setup (Docker install, DNS/firewall, `.env`, `service.json`, build, data, verify, updates and backups).

**Changing the domain or base path later**: no file edits are needed.
- **Domain:** set `DOMAIN` in costing's `.env` and `PUBLIC_URL` here. Then run `docker compose up -d nginx` in costing and `docker compose up -d` here, and update DNS.
- **Base path:** set `BASE_PATH` (and the path part of `PUBLIC_URL`) in `.env` here and `SCM_PATH` in costing's `.env`, then run `docker compose up -d` in both.

The UltraMsg webhook URL also includes the base path: `https://ai.bullmachine.in/scm/webhooks/ultramsg?secret=...`.

FreJun calls need `PUBLIC_URL` on **HTTPS**, so add a TLS certificate to the costing nginx (port 443) before going live with Scenario 3 calls.

---

## Go-live checklist

- [ ] `ADMIN_PASSWORD` and `DASHBOARD_PASSWORD` set (different codes; business users get only the dashboard one).
- [ ] `APP_PASSWORD` set – otherwise anyone with the URL can place calls on the FreJun account.
- [ ] `ULTRAMSG_BASE_URL` / `ULTRAMSG_TOKEN` work: a live Test-environment vendor test reaches your phone.
- [ ] `SCM_HOD_MOBILE`, `SCM_HEAD_EMAIL` filled; Phone_Master has Scenario 1/2/3 numbers for the vendors.
- [ ] `PUBLIC_URL` (Option A) points at this server and `/health` answers over HTTPS – needed for Scenario 3 calls.
- [ ] `FOLLOWUP_ALERT_MOBILE` set, so a failed automatic run reaches someone.
- [ ] UltraMsg webhook → `https://<host>/webhooks/ultramsg?secret=<ULTRAMSG_WEBHOOK_SECRET>`, *message received* on.
- [ ] The old **n8n workflow is off**, or vendors get messages from both.
- [ ] Admin → Production → Settings: daily time (IST), check period 1 day, pilot mode as intended, automatic
      **dry** for a few days; compare the dashboard with what you expect.
- [ ] Then automatic **live** (asks you to type LIVE). Pilot mode on = only pilot vendors, all messages to the pilot number.
- [ ] Database backups scheduled.

---

## Configuration

All settings are environment variables (`.env` locally, the service's environment in production; template:
`.env.example`). Settings changed in the admin page (schedule, pilot, check period, …) are stored per environment
in PostgreSQL and override the defaults below.

**Web app and voice agent**

| Variable | Default | |
|---|---|---|
| `APP_PASSWORD` | – | access code for the call pages (and admin fallback) |
| `GEMINI_API_KEY` | – | Google AI Studio key |
| `GEMINI_MODEL` / `GEMINI_VOICE` / `GEMINI_LANGUAGE` | `gemini-3.8-live` / `Aoede` / auto | |
| `GEMINI_SILENCE_MS` | `300` | silence before the agent answers |
| `TELER_API_KEY`, `FREJUN_PHONE_NUMBER` | – | FreJun account and caller number (+91…) |
| `PUBLIC_URL` | – | public HTTPS base URL FreJun uses (not on Render) |
| `MY_PHONE_NUMBER` | – | only for `scripts/make_call.py` |
| `MAX_SESSION_SECONDS` | `900` | hard stop per call |
| `CALL_RATE_LIMIT` / `CALL_RATE_WINDOW` | `5` / `600` | calls per visitor per window (seconds) |
| `LOG_LEVEL` | `INFO` | |

**Access**

| Variable | |
|---|---|
| `ADMIN_PASSWORD` | admin page code (falls back to `APP_PASSWORD`; neither set = this PC only) |
| `DASHBOARD_PASSWORD` | dashboard-only code for business users (the admin code also works) |

**Google Sheet and database**

| Variable | Default | |
|---|---|---|
| `PO_SHEET_ID` | (CE - Pending PO) | the spreadsheet |
| `GOOGLE_DELEGATED_USER` | – | Workspace user the service account acts as |
| `GOOGLE_SERVICE_ACCOUNT_FILE` / `GOOGLE_SERVICE_ACCOUNT_JSON` | `service.json` / – | key file path, or its contents (servers) |
| `PO_DB_HOST` / `PO_DB_PORT` / `PO_DB_USER` / `PO_DB_PASSWORD` | `localhost` / `5432` / `postgres` / – | |
| `FOLLOWUP_PROD_DB` / `FOLLOWUP_TEST_DB` | `pending_po` / `pending_po_test` | |
| `PO_SYNC_INTERVAL_SECONDS` | `60` | `backend.po_sync` refresh interval |

**Follow-up** (defaults; the admin page overrides most of them per environment)

| Variable | Default | |
|---|---|---|
| `FOLLOWUP_SCHEDULER` | `true` | automatic runs inside the web app |
| `FOLLOWUP_RUN_AT` | `08:30` | daily run time (IST) |
| `FOLLOWUP_CHECK_PERIOD_MINUTES` | `1440` | overdue check period (production) |
| `FOLLOWUP_PILOT_MODE` / `FOLLOWUP_PILOT_VENDOR_CODES` / `FOLLOWUP_PILOT_MOBILE` / `FOLLOWUP_PILOT_EMAIL` | `true` / `400186` / – / – | pilot: only these vendors, every message to this number / email |
| `FOLLOWUP_PILOT_CALL_MOBILE` | – | pilot: the Scenario 3 voice agent call rings this number (blank = pilot mobile) |
| `FOLLOWUP_VENDOR_SCOPE` | `selected` | `selected`: only the selected vendors (initially `FOLLOWUP_PILOT_VENDOR_CODES`) are followed up; `all`: every vendor. Changed on the dashboard |
| `FOLLOWUP_TEST_SCENARIO_MOBILE` | – | test copies get this as every Scenario 1/2/3 number |
| `FOLLOWUP_AGENT_CALLS` / `FOLLOWUP_MEET_INVITES` | `true` / `true` | Scenario 3 voice call / meeting Google Meet |
| `FOLLOWUP_MAX_LINES_PER_MESSAGE` | `15` | |
| `FOLLOWUP_SEND_INTERVAL_SECONDS` | `1.5` | pause between WhatsApp sends |
| `FOLLOWUP_ALERT_MOBILE` | – | WhatsApp when an automatic run fails |
| `ULTRAMSG_BASE_URL` / `ULTRAMSG_TOKEN` | – | WhatsApp sending |
| `ULTRAMSG_WEBHOOK_SECRET` | – | protects `/webhooks/ultramsg` |
| `SCM_HOD_NAME` / `SCM_HOD_MOBILE` / `SCM_HEAD_EMAIL` | `SCM Head` / – / – | meeting stage recipients |

**Monitoring**: `LANGSMITH_TRACING`, `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT` (default `po-voice-agent`),
`LANGSMITH_ENDPOINT` (EU workspaces).

---

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
