from __future__ import annotations

import unittest
from unittest.mock import patch

from playwright.async_api import Error as PlaywrightError

from src.core.config import Settings
from src.core.exceptions import (
    CdpUnavailableError,
    CloudflareChallengeError,
    RateLimitException,
    TargetTabClosedError,
    TargetTabMissingError,
)
from src.core.models import ScraperFailureCode, ScraperHealthStatus, SlotStatus
from src.services.scraper import (
    SlotScraper,
    _QUEUE_UI_SELECTOR,
    _QUEUE_UI_WAIT_MS,
    cdp_tab_matches,
    collect_dom_evidence,
    is_execution_context_destroyed,
    is_target_closed_error,
    normalize_cdp_url,
    page_matches_challenge_url,
    page_matches_target_url,
)

TARGET_URL = "https://warszawa.pasport.org.ua/solutions/e-queue"


def settings(*, cdp_url: str | None = "http://127.0.0.1:9222") -> Settings:
    return Settings(
        bot_token="1234567890:TESTTOKENVALUE",
        target_url=TARGET_URL,
        cdp_url=cdp_url,
        _env_file=None,
    )


def free_evidence() -> dict[str, object]:
    return {
        "title": "Електронна черга",
        "url": TARGET_URL,
        "visibleText": "Послуга * - Обрати - Телефон",
        "occupiedBannerVisible": False,
        "serviceSelectVisible": True,
        "selectPlaceholderVisible": True,
        "telInputVisible": True,
        "challengeVisible": False,
    }


def challenge_evidence() -> dict[str, object]:
    return {
        "title": "Just a moment...",
        "url": TARGET_URL,
        "visibleText": "Checking your browser",
        "occupiedBannerVisible": False,
        "serviceSelectVisible": False,
        "selectPlaceholderVisible": False,
        "telInputVisible": False,
        "challengeVisible": True,
    }


class TargetClosedError(Exception):
    pass


class ScraperHelperTests(unittest.TestCase):
    def test_target_closed_by_type_and_message(self) -> None:
        self.assertTrue(is_target_closed_error(TargetClosedError("closed")))
        self.assertTrue(
            is_target_closed_error(
                RuntimeError("Target page, context or browser has been closed")
            )
        )
        self.assertFalse(is_target_closed_error(RuntimeError("connection reset")))
        self.assertTrue(
            is_execution_context_destroyed(
                PlaywrightError(
                    "Execution context was destroyed, most likely because of a navigation"
                )
            )
        )
        self.assertFalse(is_execution_context_destroyed(RuntimeError("connection reset")))

    def test_normalize_cdp_url(self) -> None:
        self.assertIsNone(normalize_cdp_url(None))
        self.assertIsNone(normalize_cdp_url("  "))
        self.assertEqual(
            normalize_cdp_url(" http://127.0.0.1:9222 "),
            "http://127.0.0.1:9222",
        )

    def test_exact_target_url_matching(self) -> None:
        self.assertTrue(
            page_matches_target_url(
                f"{TARGET_URL}?__cf_chl_rt_tk=xyz",
                TARGET_URL,
            )
        )
        self.assertTrue(page_matches_target_url(f"{TARGET_URL}/", TARGET_URL))
        self.assertTrue(
            page_matches_target_url(
                f"{TARGET_URL}/?__cf_chl_rt_tk=xyz#challenge",
                TARGET_URL,
            )
        )
        self.assertTrue(
            page_matches_target_url(
                "https://warszawa.pasport.org.ua:443/solutions/e-queue",
                TARGET_URL,
            )
        )
        self.assertFalse(
            page_matches_target_url(
                "https://warszawa.pasport.org.ua/other",
                TARGET_URL,
            )
        )
        self.assertFalse(
            page_matches_target_url(
                f"https://example.test/?next={TARGET_URL}",
                TARGET_URL,
            )
        )
        self.assertTrue(cdp_tab_matches(TARGET_URL, TARGET_URL))
        self.assertFalse(
            cdp_tab_matches("https://krakow.pasport.org.ua/solutions/e-queue", TARGET_URL)
        )

    def test_challenge_url_must_use_target_host(self) -> None:
        self.assertTrue(
            page_matches_challenge_url(
                "https://warszawa.pasport.org.ua/cdn-cgi/challenge-platform/x",
                TARGET_URL,
            )
        )
        self.assertFalse(
            page_matches_challenge_url(
                "https://example.test/cdn-cgi/challenge-platform/x",
                TARGET_URL,
            )
        )

class FakePage:
    def __init__(
        self,
        url: str,
        evidence: dict[str, object] | None = None,
    ) -> None:
        self.url = url
        self.evidence = evidence or free_evidence()
        self.closed = False
        self.reload_calls = 0
        self.reload_timeout: int | None = None
        self.reload_error: BaseException | None = None
        self.evidence_sequence: list[dict[str, object]] = []
        self.evaluate_errors: list[BaseException] = []
        self.wait_for_selector_calls = 0
        self.wait_for_selector_timeout: int | None = None
        self.wait_for_selector_selector: str | None = None
        self.wait_for_selector_error: BaseException | None = None
        self.page_title = ""
        self.html = ""
        self.context: FakeContext | None = None

    def is_closed(self) -> bool:
        return self.closed

    async def title(self) -> str:
        return self.page_title or str(self.evidence.get("title", ""))

    async def content(self) -> str:
        return self.html or str(self.evidence.get("visibleText", ""))

    async def reload(self, *, wait_until: str, timeout: int) -> None:
        self.reload_calls += 1
        self.reload_timeout = timeout
        if self.reload_error is not None:
            raise self.reload_error
        if wait_until != "domcontentloaded":
            raise AssertionError("unexpected load state")

    async def wait_for_selector(
        self,
        selector: str,
        *,
        timeout: int,
        state: str = "visible",
    ) -> None:
        del state
        self.wait_for_selector_calls += 1
        self.wait_for_selector_timeout = timeout
        self.wait_for_selector_selector = selector
        if self.wait_for_selector_error is not None:
            raise self.wait_for_selector_error

    async def evaluate(self, script: str) -> dict[str, object]:
        if not script:
            raise AssertionError("DOM evidence script is required")
        if self.evaluate_errors:
            raise self.evaluate_errors.pop(0)
        if self.evidence_sequence:
            self.evidence = self.evidence_sequence.pop(0)
        return self.evidence


class FakeContext:
    def __init__(self, pages: list[FakePage] | None = None) -> None:
        self.pages = list(pages or [])
        self.new_page_calls = 0
        self.clear_cookies_calls = 0
        for page in self.pages:
            page.context = self

    async def new_page(self) -> FakePage:
        self.new_page_calls += 1
        raise AssertionError("strict CDP scraper must not create pages")

    async def clear_cookies(self) -> None:
        self.clear_cookies_calls += 1


class FakeBrowser:
    def __init__(self, contexts: list[FakeContext] | None = None) -> None:
        self.contexts = list(contexts or [])
        self.connected = True
        self.close_calls = 0

    def is_connected(self) -> bool:
        return self.connected

    async def close(self) -> None:
        self.close_calls += 1
        self.connected = False


class DisconnectingBrowser(FakeBrowser):
    def __init__(self, contexts: list[FakeContext]) -> None:
        super().__init__(contexts)
        self.connection_checks = 0

    def is_connected(self) -> bool:
        self.connection_checks += 1
        return self.connection_checks == 1


class FakePlaywright:
    def __init__(self) -> None:
        self.stop_calls = 0

    async def stop(self) -> None:
        self.stop_calls += 1


class FailingChromium:
    def __init__(self) -> None:
        self.connect_calls = 0
        self.launch_calls = 0

    async def connect_over_cdp(self, url: str, *, timeout: int) -> FakeBrowser:
        self.connect_calls += 1
        raise PlaywrightError(f"cannot connect to {url} in {timeout}")

    async def launch(self, **_: object) -> FakeBrowser:
        self.launch_calls += 1
        raise AssertionError("strict CDP scraper must not launch a browser")


class FailingPlaywright(FakePlaywright):
    def __init__(self) -> None:
        super().__init__()
        self.chromium = FailingChromium()


class AsyncPlaywrightStarter:
    def __init__(self, playwright: FailingPlaywright) -> None:
        self.playwright = playwright

    async def start(self) -> FailingPlaywright:
        return self.playwright


class StrictCdpLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._stability_patch = patch(
            "src.services.scraper._DOM_STABILITY_SECONDS",
            0,
        )
        self._poll_patch = patch("src.services.scraper._DOM_POLL_SECONDS", 0)
        self._retry_patch = patch("src.services.scraper._CONTEXT_RETRY_SECONDS", 0)
        self._queue_wait_patch = patch("src.services.scraper._QUEUE_UI_WAIT_MS", 0)
        self._cf_hold_patch = patch(
            "src.services.scraper._CF_INTERSTITIAL_HOLD_SECONDS",
            0,
        )
        self._stability_patch.start()
        self._poll_patch.start()
        self._retry_patch.start()
        self._queue_wait_patch.start()
        self._cf_hold_patch.start()

    async def asyncTearDown(self) -> None:
        self._cf_hold_patch.stop()
        self._queue_wait_patch.stop()
        self._retry_patch.stop()
        self._poll_patch.stop()
        self._stability_patch.stop()

    async def test_collect_dom_evidence_maps_script_result(self) -> None:
        page = FakePage(TARGET_URL)

        result = await collect_dom_evidence(page)  # type: ignore[arg-type]

        self.assertTrue(result.service_select_visible)
        self.assertTrue(result.tel_input_visible)
        self.assertEqual(result.url, TARGET_URL)

    async def test_missing_cdp_url_is_typed_and_latched(self) -> None:
        scraper = SlotScraper(settings(cdp_url=None))

        with self.assertRaises(CdpUnavailableError):
            await scraper.check_availability()

        health = await scraper.get_health_snapshot()
        self.assertEqual(health.status, ScraperHealthStatus.NEEDS_HUMAN)
        self.assertEqual(health.failure_code, ScraperFailureCode.CDP_UNAVAILABLE)

    async def test_connection_failure_never_launches_fallback_browser(self) -> None:
        playwright = FailingPlaywright()
        scraper = SlotScraper(settings())
        with patch(
            "src.services.scraper.async_playwright",
            return_value=AsyncPlaywrightStarter(playwright),
        ):
            with self.assertRaises(CdpUnavailableError):
                await scraper.check_availability()

        self.assertEqual(playwright.chromium.connect_calls, 1)
        self.assertEqual(playwright.chromium.launch_calls, 0)
        self.assertEqual(playwright.stop_calls, 1)

    async def test_missing_exact_target_tab_is_typed_without_spawning(self) -> None:
        context = FakeContext([FakePage("https://warszawa.pasport.org.ua/other")])
        scraper = SlotScraper(settings())
        scraper._browser = FakeBrowser([context])  # type: ignore[assignment]

        with self.assertRaises(TargetTabMissingError):
            await scraper.check_availability()

        self.assertEqual(context.new_page_calls, 0)
        health = await scraper.get_health_snapshot()
        self.assertEqual(health.failure_code, ScraperFailureCode.TARGET_TAB_MISSING)

    async def test_disconnected_cdp_fails_before_page_action(self) -> None:
        page = FakePage(TARGET_URL)
        context = FakeContext([page])
        scraper = SlotScraper(settings())
        scraper._browser = DisconnectingBrowser([context])  # type: ignore[assignment]

        with self.assertRaises(CdpUnavailableError):
            await scraper.check_availability()

        self.assertEqual(page.reload_calls, 0)

    async def test_soft_reload_uses_existing_tab_and_fifteen_second_timeout(self) -> None:
        page = FakePage(TARGET_URL)
        context = FakeContext([page])
        scraper = SlotScraper(settings())
        scraper._browser = FakeBrowser([context])  # type: ignore[assignment]

        result = await scraper.check_availability()

        self.assertEqual(result.status, SlotStatus.FREE_SLOTS_AVAILABLE)
        self.assertEqual(page.reload_calls, 1)
        self.assertEqual(page.reload_timeout, 15_000)
        self.assertEqual(page.wait_for_selector_calls, 1)
        self.assertEqual(page.wait_for_selector_selector, _QUEUE_UI_SELECTOR)
        self.assertEqual(context.new_page_calls, 0)
        self.assertEqual(_QUEUE_UI_WAIT_MS, 20_000)

    async def test_challenge_latches_and_does_not_reload_again(self) -> None:
        page = FakePage(TARGET_URL, challenge_evidence())
        scraper = SlotScraper(settings())
        scraper._browser = FakeBrowser([FakeContext([page])])  # type: ignore[assignment]

        with self.assertRaises(CloudflareChallengeError):
            await scraper.check_availability()
        with self.assertRaises(CloudflareChallengeError):
            await scraper.check_availability()

        self.assertEqual(page.reload_calls, 1)
        health = await scraper.get_health_snapshot()
        self.assertEqual(health.status, ScraperHealthStatus.NEEDS_HUMAN)
        self.assertEqual(
            health.failure_code,
            ScraperFailureCode.CLOUDFLARE_CHALLENGE,
        )

    async def test_transient_cloudflare_interstitial_does_not_latch(self) -> None:
        page = FakePage(TARGET_URL)
        page.evidence_sequence = [
            challenge_evidence(),
            free_evidence(),
            free_evidence(),
        ]
        scraper = SlotScraper(settings())
        scraper._browser = FakeBrowser([FakeContext([page])])  # type: ignore[assignment]

        with patch("src.services.scraper._CF_INTERSTITIAL_HOLD_SECONDS", 60):
            result = await scraper.check_availability()

        self.assertEqual(result.status, SlotStatus.FREE_SLOTS_AVAILABLE)
        self.assertEqual(page.reload_calls, 1)
        health = await scraper.get_health_snapshot()
        self.assertEqual(health.status, ScraperHealthStatus.READY)
        self.assertIsNone(health.failure_code)

    async def test_destroyed_execution_context_retries_dom_evaluate(self) -> None:
        page = FakePage(TARGET_URL)
        page.evaluate_errors = [
            PlaywrightError(
                "Execution context was destroyed, most likely because of a navigation"
            )
        ]

        result = await collect_dom_evidence(page)  # type: ignore[arg-type]

        self.assertTrue(result.tel_input_visible)
        self.assertEqual(result.url, TARGET_URL)

    async def test_queue_ui_wait_retries_after_context_destruction(self) -> None:
        page = FakePage(TARGET_URL)
        page.wait_for_selector_error = PlaywrightError(
            "Execution context was destroyed, most likely because of a navigation"
        )
        scraper = SlotScraper(settings())
        scraper._browser = FakeBrowser([FakeContext([page])])  # type: ignore[assignment]

        result = await scraper.check_availability()

        self.assertEqual(result.status, SlotStatus.FREE_SLOTS_AVAILABLE)
        self.assertEqual(page.wait_for_selector_calls, 2)
        self.assertEqual(page.reload_calls, 1)

    async def test_rate_limit_message_raises_typed_failure(self) -> None:
        raw = free_evidence()
        raw["visibleText"] = "Too many requests, please try again later"
        page = FakePage(TARGET_URL, raw)
        scraper = SlotScraper(settings())
        scraper._browser = FakeBrowser([FakeContext([page])])  # type: ignore[assignment]

        with self.assertRaises(RateLimitException):
            await scraper.check_availability()

        self.assertEqual(page.reload_calls, 1)
        self.assertEqual(page.wait_for_selector_calls, 0)
        health = await scraper.get_health_snapshot()
        self.assertEqual(health.status, ScraperHealthStatus.DEGRADED)
        self.assertEqual(health.failure_code, ScraperFailureCode.RATE_LIMITED)

    async def test_backend_error_page_is_degraded_not_human_action(self) -> None:
        raw = free_evidence()
        raw.update(
            {
                "visibleText": (
                    "DateTimeZone::__construct(): Unknown or bad timezone ()"
                ),
                "serviceSelectVisible": False,
                "selectPlaceholderVisible": False,
                "telInputVisible": False,
            }
        )
        page = FakePage(TARGET_URL, raw)
        scraper = SlotScraper(settings())
        scraper._browser = FakeBrowser([FakeContext([page])])  # type: ignore[assignment]

        result = await scraper.check_availability()
        context = page.context
        assert context is not None

        self.assertEqual(result.status, SlotStatus.UNKNOWN)
        self.assertEqual(result.failure_code, ScraperFailureCode.SERVER_ERROR)
        self.assertEqual(result.details, "site_backend_error")
        self.assertEqual(page.wait_for_selector_calls, 0)
        self.assertEqual(context.clear_cookies_calls, 1)
        health = await scraper.get_health_snapshot()
        self.assertEqual(health.status, ScraperHealthStatus.DEGRADED)
        self.assertEqual(health.failure_code, ScraperFailureCode.SERVER_ERROR)

    async def test_php_crash_in_page_source_skips_queue_ui_wait(self) -> None:
        raw = free_evidence()
        raw.update(
            {
                "title": "0 - ",
                "visibleText": "",
                "serviceSelectVisible": False,
                "selectPlaceholderVisible": False,
                "telInputVisible": False,
            }
        )
        page = FakePage(TARGET_URL, raw)
        page.html = (
            "<pre>DateTimeZone::__construct(): Unknown or bad timezone ()</pre>"
        )
        context = FakeContext([page])
        scraper = SlotScraper(settings())
        scraper._browser = FakeBrowser([context])  # type: ignore[assignment]

        result = await scraper.check_availability()

        self.assertEqual(result.status, SlotStatus.UNKNOWN)
        self.assertEqual(result.failure_code, ScraperFailureCode.SERVER_ERROR)
        self.assertEqual(result.details, "site_backend_error")
        self.assertEqual(page.wait_for_selector_calls, 0)
        self.assertEqual(context.clear_cookies_calls, 1)

    async def test_ukrainian_error_title_skips_queue_ui_wait(self) -> None:
        raw = free_evidence()
        raw.update(
            {
                "title": "Електронна черга",
                "visibleText": "",
                "serviceSelectVisible": False,
                "selectPlaceholderVisible": False,
                "telInputVisible": False,
            }
        )
        page = FakePage(TARGET_URL, raw)
        page.page_title = "Виникла помилка"
        context = FakeContext([page])
        scraper = SlotScraper(settings())
        scraper._browser = FakeBrowser([context])  # type: ignore[assignment]

        result = await scraper.check_availability()

        self.assertEqual(result.status, SlotStatus.UNKNOWN)
        self.assertEqual(result.failure_code, ScraperFailureCode.SERVER_ERROR)
        self.assertEqual(page.wait_for_selector_calls, 0)
        self.assertEqual(context.clear_cookies_calls, 1)

    async def test_human_refresh_clears_latch_without_worker_reload(self) -> None:
        page = FakePage(TARGET_URL, challenge_evidence())
        scraper = SlotScraper(settings())
        scraper._browser = FakeBrowser([FakeContext([page])])  # type: ignore[assignment]

        with self.assertRaises(CloudflareChallengeError):
            await scraper.check_availability()
        page.evidence = free_evidence()
        result = await scraper.check_availability()

        self.assertEqual(result.status, SlotStatus.FREE_SLOTS_AVAILABLE)
        self.assertEqual(page.reload_calls, 1)
        health = await scraper.get_health_snapshot()
        self.assertEqual(health.status, ScraperHealthStatus.READY)
        self.assertIsNone(health.failure_code)

    async def test_inconclusive_recovery_probe_keeps_needs_human_health(self) -> None:
        page = FakePage(TARGET_URL, challenge_evidence())
        scraper = SlotScraper(settings())
        scraper._browser = FakeBrowser([FakeContext([page])])  # type: ignore[assignment]
        with self.assertRaises(CloudflareChallengeError):
            await scraper.check_availability()

        incomplete = free_evidence()
        incomplete["telInputVisible"] = False
        page.evidence = incomplete
        result = await scraper.check_availability()

        self.assertEqual(result.status, SlotStatus.UNKNOWN)
        health = await scraper.get_health_snapshot()
        self.assertEqual(health.status, ScraperHealthStatus.NEEDS_HUMAN)
        self.assertTrue(health.target_tab_present)
        self.assertEqual(
            health.failure_code,
            ScraperFailureCode.CLOUDFLARE_CHALLENGE,
        )

    async def test_closed_tab_during_reload_is_typed_and_latched(self) -> None:
        page = FakePage(TARGET_URL)
        page.reload_error = PlaywrightError(
            "Target page, context or browser has been closed"
        )
        scraper = SlotScraper(settings())
        scraper._browser = FakeBrowser([FakeContext([page])])  # type: ignore[assignment]

        with self.assertRaises(TargetTabClosedError):
            await scraper.check_availability()

        health = await scraper.get_health_snapshot()
        self.assertEqual(health.failure_code, ScraperFailureCode.TARGET_TAB_CLOSED)

    async def test_stop_detaches_without_closing_external_chrome(self) -> None:
        browser = FakeBrowser()
        playwright = FakePlaywright()
        scraper = SlotScraper(settings())
        scraper._browser = browser  # type: ignore[assignment]
        scraper._playwright = playwright  # type: ignore[assignment]

        await scraper.stop()

        self.assertEqual(browser.close_calls, 0)
        self.assertEqual(playwright.stop_calls, 1)
        health = await scraper.get_health_snapshot()
        self.assertEqual(health.status, ScraperHealthStatus.STOPPED)

    async def test_inconclusive_dom_returns_unknown_without_latching(self) -> None:
        raw = free_evidence()
        raw["telInputVisible"] = False
        page = FakePage(TARGET_URL, raw)
        scraper = SlotScraper(settings())
        scraper._browser = FakeBrowser([FakeContext([page])])  # type: ignore[assignment]

        with patch("src.services.scraper._DOM_SIGNAL_TIMEOUT_MS", 0):
            result = await scraper.check_availability()

        self.assertEqual(result.status, SlotStatus.UNKNOWN)
        self.assertEqual(result.failure_code, ScraperFailureCode.INCONCLUSIVE_PAGE)
        health = await scraper.get_health_snapshot()
        self.assertEqual(health.status, ScraperHealthStatus.DEGRADED)

    async def test_transient_free_form_does_not_beat_occupied_banner(self) -> None:
        occupied = free_evidence()
        occupied.update(
            {
                "visibleText": (
                    "Наразі всі місця зайняті. "
                    "Будь ласка, спробуйте в інший час або день."
                ),
                "occupiedBannerVisible": True,
                "serviceSelectVisible": False,
                "selectPlaceholderVisible": False,
                "telInputVisible": False,
            }
        )
        page = FakePage(TARGET_URL)
        page.evidence_sequence = [free_evidence(), occupied, occupied]
        scraper = SlotScraper(settings())
        scraper._browser = FakeBrowser([FakeContext([page])])  # type: ignore[assignment]

        result = await scraper.check_availability()

        self.assertEqual(result.status, SlotStatus.NO_SLOTS)


if __name__ == "__main__":
    unittest.main()
