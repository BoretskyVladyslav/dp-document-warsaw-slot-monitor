# DP Document Warsaw Slot Monitor

Telegram bot that monitors the DP «Документ» electronic queue and notifies active subscribers when booking becomes available. Warsaw is the initial target; city identity and URL remain configuration values.

## Runtime model

The monitor operates only through CDP attachment to a dedicated, visible Google Chrome profile on Windows.

- Chrome is started by `scripts\start_chrome_cdp.bat`.
- The script opens `TARGET_URL` in a dedicated persistent profile.
- A user completes any Cloudflare challenge in that tab.
- The bot attaches to `CDP_URL`, finds that exact tab, reloads it, waits for the occupied banner or telephone field, then reads visible DOM evidence. There is no confirmed in-page refresh control, so a top-level reload remains the refresh path.
- The bot never launches a browser, creates a tab, imports `cf_clearance`, or falls back to headless Chromium.

Cloudflare access cannot be guaranteed. The reliability contract is fail-closed classification, durable notifications, controlled recovery, and no automated challenge loop.

## Requirements

- Windows 10 or later
- Python 3.14+
- Google Chrome
- Telegram bot token
- Telegram private-chat ID for each administrator

## Configuration

Copy the example and edit the resulting `.env`:

```powershell
Copy-Item .env.example .env
```

Required settings:

- `BOT_TOKEN`: BotFather token.
- `ADMIN_IDS`: comma-separated private-chat IDs for diagnostics, `/check_now`, and system incidents.
- `TARGET_URL`: exact queue page.
- `CDP_URL`: normally `http://127.0.0.1:9222`.

Optional settings:

- `CITY_NAME`: city label; default `Warsaw`.
- `CHECK_INTERVAL_SECONDS`: scheduled interval from 15 to 3600 seconds; default `300`, with ±15 seconds of jitter.
- `DATABASE_PATH`: SQLite path; default `data/monitor.db`.
- `CHECK_ONCE`: perform one cycle and exit; default `false`.

Never commit `.env`.

## Quick start (Windows)

Double-click these files from the project folder:

1. `setup.bat` — installs Python packages, Playwright Chromium, and copies `.env.example` to `.env` if needed.
2. Edit `.env` and set `BOT_TOKEN` (and confirm `ADMIN_IDS`).
3. `START_BOT.bat` — starts dedicated CDP Chrome, waits 3 seconds, then runs the bot.

Complete any Cloudflare challenge in the Chrome tab that opens. Leave that Chrome window and tab open while the bot is running.

If `setup.bat` reports that Python is missing, install Python from python.org and enable **Add python.exe to PATH**.

Manual commands below remain available for diagnostics.

## Installation

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## Start

For a clean diagnostic restart (stops `src.main`, dedicated CDP Chrome, pending outbox, and monitor latch/cooldown; **keeps subscribers**; wipes `CDP_Profile` cookies/cache without deleting the profile directory):

```powershell
scripts\clean_run.bat
scripts\start_chrome_cdp.bat
.venv\Scripts\python.exe -m src.main
```

Start the dedicated Chrome profile. The optional first argument overrides the default Warsaw URL; the optional second argument overrides port `9222`:

```powershell
scripts\start_chrome_cdp.bat
```

The script:

1. starts Chrome with `--remote-debugging-address=127.0.0.1`;
2. enables `--remote-debugging-port=9222`;
3. uses `%LOCALAPPDATA%\Google\Chrome\User Data\CDP_Profile`;
4. opens the target queue URL;
5. verifies that the CDP endpoint exposes a tab on the configured target host.

If another non-protected process owns the selected port, the script force-stops it, waits until the listener disappears, and prints `Freed port 9222 from previous process`. It then starts the dedicated Chrome instance. A different port can still be selected explicitly:

```powershell
scripts\start_chrome_cdp.bat "https://warszawa.pasport.org.ua/solutions/e-queue" 9223
```

```dotenv
CDP_URL=http://127.0.0.1:9223
```

Complete any Cloudflare challenge in the opened target tab, then run:

```powershell
.venv\Scripts\python.exe -m src.main
```

Do not close the dedicated Chrome process or target tab while monitoring.

## Detection

Only visible DOM evidence is authoritative:

- `NO_SLOTS`: after a service is selected, the occupied banner `"Вибачте, на даний момент всі місця зайняті!"` is visible.
- `FREE_SLOTS_AVAILABLE`: a real service option is selected and that occupied banner does not appear after a short stability wait.
- `UNKNOWN`: evidence is partial, challenged, disconnected, timed out, a transient site backend error, or otherwise inconclusive. The booking form alone (service placeholder / telephone field) is not a free-slot signal.

The final visible state must remain stable before it is accepted. `UNKNOWN` never creates a slot alert and never overwrites the last verified business state.

## Recovery

Missing tabs, closed tabs, CDP disconnects, and Cloudflare challenges create a persistent human-action incident.

- Administrators receive one system alert per unresolved incident.
- Repeated checks do not reload a challenged page.
- Open or refresh the exact target tab and complete the challenge manually.
- A successful visible-state verification clears the incident and resumes normal soft reloads.
- A visible `Too many requests` response starts a persistent cooldown (15 minutes, doubling up to 2 hours on consecutive hits). Cookies and HTTP cache are cleared so the next probe after cooldown (or after `scripts\clean_run.bat`) is a hard navigation, not a cached block page. Scheduled and manual checks respect an active cooldown without reloading. `--check-once` does not restore a persisted cooldown. Administrators receive one latched alert with the cooldown duration and UTC end time.
- PHP/Joomla backend error pages (`DateTimeZone::__construct`, HTTP 500, Ukrainian request-processing errors) are transient `server_error` results. Cookies are cleared and the monitor retries; administrators are not Telegram-alerted (there is no actionable step). Cloudflare challenges still send a latched human-action alert.
- Three consecutive UNKNOWN/server_error results double the next poll interval and send one circuit-breaker diagnostic.

## Telegram commands

Public:

- `/start` or `/subscribe`: enable alerts.
- `/stop` or `/unsubscribe`: disable alerts.
- `/help`: command summary.
- `/status`: city, last verified slot state/time, and booking URL.

Administrator private chats:

- `/status`: public fields plus uptime, latest attempt/error, CDP connection, target-tab presence, and scraper health.
- `/check_now`: await the current check or start one immediately. It never runs concurrently with a scheduled browser interaction and is limited to one new manual check per administrator every 30 seconds.

## Persistence and delivery

SQLite stores:

- subscriptions;
- last attempted and last verified checks;
- active human-action incidents;
- transition events and per-recipient outbox delivery state.

Only transitions into `FREE_SLOTS_AVAILABLE` create business alerts. Successful recipients are not resent; transient failures remain pending for a later cycle; unreachable subscribers are deactivated.

Delivery is durable at-least-once. Telegram does not provide a transactional idempotency key, so a process crash after Telegram accepts a message but before SQLite records success can still produce one duplicate.

## Tests

```powershell
python -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest -q
```
