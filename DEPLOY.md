# Deploy with Docker

Runs three containers: `web` (voice agent, `/admin`, `/dashboard`), `sync` (Google Sheet -> PostgreSQL mirror) and `db` (PostgreSQL 17). Everything is under the base path `/scm/`. There is no nginx in this project: the central nginx in the costing project (`~/costing`, port 80) serves `ai.bullmachine.in/scm/` and reaches `web` as `voice-web` on the shared external Docker network `ai-proxy`. Both projects are started separately; create the network once per machine with `docker network create ai-proxy`. Full local and server steps are in [readme.md](readme.md) under "Option C – Docker with nginx".

## Prerequisites

- Docker Desktop (Windows/Mac) or Docker Engine + Compose plugin (Linux), **running**
- `service.json` (Google service account key) in the project root
- API keys: Gemini, Teler/FreJun, and optionally UltraMsg and LangSmith

## 1. Configure

```bash
cp .env.example .env        # Windows: copy .env.example .env
```

Edit `.env` and fill in at least:

| Variable | Why |
|---|---|
| `GEMINI_API_KEY` | voice agent |
| `TELER_API_KEY`, `FREJUN_PHONE_NUMBER` | phone calls |
| `PUBLIC_URL` | public HTTPS URL FreJun calls back (see step 4) |
| `APP_PASSWORD`, `ADMIN_PASSWORD` | page logins |
| `GOOGLE_DELEGATED_USER`, `PO_SHEET_ID` | sheet sync |
| `PO_DB_PASSWORD` | database password (defaults to `postgres`; change it in production) |

You do not need to set `PO_DB_HOST`, `PO_DB_USER` or `GOOGLE_SERVICE_ACCOUNT_FILE`: `docker-compose.yml` overrides them to point at the `db` container and the mounted `service.json`.

## 2. Build and start

```bash
docker compose up -d --build
```

Check that everything is up:

```bash
docker compose ps
curl http://127.0.0.1:8001/scm/health                                # app directly (loopback)
curl http://localhost/scm/health                                     # through the central nginx (costing project)
```

The app is served under `BASE_PATH` (default `/scm`; must equal `SCM_PATH` of the central nginx). Open http://127.0.0.1:8001/scm/ or http://localhost/scm/ (through the central nginx). `/` redirects to `/scm/`.

## 3. Common commands

```bash
docker compose logs -f web          # follow web logs (also: sync, db)
docker compose restart web          # restart one service
docker compose up -d --build        # rebuild after code changes
docker compose up -d web db         # run without the sync worker
docker compose down                 # stop (database data is kept)
docker compose down -v              # stop AND delete the database volume
```

Run the follow-up CLI inside the web container:

```bash
docker compose exec web python -m backend.po_followup --help
```

## 4. Public URL for calls

FreJun must reach the app over HTTPS. On the server that is `https://ai.bullmachine.in/scm` (TLS on the costing project's central nginx), so set `PUBLIC_URL=https://ai.bullmachine.in/scm`. For testing, use a tunnel to the app port and set `PUBLIC_URL` to the tunnel URL plus `/scm`:

```bash
cloudflared tunnel --url http://localhost:8001
# or: ngrok http 8001
```

Put the resulting URL in `PUBLIC_URL` in `.env`, then `docker compose up -d` to apply it.

## 5. Deploy on a server

```bash
git clone <repo> && cd voice_call
# copy .env and service.json onto the server (never commit them)
docker compose up -d --build
```

Containers use `restart: unless-stopped`, so they come back after a reboot. To update: `git pull && docker compose up -d --build`.

## Troubleshooting

- **`failed to connect to the docker API`**: Docker Desktop isn't running; start it.
- **`env file .env not found`**: run step 1.
- **Admin API answers 503 / DB errors**: check `docker compose logs db`; the `web` container waits for the database health check before starting.
- **Changed `PO_DB_PASSWORD` after first start and the DB rejects it**: the password is fixed when the volume is first created. Run `docker compose down -v` (deletes DB data) and start again.
- **Sheet sync fails with credentials errors**: confirm `service.json` is in the project root and `GOOGLE_DELEGATED_USER` is set.
