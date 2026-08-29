from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, Page, Playwright
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from src.core.config import Settings
from src.core.exceptions import CloudflareChallengeError, NetworkTimeoutError, ScraperError
from src.core.models import SlotCheckResult, SlotStatus
from src.services.slot_parser import dumps_payload, is_cloudflare_challenge, parse_slot_page
from src.services.stealth import CHROME_CLIENT_HINTS, CLOUDFLARE_CLEARED_JS, STEALTH_INIT_SCRIPT, stealth_async

logger = logging.getLogger(__name__)
_REPO_ROOT = Path(__file__).resolve().parents[2]

_MAX_ATTEMPTS = 2
_CLOUDFLARE_WAIT_MS = 45_000
_TURNSTILE_IFRAME_SELECTOR = (
    'iframe[src*="challenges.cloudflare.com"], '
    'iframe[src*="turnstile"], '
    ".cf-turnstile iframe, "
    "#challenge-stage iframe"
)
_LAUNCH_ARGS: list[str] = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-infobars",
    "--lang=uk-UA,uk,en-US,en",
]


def is_turnstile_frame_url(url: str) -> bool:
    lowered = url.lower()
    return "challenges.cloudflare.com" in lowered or "turnstile" in lowered


def is_target_closed_error(exc: BaseException) -> bool:
    if type(exc).__name__ == "TargetClosedError":
        return True
    message = str(exc).lower()
    return (
        "target closed" in message
        or "has been closed" in message
        or "browser has been closed" in message
    )


def resolve_browser_profile_dir(raw: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = _REPO_ROOT / path
    return path.resolve()


def cookie_name_set(cookies: list[dict[str, Any]]) -> frozenset[str]:
    return frozenset(str(item.get("name", "")) for item in cookies)


def has_cf_clearance_cookie(cookies: list[dict[str, Any]]) -> bool:
    return "cf_clearance" in cookie_name_set(cookies)


def persistent_context_kwargs(settings: Settings, user_data_dir: str, *, headless: bool) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "user_data_dir": user_data_dir,
        "headless": headless,
        "args": list(_LAUNCH_ARGS),
        "ignore_default_args": ["--enable-automation"],
        "user_agent": settings.user_agent,
        "locale": "uk-UA",
        "timezone_id": "Europe/Warsaw",
        "color_scheme": "light",
        "java_script_enabled": True,
        "extra_http_headers": CHROME_CLIENT_HINTS,
    }
    if headless:
        kwargs["viewport"] = {"width": 1920, "height": 1080}
        kwargs["screen"] = {"width": 1920, "height": 1080}
    else:
        kwargs["no_viewport"] = True
    if settings.proxy_url:
        kwargs["proxy"] = {"server": settings.proxy_url}
    return kwargs


async def open_persistent_context(
    settings: Settings,
    *,
    headless: bool | None = None,
) -> tuple[Playwright, BrowserContext, str, Path]:
    playwright = await async_playwright().start()
    profile = resolve_browser_profile_dir(settings.browser_profile_dir)
    profile.mkdir(parents=True, exist_ok=True)
    headed = settings.headless if headless is None else headless
    context_kwargs = persistent_context_kwargs(settings, str(profile), headless=headed)
    try:
        context = await playwright.chromium.launch_persistent_context(
            **context_kwargs,
            channel="chrome",
        )
        channel = "chrome"
    except PlaywrightError as exc:
        logger.warning("system_chrome_unavailable", extra={"error": str(exc)})
        context = await playwright.chromium.launch_persistent_context(**context_kwargs)
        channel = "chromium"
    await context.add_init_script(STEALTH_INIT_SCRIPT)
    await stealth_async(context)
    return playwright, context, channel, profile


async def _human_pause(min_s: float = 0.4, max_s: float = 1.8) -> None:
    await asyncio.sleep(random.uniform(min_s, max_s))


class SlotScraper:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._browser_channel: str = "chromium"

    async def start(self) -> None:
        if self._context is not None and not await self._context_is_open():
            logger.warning(
                "stale_browser_context",
                extra={"city": self._settings.city_name},
            )
            await self._reset()
        if self._context is not None:
            return
        (
            self._playwright,
            self._context,
            self._browser_channel,
            profile,
        ) = await open_persistent_context(self._settings)
        await self._ensure_keepalive_page()
        cookies = await self._context.cookies()
        logger.info(
            "scraper_started",
            extra={
                "city": self._settings.city_name,
                "headless": self._settings.headless,
                "channel": self._browser_channel,
                "profile": str(profile),
                "cookie_count": len(cookies),
                "has_cf_clearance": has_cf_clearance_cookie(cookies),
            },
        )

    async def stop(self) -> None:
        if self._context is not None:
            try:
                await self._context.close()
            except PlaywrightError as exc:
                logger.warning("context_close_failed", extra={"error": str(exc)})
            self._context = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except PlaywrightError as exc:
                logger.warning("playwright_stop_failed", extra={"error": str(exc)})
            self._playwright = None

    async def check_availability(self) -> SlotCheckResult:
        last_unknown: SlotCheckResult | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            page: Page | None = None
            try:
                await self.start()
                assert self._context is not None
                page = await self._context.new_page()
                return await self._inspect_page(page)
            except CloudflareChallengeError as exc:
                logger.warning(
                    "cloudflare_challenge",
                    extra={
                        "city": self._settings.city_name,
                        "attempt": attempt,
                        "error": str(exc),
                    },
                )
                last_unknown = SlotCheckResult(
                    status=SlotStatus.UNKNOWN,
                    checked_at=datetime.now(timezone.utc),
                    error="cloudflare_challenge",
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
                if page is not None:
                    await self._close_page(page)
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

        await _human_pause(0.8, 2.2)
        try:
            await page.wait_for_load_state("networkidle", timeout=min(timeout, 20_000))
        except PlaywrightTimeoutError:
            logger.info("networkidle_skipped", extra={"city": self._settings.city_name})

        await self._wait_out_cloudflare(page, timeout)
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

    async def _wait_out_cloudflare(self, page: Page, nav_timeout: int) -> None:
        if not await self._challenge_visible(page):
            return
        logger.info(
            "cloudflare_wait",
            extra={"city": self._settings.city_name, "timeout_ms": _CLOUDFLARE_WAIT_MS},
        )
        clicked = await self._try_click_turnstile(page)
        if clicked:
            logger.info("turnstile_clicked", extra={"city": self._settings.city_name})
            await _human_pause(0.7, 1.6)
        try:
            await page.wait_for_function(CLOUDFLARE_CLEARED_JS, timeout=_CLOUDFLARE_WAIT_MS)
            await page.wait_for_load_state("domcontentloaded", timeout=nav_timeout)
            try:
                await page.wait_for_load_state("networkidle", timeout=min(nav_timeout, 12_000))
            except PlaywrightTimeoutError:
                logger.info("challenge_networkidle_skipped", extra={"city": self._settings.city_name})
            await _human_pause(0.8, 2.0)
        except PlaywrightTimeoutError as exc:
            raise CloudflareChallengeError(
                "Cloudflare/Turnstile challenge did not complete within the wait window"
            ) from exc

    async def _challenge_visible(self, page: Page) -> bool:
        if page.is_closed():
            return False
        try:
            title = await page.title()
            html = await page.content()
        except PlaywrightError as exc:
            if is_target_closed_error(exc):
                raise
            logger.warning("challenge_detect_failed", extra={"error": str(exc)})
            return False
        html_l = html.lower()
        if is_cloudflare_challenge(title=title, html=html):
            return True
        if "challenges.cloudflare.com" in html_l or "cf-turnstile" in html_l:
            return True
        try:
            stage = page.locator("#challenge-stage, .cf-turnstile")
            return await stage.count() > 0
        except PlaywrightError as exc:
            if is_target_closed_error(exc):
                raise
            return False

    async def _try_click_turnstile(self, page: Page) -> bool:
        if page.is_closed():
            return False
        try:
            iframe = page.locator(_TURNSTILE_IFRAME_SELECTOR).first
            try:
                await iframe.wait_for(state="attached", timeout=8_000)
            except PlaywrightTimeoutError:
                stage = page.locator("#challenge-stage, .cf-turnstile").first
                if await stage.count() == 0:
                    return False
                box = await stage.bounding_box()
                if box is None:
                    return False
                return await self._human_mouse_click(page, box)

            for frame in page.frames:
                if not is_turnstile_frame_url(frame.url):
                    continue
                checkbox = frame.locator(
                    'input[type="checkbox"], [role="checkbox"], .cb-lb, label.cb-lb'
                )
                if await checkbox.count() == 0:
                    continue
                box = await checkbox.first.bounding_box()
                if box is None:
                    continue
                logger.info(
                    "turnstile_checkbox_found",
                    extra={"city": self._settings.city_name, "frame": frame.url[:120]},
                )
                return await self._human_mouse_click(page, box)

            box = await iframe.bounding_box()
            if box is None:
                return False
            checkbox_box = {
                "x": box["x"] + 8.0,
                "y": box["y"] + max(8.0, box["height"] * 0.25),
                "width": min(28.0, max(12.0, box["width"] * 0.2)),
                "height": min(28.0, max(12.0, box["height"] * 0.5)),
            }
            return await self._human_mouse_click(page, checkbox_box)
        except PlaywrightError as exc:
            if is_target_closed_error(exc):
                raise
            logger.warning("turnstile_click_failed", extra={"error": str(exc)})
            return False

    async def _human_mouse_click(self, page: Page, box: dict[str, float]) -> bool:
        x = box["x"] + box["width"] * random.uniform(0.35, 0.65)
        y = box["y"] + box["height"] * random.uniform(0.35, 0.65)
        await page.mouse.move(
            random.uniform(48, 260),
            random.uniform(90, 420),
            steps=random.randint(6, 14),
        )
        await _human_pause(0.12, 0.42)
        await page.mouse.move(x, y, steps=random.randint(12, 28))
        await _human_pause(0.08, 0.28)
        await page.mouse.down()
        await _human_pause(0.04, 0.11)
        await page.mouse.up()
        return True

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

    async def _context_is_open(self) -> bool:
        if self._context is None:
            return False
        try:
            _ = self._context.pages
            await self._context.cookies()
        except PlaywrightError:
            return False
        return True

    async def _ensure_keepalive_page(self) -> None:
        assert self._context is not None
        living = [item for item in self._context.pages if not item.is_closed()]
        if living:
            keeper = living[0]
            for extra in living[1:]:
                await self._close_page(extra)
            try:
                if keeper.url != "about:blank":
                    await keeper.goto("about:blank")
            except PlaywrightError as exc:
                logger.warning("keepalive_reset_failed", extra={"error": str(exc)})
            return
        keeper = await self._context.new_page()
        try:
            await keeper.goto("about:blank")
        except PlaywrightError as extra_exc:
            logger.warning("keepalive_blank_failed", extra={"error": str(extra_exc)})

    async def _close_page(self, page: Page) -> None:
        try:
            if page.is_closed():
                return
            await page.close()
        except PlaywrightError as exc:
            logger.warning("page_close_failed", extra={"error": str(exc)})

    async def _reset(self) -> None:
        await self.stop()
