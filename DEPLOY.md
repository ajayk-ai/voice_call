# Deployment Guide – PO Follow-up Voice Agent (Docker, single server)

Target: this Ubuntu server (AWS), PostgreSQL on the host, application in Docker, reached directly at
`http://<server-ip>:8082` (no domain, no HTTPS).

| Component | Where it runs | Notes |
|---|---|---|
| PostgreSQL | Host (`127.0.0.1:5432`) | Role `po_app`; database `pending_po` |
| `app` container | Host network, port 8082 | Web app: voice agent, `/admin`, `/dashboard`, follow-up scheduler |
| `sync` container | Host network | Google Sheet to PostgreSQL mirror (`python -m backend.po_sync`) |

Containers use host networking so they reach PostgreSQL at `localhost`; PostgreSQL stays closed to the network.

---

## 1. Prerequisites

- Docker and Docker Compose installed; your user is in the `docker` group.
- PostgreSQL running on the host.
- Google service account key file (`service.json`) with domain-wide delegation for `GOOGLE_DELEGATED_USER`.
- API keys: Gemini, Teler/FreJun, Ultramsg.
- Port 8082 free on the server (`ss -tlnp | grep 8082` returns nothing).

## 2. Get the code

```bash
cd /home/bulluser/voice_call
git pull
```

## 3. Database (one time)

```bash
sudo -v && bash scripts/setup_db.sh
```

This creates role `po_app` (login, `CREATEDB`, not superuser) with a random password, creates both databases,
and writes `PO_DB_HOST`, `PO_DB_PORT`, `PO_DB_USER`, `PO_DB_PASSWORD` to `.env` (mode 600).
Re-running keeps the existing password; use `--reset-password` to rotate it.

## 4. Configure `.env`

If `.env` does not exist, the script above creates it from `.env.example`. Set every value below.

| Variable | Value |
|---|---|
| `PUBLIC_URL` | `http://<server-ip>:8082` |
| `APP_PASSWORD` | Admin sign-in password |
| `ULTRAMSG_WEBHOOK_SECRET` | Random string; used in the vendor-reply webhook URL |
| `GEMINI_API_KEY`, `TELER_API_KEY`, `FREJUN_PHONE_NUMBER` | From the providers (phone number with +91) |
| `ULTRAMSG_BASE_URL`, `ULTRAMSG_TOKEN` | Ultramsg instance |
| `GOOGLE_DELEGATED_USER`, `PO_SHEET_ID` | Workspace user and the PO sheet |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | Keep `service.json` |

Generate secrets with `openssl rand -base64 24 | tr -d '/+='`. Do not commit `.env` (it is in `.gitignore`).
Passwords travel unencrypted over HTTP: use unique values that are not reused anywhere else.

Place the Google key next to `.env`:

```bash
cp /path/to/key.json /home/bulluser/voice_call/service.json
chmod 600 service.json
```

The file must exist before `docker compose up`; otherwise Docker creates a directory named `service.json`.
Keep the name exactly `service.json` and match `GOOGLE_SERVICE_ACCOUNT_FILE=service.json` in `.env`.
Containers run as uid/gid 1001 (the `bulluser` account, default in `docker-compose.yml`) so the mode 600 key stays
readable. If your account has a different id (`id -u`), set `APP_UID` and `APP_GID` in `.env`.

## 5. Open the network port

AWS Console → EC2 → Security Groups → inbound rule: TCP 8082.
- FreJun and Ultramsg call into the server, so allow `0.0.0.0/0` if they must reach it.
- If only office staff use `/admin` and `/dashboard`, restrict the source to the office IP range.

Do not open port 5432 (PostgreSQL).

## 6. Build and start

```bash
cd /home/bulluser/voice_call
docker compose up -d --build
docker compose ps                      # both services "Up"; app shows "healthy" after about 20 seconds
```

## 7. Verify

```bash
curl http://127.0.0.1:8082/health      # "Voice agent server is running"
curl http://<server-ip>:8082/health    # from another machine: confirms the security group rule
docker compose logs --tail 50 app      # no "disabled" warnings for the keys you configured
```

First data load:

```bash
docker compose exec app python -m backend.po_sync --once
```

Then open `http://<server-ip>:8082/admin` → System tests, and run all three.

## 8. Vendor reply webhook (Ultramsg)

Set the webhook URL in the Ultramsg instance to:

```
http://<server-ip>:8082/webhooks/ultramsg?secret=<ULTRAMSG_WEBHOOK_SECRET>
```

## 9. Go-live

Use Trial in Vendor Control first. Live sends real messages to the chosen vendors.

**Known limitation:** FreJun may reject a non-HTTPS callback URL. This is unverified. Place one test call early
(Scenario 3 or the web page). If the call drops immediately, HTTPS is required (domain plus reverse proxy).

## 10. Operations

| Task | Command |
|---|---|
| Logs (follow) | `docker compose logs -f app` / `docker compose logs -f sync` |
| Status | `docker compose ps` |
| Restart | `docker compose restart app` |
| Apply a `.env` change | `docker compose up -d` (`restart` does not re-read `.env`) |
| Stop | `docker compose down` |
| Deploy a new version | `git pull && docker compose up -d --build` |

Tables and columns are created on start; there are no manual migrations.
Run one `app` container only (do not scale it): call audio WebSockets and the rate limiter live in the process.
Containers restart automatically after a reboot (`restart: unless-stopped`).

**Backups.** Follow-up state (messages sent, daily checks) exists only in PostgreSQL:

```bash
# /etc/cron.d/po-agent-backup
30 2 * * * postgres pg_dump -Fc pending_po > /var/backups/pending_po_$(date +\%a).dump
```

**Rollback.** `git checkout <previous-commit> && docker compose up -d --build`. Database changes are additive,
so older code keeps working with the current schema. (Assumption; confirm after any schema change.)

## 11. Troubleshooting

| Symptom | Check |
|---|---|
| `app` unhealthy / restarting | `docker compose logs app`; usually a missing or wrong `.env` value |
| `password authentication failed` | `PO_DB_PASSWORD` in `.env` does not match the role; re-run `scripts/setup_db.sh --reset-password` |
| `connection refused` to PostgreSQL | `systemctl status postgresql`; confirm `PO_DB_HOST=localhost`, `PO_DB_PORT=5432` |
| Admin cannot sign in | `APP_PASSWORD` is empty |
| Cannot reach `:8082` from outside | AWS security group rule (section 5) |
| Call connects, then drops | `PUBLIC_URL` wrong or stale; FreJun may need HTTPS (section 9) |
| `service.json` is a directory | `docker compose down`, `rmdir service.json`, copy the key file, `docker compose up -d` |
| `Permission denied: 'service.json'` | Container uid differs from the file owner: set `APP_UID`/`APP_GID` in `.env` (section 4) |

## 12. Security notes

- Traffic is plain HTTP: passwords and call data are readable on the network path. Use HTTPS when a domain is available.
- `X-Forwarded-For` is trusted by the per-client call rate limiter (`backend/voice_agent/security.py`). Without a
  reverse proxy a client can fake it and bypass the limit. Keep `APP_PASSWORD` set.
- `.env` and `service.json` contain secrets. Keep them mode 600 and out of git, backups shared with others, and chat.
- Phone numbers and vendor data are personal data under DPDPA; limit access to `/admin` and `/dashboard` accordingly.
