from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from src.core.config import get_settings
from src.services.scraper import (
    has_cf_clearance_cookie,
    open_persistent_context,
    resolve_browser_profile_dir,
)
from src.services.slot_parser import is_cloudflare_challenge, parse_slot_page

PROMPT = (
    "Please complete the Cloudflare/Turnstile check in the opened browser window... "
    "Press Enter in terminal when the booking page has loaded."
)


async def main() -> int:
    settings = get_settings()
    profile = resolve_browser_profile_dir(settings.browser_profile_dir)
    playwright, context, channel, profile = await open_persistent_context(
        settings,
        headless=False,
    )
    page = context.pages[0] if context.pages else await context.new_page()
    try:
        print(f"profile={profile}", flush=True)
        print(f"channel={channel}", flush=True)
        print(f"url={settings.target_url}", flush=True)
        try:
            await page.goto(str(settings.target_url), wait_until="domcontentloaded", timeout=60_000)
        except PlaywrightTimeoutError as exc:
            print(f"navigation_timeout: {exc}", flush=True)
        print(PROMPT, flush=True)
        await asyncio.to_thread(input)

        title = await page.title()
        html = await page.content()
        cookies = await context.cookies()
        challenge = is_cloudflare_challenge(title=title, html=html)
        parsed = parse_slot_page(html=html, title=title, json_payloads=[])
        cleared = has_cf_clearance_cookie(cookies)
        print(f"page_title={title}", flush=True)
        print(f"challenge_interstitial={challenge}", flush=True)
        print(f"parsed_status={parsed.status.value}", flush=True)
        print(f"cookie_count={len(cookies)}", flush=True)
        print(f"cf_clearance={cleared}", flush=True)
        print(f"profile_exists={profile.is_dir()}", flush=True)
        if challenge:
            print("booking_page_visible=False", flush=True)
            return 1
        print("booking_page_visible=True", flush=True)
        if not cleared:
            print("warning: cf_clearance cookie was not present after close-ready check", flush=True)
            return 1
        return 0
    finally:
        try:
            await context.close()
        except PlaywrightError as exc:
            print(f"context_close_failed: {exc}", flush=True)
        try:
            await playwright.stop()
        except PlaywrightError as exc:
            print(f"playwright_stop_failed: {exc}", flush=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
