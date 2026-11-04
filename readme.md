# PO Agent – overdue purchase order follow-up

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

```
 Google Sheet (users maintain Master, Phone_Master, ...)
        │  po_sync: copies every tab, every 60 s (or before each run)
        ▼
 PostgreSQL  pending_po (production)       pending_po_test (test: a copy you can edit)
   sheets.*    the sheet tabs                sheets.*    copied from production
   followup.*  tracker, logs, tasks, runs,   followup.*  its own state
               daily overdue checks
        │
        ▼
 Web app (one FastAPI process: uvicorn main:app)
   scheduler ── daily run ──► WhatsApp (UltraMsg) · voice agent call (FreJun ⇄ Gemini) · Google Meet
   /dashboard   /admin   /   /webhooks/ultramsg (vendor replies)   /media (phone audio)
```

**Scenarios.** Each run checks Master once per check period (production: one IST day; the latest run of the day
counts) and records which lines are overdue. The number of checks in a row a line has been overdue confirms its scenario:

| Overdue checks in a row | Scenario | Who gets WhatsApp | Also |
|---|---|---|---|
| 1 (today only) | Scenario 1 | vendor planner + coordinator (Phone_Master Scenario 1, else Master) | |
| 2 (also yesterday) | Scenario 2 | vendor management (Scenario 2) | |
| 3 | Scenario 3 | buyer | row in `Call_Tasks`; the voice agent calls the Scenario 3 number |
| 4 or more | Meeting | buyer + SCM HOD | row in `Meeting_Tasks` + Google Meet invite |

- A check in which the line is not overdue breaks the streak; overdue again later starts at Scenario 1.
  A day with no check (server off, holiday) neither counts nor breaks it.
- Scenarios are never skipped: Scenario 2 goes out only once it is confirmed *and* Scenario 1 went out.
- One message per vendor per scenario per day; `Alert_Log` dedupe keys stop repeats if a run is started twice.
- On the first day there is no history, so every overdue line starts at Scenario 1.
- The rules: `backend/po_followup/domain/overdue.py` (streaks) and `domain/escalation.py` (levels).

**Two environments**, switched at the top of the admin page, each its own PostgreSQL database:

| | Production (`pending_po`) | Test (`pending_po_test`) |
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
scm.ai.bullmachine.in/voicecall/ --> costing nginx (:80) --> this nginx (:8082) --> web (127.0.0.1:8001)
```

Costing must be running because its nginx owns port 80. Deploy it first if you have not already (costing README, "Deploy on an Ubuntu server (Docker)"). Local and architecture detail: [Option C](#option-c--docker-with-nginx-local-and-shared-server); troubleshooting: [DEPLOY.md](DEPLOY.md).

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

- An A record for `scm.ai.bullmachine.in` (and `ai.bullmachine.in`, or a wildcard `*.ai.bullmachine.in`) pointing at the VM's public IP.
- Inbound **80** open in the cloud security group or NSG (443 too once HTTPS is added); 22 limited to known IPs.
- Keep 8001, 8082 and 5432 closed to the internet; only nginx is public.

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
PUBLIC_URL=https://scm.ai.bullmachine.in/voicecall
GOOGLE_DELEGATED_USER=<workspace user>
PO_SHEET_ID=<spreadsheet id>
PO_DB_PASSWORD=<strong database password>
VOICE_DOMAIN=scm.ai.bullmachine.in
# BASE_PATH=/voicecall        # default; change only for another path
```

- Copy the Google service account key to the project root as `service.json` (`scp` from your PC); it is mounted read-only into the containers.
- `PUBLIC_URL` must include `/voicecall` and be HTTPS for FreJun calls. HTTPS is terminated on the costing nginx.
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
curl http://127.0.0.1:8001/voicecall/health                                 # the app
curl -H "Host: scm.ai.bullmachine.in" http://localhost:8082/voicecall/health  # this nginx
curl -H "Host: scm.ai.bullmachine.in" http://localhost/voicecall/health       # through the costing edge on :80
```

Then open `http://scm.ai.bullmachine.in/voicecall/admin`. `ADMIN_PASSWORD` and `DASHBOARD_PASSWORD` must be set, because every request through nginx counts as remote.

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

This project and the costing project each have their own `docker-compose.yml` and their own nginx. Each app runs under its own **base path**: this one under `/voicecall/`, costing under `/costing/`. `/` redirects to the base path. Domains and base paths come from `.env`, so the same files work on your PC and on the server:

```
:80 --> costing nginx --+-- COSTING_DOMAIN/costing/   --> costing app
                        +-- VOICE_DOMAIN/voicecall/   --> this nginx (:8082) --> web (127.0.0.1:8001/voicecall/)
```

| | voice_call (this project) | costing |
|---|---|---|
| App port directly | http://127.0.0.1:8001/voicecall/ | http://127.0.0.1:8000/costing/ |
| Through nginx, local (default) | http://scm.localhost/voicecall/ (also http://localhost:8082/voicecall/) | http://localhost/costing/ |
| Through nginx, server | http://scm.ai.bullmachine.in/voicecall/ | http://ai.bullmachine.in/costing/ |

Settings in `.env`:
- `BASE_PATH`: default `/voicecall`; leave it unset unless you want another path.
- `VOICE_DOMAIN`: default `scm.localhost`; on the server, `scm.ai.bullmachine.in`, with the same value in costing's `.env`.

`docker compose up` here starts:

| Service | What it runs |
|---|---|
| `web` | Built from the [Dockerfile](Dockerfile): Python deps installed with uv into `/opt/venv`, plus `main.py`, `backend/` and `frontend/` in `/app`. Started with `uvicorn main:app` on port 8000 under `BASE_PATH`. `frontend/` is plain HTML/CSS with no build step; the pages use relative URLs, so they work under any base path. |
| `sync` | Same image; runs `python -m backend.po_sync` (sheet → PostgreSQL). |
| `db` | PostgreSQL 17. |
| `nginx` | Host port 8082; config is [deploy/nginx.conf.template](deploy/nginx.conf.template). Passes the `/media` and `/test-ws` WebSockets. |

Port 80 belongs to the costing project's nginx, which forwards `VOICE_DOMAIN` here, so **run costing as well** (its README, "Docker (local and server)"). Without it, use http://localhost:8082/voicecall/ or http://127.0.0.1:8001/voicecall/.

Running outside Docker (`uvicorn main:app`, Render) with `BASE_PATH` unset still serves the app at `/`, as before.

**Run locally (Docker Desktop)**
1. Start Docker Desktop. `.env` and `service.json` are in the project root as for development. Leave `VOICE_DOMAIN`/`BASE_PATH` unset.
2. Behind the office SSL-inspection firewall (Fortinet), put the firewall's CA certificate in `certs/` as a `.crt` file (PEM), or builds fail with `CERTIFICATE_VERIFY_FAILED`. Export it from Windows (`certmgr.msc` → Trusted Root → the Fortinet `FG…` certificate → Export, Base-64). `certs/*.crt` is git-ignored. On the server, leave `certs/` empty.
3. `docker compose up -d --build`, then open http://127.0.0.1:8001/voicecall/, http://localhost:8082/voicecall/, or http://scm.localhost/voicecall/ (with costing running).
4. Known office-network limit: Python 3.14 rejects the Fortinet CA ("Basic Constraints of CA cert not marked critical"), so the sheet sync and Google/Gemini calls fail on the office network. The pages still load. This does not happen on the server or on a network without SSL inspection.

**Server deployment**: see [Ubuntu server deployment (Docker)](#ubuntu-server-deployment-docker) above for the full step-by-step setup (Docker install, DNS/firewall, `.env`, `service.json`, build, data, verify, updates and backups).

**Changing the domain or base path later**: no file edits are needed.
- **Domain:** set `VOICE_DOMAIN` (and `PUBLIC_URL`) in `.env` here and `VOICE_DOMAIN` in costing's `.env`. Then run `docker compose up -d` here and `docker compose up -d nginx` in costing, and update DNS.
- **Base path:** set `BASE_PATH` (and the path part of `PUBLIC_URL`) in `.env`, then run `docker compose up -d`.

The UltraMsg webhook URL also includes the base path: `https://<host>/voicecall/webhooks/ultramsg?secret=...`.

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

**Dashboard** (`/dashboard`, business users): tabs **All overdue · Scenario 1 · Scenario 2 · Scenario 3 · Meeting**
with counts. Per line: pending qty, due date, days late, the overdue streak (last 7 checks) and each scenario's
status – Sent + date, Due now, Confirmed (waits for the previous scenario), On hold (pilot), Not yet + the date the
next check would confirm it. Search, vendor / buyer / days-late filters, sortable columns, group by PO or all lines,
CSV export of what is filtered, line details on click. Refreshes every 2 minutes; `?env=test` for test data.

**Vendors to follow up** (bottom of the dashboard, production and `?env=test`):
- **Which vendors the follow-up runs for**: *Selected vendors only* (default – start with 5–10) or *All vendors*.
  Tick **Follow up** next to vendors (search by name, code or number; show Selected / With overdue lines / All),
  then **Save selection**. Unticked vendors show *On hold* on the overdue list. Switching production to all vendors
  asks you to type ALL. Pilot mode (admin) still sends every message to the pilot number, on top of this.
- **Scenario 1 / 2 / 3 numbers** (Phone_Master): **Edit** per vendor (or from a line's details) and **Add vendor**.
  Blank = use Master's numbers (Scenario 3 blank = Scenario 2's number); several numbers: separate with commas.
- **Bulk**: **Download CSV** (every vendor: code, name, Follow up Yes/No, Scenario 1/2/3), change it in Excel,
  then **Bulk upload** it (or paste rows from Excel). Missing columns and blank Follow up cells are left as they are;
  an empty Scenario cell clears that number. Every row is checked and the changes previewed before *Apply*;
  if any row is wrong nothing is saved.

Production numbers are saved to the Phone_Master tab of the Google Sheet first, then to the production copy, so the
next run uses them; test changes go to the test database only. The selection is a setting of each environment
(`pilot_vendor_codes`, `vendor_scope`). Anyone with the dashboard code can change both.

**Admin** (`/admin`): **Overview** (health, run now: Preview / Dry / Live, run history), **Vendor trace**
(Scenario 1 → 2 → 3 → meeting timeline per vendor, every message, numbers used), **Test a vendor**,
**Settings** (schedule, check period, pilot, numbers), **Data & logs**. Live production runs and automatic live
sending must be confirmed by typing LIVE.

**Test a vendor** (Test environment): pick a vendor, enter your WhatsApp number and a check period (e.g. 1 minute),
Start. It resets that vendor's test history, sends Scenario 1 at once, and the scheduler moves it to Scenario 2, 3
and the meeting one period apart. Every message starts with "PILOT TEST – real recipient would be: …".

**Voice agent** (`/`): **Normal call** rings a number from the FreJun number; **Voice agent** talks in the browser.
Change what the agent says in `backend/voice_agent/prompts.py`.

**Reliability built in**: one run at a time per environment across processes (PostgreSQL advisory lock); failed
automatic runs retried 3× 10 minutes apart; `Alert_Log` written after each vendor; runs interrupted by a crash
marked failed within a minute; timeouts on PostgreSQL, Google and UltraMsg calls; a Health card on the admin page.

---

## Command line

```bash
python -m backend.po_followup admin [--port 8100]              # web app on 127.0.0.1 + scheduler
python -m backend.po_followup run --env test --mode preview    # one run now (env prod|test, mode preview|dry|live)
python -m backend.po_followup scheduler                        # scheduler without the web app
python -m backend.po_followup copy-prod-to-test                # refresh test data from production
python -m backend.po_followup setup-tabs                       # create missing tracking tabs in the Google Sheet
python -m backend.po_followup build-phone-master               # create Phone_Master from Master's vendors
python -m backend.po_sync [--once]                             # Google Sheet -> PostgreSQL mirror
python scripts/make_call.py                                    # ring MY_PHONE_NUMBER with the agent
```

The sheet mirror (`backend.po_sync`) writes one table per tab in schema `sheets` (`sheets.master`,
`sheets.phone_master`, …; cleaned-up column names, original header as the column comment, all TEXT plus
`sheet_row`) and `public.sync_state` (rows, last sync, last error per tab). Only changed tabs are rewritten, each in
one transaction.

---

## Project layout
Backend (one Python package) and frontend (HTML/CSS served by the backend) are kept apart:
```
main.py                      # entry point: uvicorn main:app
backend/
  core/                      # env (.env + OS certificates), google_auth, tracing, frontend paths
  voice_agent/               # the FastAPI app: app.py, config, prompts, Gemini, audio, security, call tickets
    routes/                  #   pages (/, /health, /api/status), calls (/api/call, FreJun), media (WS /media), browser
  po_followup/
    api/                     #   HTTP routers: admin, dashboard, replies (vendor WhatsApp webhook)
    models/                  #   Pydantic request/response models
    domain/                  #   pure rules: overdue (streaks), escalation (levels), messages
    services/                #   run (one daily run), followup (runs, settings, scheduler), trace, dashboard
    integrations/            #   Google Sheets, UltraMsg, Google Meet, voice agent calls
    db/store.py              #   PostgreSQL storage, one database per environment
    config.py, cli.py
  po_sync/                   # Google Sheet -> PostgreSQL mirror
frontend/
  pages/                     # index.html, admin.html, dashboard.html
  assets/ui.css              # shared fonts, top bar, typography (served at /assets)
scripts/make_call.py
tests/                       # pytest, one folder per module
```
Conventions: routers in `api/` stay thin and call `services/`; `domain/` does no I/O; request/response shapes live
in `models/`; everything a browser loads lives in `frontend/`; imports are absolute (`from backend.core import env`).

---

## Monitoring

`GET /health` for uptime checks. The admin Overview shows the scheduler heartbeat, failing automatic runs and stuck runs.

LangSmith (optional): set `LANGSMITH_TRACING=true` and `LANGSMITH_API_KEY`. Tracing is off without a key, and a
LangSmith outage never affects calls or runs.

| Trace | Shows |
|---|---|
| `phone_call` / `browser_session` | each turn (vendor said → agent said), Gemini tokens, response latency, interruptions |
| `place_call`, `call_status` | FreJun call creation and hang-up reasons |
| `po_followup_daily_run` | per vendor/scenario: lines, recipients, send status; totals |

Phone numbers are masked in traces; call transcripts and WhatsApp texts are sent as they are.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Admin / dashboard: "Admin unavailable: PostgreSQL driver not installed" | install `psycopg[binary]` (Render has none – see Option B) |
| Admin / dashboard: "database not reachable" | PostgreSQL running? `PO_DB_*` correct? The page retries by itself |
| "Set ADMIN_PASSWORD on the server …" (403) | behind a proxy or from another machine, a code is required |
| Dashboard shows every line at Scenario 1 | no earlier checks yet – they build up one per day |
| No automatic runs | admin → Settings: automatic on? `FOLLOWUP_SCHEDULER` not false? The Health card shows the heartbeat |
| No ring | `TELER_API_KEY`; `FREJUN_PHONE_NUMBER` and the target in +91… format |
| Call connects, then drops ("Failed to fetch call flow") | server asleep (Render free) or wrong public URL; on Render don't set `PUBLIC_URL` |
| Call connects, silence; log "missing or invalid call ticket" | the machine placing the call and the server streaming it need the same `TELER_API_KEY` and current code |
| Agent silent | `GEMINI_API_KEY`; look for "Stream error" in the logs |
| "Wrong access code" | must match `APP_PASSWORD` / `ADMIN_PASSWORD` / `DASHBOARD_PASSWORD` on that server |
| SSL / certificate errors on an office network | the app uses the OS certificate store (truststore); install the company root CA in Windows |
| WhatsApp FAILED in Alert_Log | UltraMsg token / instance; bad numbers show as INVALID_MOBILE – fix them in Phone_Master |
