# DP Document Warsaw Slot Monitor

Telegram bot that watches the DP «Документ» electronic queue and notifies subscribers when appointment slots appear or disappear. Default target is Warsaw; another city is a config change (`TARGET_URL`, `CITY_NAME`), not a code change.

## Prerequisites

- Python 3.11+
- Docker and Docker Compose v2 (for container deploy)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- Your Telegram numeric user ID for `/status` (for example via [@userinfobot](https://t.me/userinfobot))

## Configuration

```bash
copy .env.example .env
```

On Linux/macOS use `cp .env.example .env`. Edit `.env` before the first run.

| Variable | Required | Default | Description |
|---|---|---|---|
| `BOT_TOKEN` | yes | — | Bot token from BotFather |
| `ADMIN_IDS` | yes for `/status` | empty | Comma-separated Telegram user IDs |
| `TARGET_URL` | yes | Warsaw e-queue URL | Page Playwright opens each cycle |
| `CHECK_INTERVAL_SECONDS` | no | `600` | Pause between checks (15–3600) |
| `PROXY_URL` | no | unset | Playwright proxy, e.g. `http://127.0.0.1:8080` |
| `CITY_NAME` | no | `Warsaw` | Label used in alerts and `/status` |
| `HEADLESS` | no | `true` | `false` only for local debugging |
| `STORAGE_STATE_PATH` | no | `data/storage_state.json` | Cookies from `scripts/solve_session.py` |
| `DATABASE_PATH` | no | `data/monitor.db` | SQLite file; Compose sets `/app/data/monitor.db` |
| `SERVICE_OPTION` | no | unset | Dropdown option text to click after load |
| `PLAYWRIGHT_TIMEOUT_MS` | no | `60000` | Navigation timeout |

Never commit `.env`. `.env.example` has no secrets.

## Run locally (Python)

From the repository root:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
python scripts/solve_session.py
python -m src.main
```

Linux/macOS activate with `source .venv/bin/activate`. SQLite is created at `data/monitor.db` (gitignored). Logs go to stdout.

## Run with Docker Compose

```bash
docker compose up --build -d
```

Compose bind-mounts `./data` to `/app/data` so subscribers survive container restarts. Chromium runs headless. Policy is `restart: unless-stopped`. A process healthcheck starts after 60s.

```bash
docker compose logs -f bot
docker compose ps
docker compose down
```

## Subscribers and logs

| Action | How |
|---|---|
| Subscribe | User sends `/start` to the bot |
| Unsubscribe | User sends `/stop` |
| Help | `/help` |
| Monitor snapshot | `/status` (IDs in `ADMIN_IDS` only) |

Inspect the database on the host (bind mount) or after a local Python run:

```bash
sqlite3 data/monitor.db "SELECT user_id, chat_id, username, is_active FROM subscribers;"
```

Docker logs:

```bash
docker compose logs -f bot
```

Local Python logs are structured lines on stdout (`slot_check`, `notify_deferred`, `notified_slots_available`, …).

## Tests

```bash
python -m unittest discover -s tests -v
```
