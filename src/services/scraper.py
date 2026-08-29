from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from typing import Any

from playwright.async_api import Browser, BrowserContext, Page, Playwright
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from src.core.config import Settings
from src.core.exceptions import CloudflareChallengeError, NetworkTimeoutError, ScraperError
from src.core.models import SlotCheckResult, SlotStatus
from src.services.slot_parser import dumps_payload, is_cloudflare_challenge, parse_slot_page

logger = logging.getLogger(__name__)


async def _human_pause(min_s: float = 0.4, max_s: float = 1.8) -> None:
    await asyncio.sleep(random.uniform(min_s, max_s))


class SlotScraper:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    async def start(self) -> None:
        if self._browser is not None:
            return
        self._playwright = await async_playwright().start()
        launch_kwargs: dict[str, Any] = {
            "headless": self._settings.headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
            "ignore_default_args": ["--enable-automation"],
        }
        if self._settings.proxy_url:
            launch_kwargs["proxy"] = {"server": self._settings.proxy_url}
        self._browser = await self._playwright.chromium.launch(**launch_kwargs)
        self._context = await self._browser.new_context(
            user_agent=self._settings.user_agent,
            locale="uk-UA",
            timezone_id="Europe/Warsaw",
            viewport={"width": 1366, "height": 768},
            java_script_enabled=True,
        )
        await self._context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        logger.info(
            "scraper_started",
            extra={"city": self._settings.city_name, "headless": self._settings.headless},
        )

    async def stop(self) -> None:
        if self._context is not None:
            await self._context.close()
            self._context = None
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def check_availability(self) -> SlotCheckResult:
        try:
            await self.start()
            assert self._context is not None
            page = await self._context.new_page()
            try:
                return await self._inspect_page(page)
            finally:
                await page.close()
        except CloudflareChallengeError as exc:
            logger.warning(
                "cloudflare_challenge",
                extra={"city": self._settings.city_name, "error": str(exc)},
            )
            return SlotCheckResult(
                status=SlotStatus.UNKNOWN,
                checked_at=datetime.now(timezone.utc),
                error="cloudflare_challenge",
                details=str(exc),
            )
        except (NetworkTimeoutError, PlaywrightTimeoutError) as exc:
            logger.warning(
                "scraper_timeout",
                extra={"city": self._settings.city_name, "error": str(exc)},
            )
            return SlotCheckResult(
                status=SlotStatus.UNKNOWN,
                checked_at=datetime.now(timezone.utc),
                error="timeout",
                details=str(exc),
            )
        except (ScraperError, PlaywrightError, OSError) as exc:
            logger.exception(
                "scraper_failed",
                extra={"city": self._settings.city_name, "error": str(exc)},
            )
            await self._reset()
            return SlotCheckResult(
                status=SlotStatus.UNKNOWN,
                checked_at=datetime.now(timezone.utc),
                error="scraper_error",
                details=str(exc),
            )

    async def _inspect_page(self, page: Page) -> SlotCheckResult:
        payloads: list[Any] = []

        async def _capture_response(response: Any) -> None:
            content_type = (response.headers.get("content-type") or "").lower()
            if "json" not in content_type:
                return
            try:
                body = await response.text()
            except (PlaywrightError, OSError, UnicodeDecodeError):
                return
            parsed = dumps_payload(body)
            if parsed is not None:
                payloads.append(parsed)

        page.on("response", _capture_response)
        timeout = self._settings.playwright_timeout_ms
        await _human_pause(0.3, 1.2)
        try:
            await page.goto(
                str(self._settings.target_url),
                wait_until="domcontentloaded",
                timeout=timeout,
            )
        except PlaywrightTimeoutError as exc:
            raise NetworkTimeoutError(str(exc)) from exc

        await _human_pause(0.8, 2.2)
        try:
            await page.wait_for_load_state("networkidle", timeout=timeout)
        except PlaywrightTimeoutError:
            logger.info("networkidle_skipped", extra={"city": self._settings.city_name})

        await self._select_service(page)
        await _human_pause(0.5, 1.5)

        title = await page.title()
        html = await page.content()
        if is_cloudflare_challenge(title=title, html=html):
            raise CloudflareChallengeError("Cloudflare challenge interstitial on target URL")

        return parse_slot_page(
            html=html,
            title=title,
            json_payloads=payloads,
            checked_at=datetime.now(timezone.utc),
        )

    async def _select_service(self, page: Page) -> None:
        label = self._settings.service_option
        if not label:
            return
        try:
            select = page.locator("select").first
            if await select.count() > 0:
                await select.select_option(label=label)
                await _human_pause()
                return
            option = page.get_by_role("option", name=label)
            if await option.count() > 0:
                await option.first.click()
                await _human_pause()
                return
            text_match = page.get_by_text(label, exact=False).first
            if await text_match.count() > 0:
                await text_match.click()
                await _human_pause()
        except PlaywrightError as exc:
            logger.warning(
                "service_option_not_selected",
                extra={"label": label, "error": str(exc)},
            )

    async def _reset(self) -> None:
        await self.stop()
