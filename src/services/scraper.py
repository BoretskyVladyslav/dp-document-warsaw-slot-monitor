from __future__ import annotations

import asyncio
import logging
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.async_api import Browser, BrowserContext, Page, Playwright
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from src.core.config import Settings
from src.core.exceptions import NetworkTimeoutError, ScraperError, SessionExpiredException
from src.core.models import SlotCheckResult, SlotStatus
from src.services.slot_parser import dumps_payload, is_cloudflare_challenge, parse_slot_page
from src.services.stealth import CHROME_CLIENT_HINTS, STEALTH_INIT_SCRIPT, stealth_async

logger = logging.getLogger(__name__)
_REPO_ROOT = Path(__file__).resolve().parents[2]

_MAX_ATTEMPTS = 2
_CONTAINER_HEADLESS_ARGS: list[str] = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
]
_LANG_ARG = "--lang=uk-UA,uk"


def chrome_launch_args(*, headless: bool) -> list[str]:
    if sys.platform == "win32" or not headless:
        return [_LANG_ARG]
    return [*_CONTAINER_HEADLESS_ARGS, _LANG_ARG]


def is_target_closed_error(exc: BaseException) -> bool:
    if type(exc).__name__ == "TargetClosedError":
        return True
    message = str(exc).lower()
    return (
        "target closed" in message
        or "has been closed" in message
        or "browser has been closed" in message
    )


def resolve_repo_path(raw: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = _REPO_ROOT / path
    return path.resolve()


def has_cf_clearance_cookie(cookies: list[dict[str, Any]]) -> bool:
    return any(str(item.get("name", "")) == "cf_clearance" for item in cookies)


def browser_launch_kwargs(*, headless: bool) -> dict[str, Any]:
    return {
        "headless": headless,
        "args": chrome_launch_args(headless=headless),
        "ignore_default_args": ["--enable-automation"],
    }


def worker_context_kwargs(settings: Settings, storage_path: Path) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "user_agent": settings.user_agent,
        "locale": "uk-UA",
        "timezone_id": "Europe/Warsaw",
        "color_scheme": "light",
        "java_script_enabled": True,
        "extra_http_headers": CHROME_CLIENT_HINTS,
    }
    if settings.headless:
        kwargs["viewport"] = {"width": 1920, "height": 1080}
        kwargs["screen"] = {"width": 1920, "height": 1080}
    else:
        kwargs["no_viewport"] = True
    if storage_path.is_file():
        kwargs["storage_state"] = str(storage_path)
    if settings.proxy_url:
        kwargs["proxy"] = {"server": settings.proxy_url}
    return kwargs


async def _human_pause(min_s: float = 0.4, max_s: float = 1.8) -> None:
    await asyncio.sleep(random.uniform(min_s, max_s))


class SlotScraper:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._browser_channel: str = "chromium"

    async def start(self) -> None:
        if self._browser is not None and self._browser.is_connected():
            return
        if self._browser is not None:
            await self._reset()
        self._playwright = await async_playwright().start()
        launch = browser_launch_kwargs(headless=self._settings.headless)
        try:
            self._browser = await self._playwright.chromium.launch(channel="chrome", **launch)
            self._browser_channel = "chrome"
        except PlaywrightError as exc:
            logger.warning("system_chrome_unavailable", extra={"error": str(exc)})
            self._browser = await self._playwright.chromium.launch(**launch)
            self._browser_channel = "chromium"
        storage = resolve_repo_path(self._settings.storage_state_path)
        logger.info(
            "scraper_started",
            extra={
                "city": self._settings.city_name,
                "headless": self._settings.headless,
                "channel": self._browser_channel,
                "storage_state": str(storage),
                "storage_state_exists": storage.is_file(),
            },
        )

    async def stop(self) -> None:
        await self._close_browser()
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except PlaywrightError as exc:
                logger.warning("playwright_stop_failed", extra={"error": str(exc)})
            self._playwright = None

    async def check_availability(self) -> SlotCheckResult:
        last_unknown: SlotCheckResult | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            context: BrowserContext | None = None
            page: Page | None = None
            try:
                await self.start()
                assert self._browser is not None
                storage = resolve_repo_path(self._settings.storage_state_path)
                context = await self._browser.new_context(
                    **worker_context_kwargs(self._settings, storage)
                )
                await context.add_init_script(STEALTH_INIT_SCRIPT)
                await stealth_async(context)
                page = await context.new_page()
                return await self._inspect_page(page)
            except SessionExpiredException as exc:
                logger.warning(
                    "session_expired",
                    extra={"city": self._settings.city_name, "error": str(exc)},
                )
                return SlotCheckResult(
                    status=SlotStatus.UNKNOWN,
                    checked_at=datetime.now(timezone.utc),
                    error="session_expired",
                    details=str(exc),
                )
            except (NetworkTimeoutError, PlaywrightTimeoutError) as exc:
                logger.warning(
                    "scraper_timeout",
                    extra={
                        "city": self._settings.city_name,
                        "attempt": attempt,
                        "error": str(exc),
                    },
                )
                last_unknown = SlotCheckResult(
                    status=SlotStatus.UNKNOWN,
                    checked_at=datetime.now(timezone.utc),
                    error="timeout",
                    details=str(exc),
                )
            except (ScraperError, PlaywrightError, OSError) as exc:
                closed = is_target_closed_error(exc)
                log = logger.warning if closed else logger.exception
                log(
                    "browser_context_closed" if closed else "scraper_failed",
                    extra={
                        "city": self._settings.city_name,
                        "attempt": attempt,
                        "error": str(exc),
                    },
                )
                last_unknown = SlotCheckResult(
                    status=SlotStatus.UNKNOWN,
                    checked_at=datetime.now(timezone.utc),
                    error="scraper_error",
                    details=str(exc),
                )
                await self._reset()
            finally:
                await self._close_page(page)
                await self._close_context(context)
            if attempt < _MAX_ATTEMPTS:
                delay = 3.0 + random.uniform(0.4, 1.6)
                logger.info(
                    "scraper_retry",
                    extra={"city": self._settings.city_name, "attempt": attempt, "delay": round(delay, 2)},
                )
                await asyncio.sleep(delay)
        return last_unknown or SlotCheckResult(
            status=SlotStatus.UNKNOWN,
            checked_at=datetime.now(timezone.utc),
            error="scraper_error",
            details="exhausted scraper attempts",
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
        await stealth_async(page)
        await _human_pause(0.3, 1.2)
        try:
            await page.goto(
                str(self._settings.target_url),
                wait_until="domcontentloaded",
                timeout=timeout,
            )
        except PlaywrightTimeoutError as exc:
            raise NetworkTimeoutError(str(exc)) from exc

        await _human_pause(0.5, 1.2)
        title = await page.title()
        html = await page.content()
        if is_cloudflare_challenge(title=title, html=html):
            raise SessionExpiredException("Cloudflare challenge interstitial; storage_state must be refreshed")

        try:
            await page.wait_for_load_state("networkidle", timeout=min(timeout, 20_000))
        except PlaywrightTimeoutError:
            logger.info("networkidle_skipped", extra={"city": self._settings.city_name})

        await self._select_service(page)
        await _human_pause(0.4, 1.0)
        title = await page.title()
        html = await page.content()
        if is_cloudflare_challenge(title=title, html=html):
            raise SessionExpiredException("Cloudflare challenge interstitial after load")

        return parse_slot_page(
            html=html,
            title=title,
            json_payloads=payloads,
            checked_at=datetime.now(timezone.utc),
        )

    async def _select_service(self, page: Page) -> None:
        label = self._settings.service_option
        if not label or page.is_closed():
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

    async def _close_page(self, page: Page | None) -> None:
        if page is None:
            return
        try:
            if not page.is_closed():
                await page.close()
        except PlaywrightError as exc:
            logger.warning("page_close_failed", extra={"error": str(exc)})

    async def _close_context(self, context: BrowserContext | None) -> None:
        if context is None:
            return
        try:
            await context.close()
        except PlaywrightError as exc:
            logger.warning("context_close_failed", extra={"error": str(exc)})

    async def _close_browser(self) -> None:
        if self._browser is None:
            return
        try:
            if self._browser.is_connected():
                await self._browser.close()
        except PlaywrightError as exc:
            logger.warning("browser_close_failed", extra={"error": str(exc)})
        self._browser = None

    async def _reset(self) -> None:
        await self.stop()
