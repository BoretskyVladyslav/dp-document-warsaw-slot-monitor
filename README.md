# DP Document Warsaw Slot Monitor

Telegram bot that watches the DP «Документ» electronic queue in Warsaw and notifies subscribers when appointment slots appear or disappear.

City is configured via `TARGET_URL` and `CITY_NAME` so another center can be monitored without code changes.

## Commands

| Command | Description |
|---|---|
| `/start` | Subscribe to slot alerts |
| `/stop` | Unsubscribe |
| `/help` | Command summary |
| `/status` | Last check, slot state, subscriber count, uptime (admins only) |

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
copy .env.example .env
```

Fill `BOT_TOKEN` and `ADMIN_IDS` in `.env`, then:

```bash
python -m src.main
```

## Docker

```bash
docker compose up --build
```

SQLite is stored in the `monitor-data` volume (`DATABASE_PATH=/data/monitor.db`).

## Environment

See `.env.example`. `CHECK_INTERVAL_SECONDS` defaults to 180. `PROXY_URL` and `HEADLESS` are optional. `SERVICE_OPTION` selects a service in a dropdown when the target page requires it.
