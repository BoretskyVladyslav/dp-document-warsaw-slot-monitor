from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.async_api import Browser, Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from src.core.config import get_settings
from src.services.scraper import (
    browser_launch_kwargs,
    has_cf_clearance_cookie,
    resolve_repo_path,
    worker_context_kwargs,
)
from src.services.slot_parser import is_cloudflare_challenge, parse_slot_page
from src.services.stealth import STEALTH_INIT_SCRIPT, stealth_async

PROMPT = (
    "Please complete the Cloudflare/Turnstile check in the opened browser window... "
    "Press Enter in terminal when the booking page has loaded."
)


async def _close_browser(browser: object) -> None:
    closer = getattr(browser, "close", None)
    connected = getattr(browser, "is_connected", None)
    if closer is None:
        return
    try:
        if connected is None or connected():
            await closer()  # type: ignore[misc]
    except PlaywrightError as exc:
        print(f"browser_close_failed: {exc}", flush=True)


async def main() -> int:
    settings = get_settings()
    storage_path = resolve_repo_path(settings.storage_state_path)
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    playwright = await async_playwright().start()
    browser = None
    context = None
    page = None
    try:
        launch = browser_launch_kwargs(headless=False)
        try:
            browser = await playwright.chromium.launch(channel="chrome", **launch)
            channel = "chrome"
        except PlaywrightError as exc:
            print(f"system_chrome_unavailable: {exc}", flush=True)
            browser = await playwright.chromium.launch(**launch)
            channel = "chromium"
        context = await browser.new_context(**worker_context_kwargs(settings, storage_path))
        await context.add_init_script(STEALTH_INIT_SCRIPT)
        await stealth_async(context)
        page = await context.new_page()
        await stealth_async(page)
        print(f"storage_state={storage_path}", flush=True)
        print(f"channel={channel}", flush=True)
        print(f"url={settings.target_url}", flush=True)
        try:
            await page.goto(str(settings.target_url), wait_until="domcontentloaded", timeout=60_000)
        except PlaywrightTimeoutError as exc:
            print(f"navigation_timeout: {exc}", flush=True)
        print(PROMPT, flush=True)
        try:
            await asyncio.to_thread(input)
        except EOFError:
            print("cancelled: stdin closed", flush=True)
            return 130
        except KeyboardInterrupt:
            print("cancelled", flush=True)
            return 130

        title = await page.title()
        html = await page.content()
        cookies = await context.cookies()
        challenge = is_cloudflare_challenge(title=title, html=html)
        parsed = parse_slot_page(html=html, title=title, json_payloads=[])
        print(f"page_title={title}", flush=True)
        print(f"challenge_interstitial={challenge}", flush=True)
        print(f"parsed_status={parsed.status.value}", flush=True)
        print(f"cf_clearance={has_cf_clearance_cookie(cookies)}", flush=True)
        if challenge:
            print("booking_page_visible=False", flush=True)
            return 1
        await context.storage_state(path=str(storage_path))
        print(f"storage_saved={storage_path.is_file()}", flush=True)
        print("booking_page_visible=True", flush=True)
        return 0
    finally:
        if page is not None:
            try:
                if not page.is_closed():
                    await page.close()
            except PlaywrightError as exc:
                print(f"page_close_failed: {exc}", flush=True)
        if context is not None:
            try:
                await context.close()
            except PlaywrightError as exc:
                print(f"context_close_failed: {exc}", flush=True)
        if browser is not None:
            await _close_browser(browser)
        try:
            await playwright.stop()
        except PlaywrightError as extra:
            print(f"playwright_stop_failed: {extra}", flush=True)


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130)
