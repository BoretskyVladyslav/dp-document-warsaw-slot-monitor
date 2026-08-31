from __future__ import annotations

import unittest
from unittest.mock import patch

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

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
    _CF_MANAGED_CHALLENGE_HOLD_MS,
    _QUEUE_UI_NETWORKIDLE_MS,
    _QUEUE_UI_SELECTOR,
    _QUEUE_UI_WAIT_MS,
    _SERVICE_SELECT_TIMEOUT_MS,
    _SERVICE_VALIDATE_RESPONSE_TIMEOUT_MS,
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
        "serviceOptionSelected": True,
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
        "serviceOptionSelected": False,
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

class FakeRequest:
    def __init__(
        self,
        url: str,
        resource_type: str = "xhr",
        method: str = "POST",
    ) -> None:
        self.url = url
        self.resource_type = resource_type
        self.method = method


class FakeResponse:
    def __init__(self, url: str, status: int, request: FakeRequest) -> None:
        self.url = url
        self.status = status
        self.request = request

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class FakeExpectResponse:
    def __init__(self, page: FakePage) -> None:
        self._page = page
        self._response: FakeResponse | None = None

    async def __aenter__(self) -> FakeExpectResponse:
        self._page._active_expect = self
        return self

    async def __aexit__(self, *args: object) -> None:
        self._page._active_expect = None

    @property
    def value(self) -> object:
        return self._resolve()

    async def _resolve(self) -> FakeResponse:
        if self._page.expect_response_error is not None:
            raise self._page.expect_response_error
        if self._response is None:
            self._response = self._page.build_validate_response()
        return self._response


class FakeLocator:
    def __init__(self, page: FakePage, selector: str = "select") -> None:
        self._page = page
        self._selector = selector
        self._nth: int | None = None

    @property
    def first(self) -> FakeLocator:
        return self

    def locator(self, selector: str) -> FakeLocator:
        self._page.locator_selectors.append(selector)
        return FakeLocator(self._page, selector)

    def nth(self, index: int) -> FakeLocator:
        child = FakeLocator(self._page, self._selector)
        child._nth = index
        self._page.option_nth_calls.append(index)
        return child

    async def wait_for(self, *, state: str, timeout: int) -> None:
        self._page.option_wait_calls.append({"state": state, "timeout": timeout})
        if self._page.option_wait_error is not None:
            raise self._page.option_wait_error

    async def inner_text(self) -> str:
        return self._page.selected_option_text

    async def select_option(
        self,
        *,
        index: int,
        timeout: int | None = None,
    ) -> None:
        self._page.select_option_calls.append(index)
        self._page.select_option_timeout = timeout
        if self._page.select_option_error is not None:
            raise self._page.select_option_error
        if index == 1 and self._page._active_expect is not None:
            if (
                not self._page.skip_validate_request
                and not self._page.stale_request_on_listen
            ):
                self._page.emit_validate_request()
            self._page._active_expect._response = self._page.build_validate_response()


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
        self.goto_calls = 0
        self.goto_url: str | None = None
        self.goto_timeout: int | None = None
        self.goto_error: BaseException | None = None
        self.evidence_sequence: list[dict[str, object]] = []
        self.evaluate_errors: list[BaseException] = []
        self.wait_for_selector_calls = 0
        self.wait_for_selector_timeout: int | None = None
        self.wait_for_selector_selector: str | None = None
        self.wait_for_selector_error: BaseException | None = None
        self.wait_for_selector_errors: list[BaseException] = []
        self.wait_for_load_state_calls = 0
        self.wait_for_load_state_state: str | None = None
        self.wait_for_load_state_timeout: int | None = None
        self.wait_for_load_state_error: BaseException | None = None
        self.wait_for_function_calls = 0
        self.wait_for_function_timeout: int | None = None
        self.wait_for_function_succeeds = False
        self.locator_selectors: list[str] = []
        self.select_option_calls: list[int] = []
        self.select_option_timeout: int | None = None
        self.select_option_error: BaseException | None = None
        self.option_nth_calls: list[int] = []
        self.option_wait_calls: list[dict[str, object]] = []
        self.option_wait_error: BaseException | None = None
        self.selected_option_text = "Закордонний паспорт та (або) ID-картка"
        self.expect_response_calls = 0
        self.expect_response_timeout: int | None = None
        self.expect_response_status = 200
        self.expect_response_error: BaseException | None = None
        self.skip_validate_request = False
        self.stale_request_on_listen = False
        self._request_listeners: list[object] = []
        self._last_validate_request: FakeRequest | None = None
        self._active_expect: FakeExpectResponse | None = None
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

    async def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        self.goto_calls += 1
        self.goto_url = url
        self.goto_timeout = timeout
        if wait_until != "domcontentloaded":
            raise AssertionError("unexpected load state")
        if self.goto_error is not None:
            raise self.goto_error

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
        if self.wait_for_selector_errors:
            raise self.wait_for_selector_errors.pop(0)
        if self.wait_for_selector_error is not None:
            raise self.wait_for_selector_error

    async def wait_for_load_state(self, state: str, *, timeout: int) -> None:
        self.wait_for_load_state_calls += 1
        self.wait_for_load_state_state = state
        self.wait_for_load_state_timeout = timeout
        if self.wait_for_load_state_error is not None:
            raise self.wait_for_load_state_error

    async def wait_for_function(self, expression: str, *, timeout: int) -> None:
        del expression
        self.wait_for_function_calls += 1
        self.wait_for_function_timeout = timeout
        if self.wait_for_function_succeeds:
            self.evidence = free_evidence()
            self.page_title = str(self.evidence["title"])
            self.html = str(self.evidence["visibleText"])
            return
        raise PlaywrightTimeoutError("Timeout")

    def locator(self, selector: str) -> FakeLocator:
        self.locator_selectors.append(selector)
        return FakeLocator(self, selector)

    def on(self, event: str, handler: object) -> None:
        if event != "request":
            return
        self._request_listeners.append(handler)
        if self.stale_request_on_listen:
            request = FakeRequest(url=self.url, resource_type="xhr")
            self._last_validate_request = request
            handler(request)  # type: ignore[operator]

    def remove_listener(self, event: str, handler: object) -> None:
        if event != "request":
            return
        if handler in self._request_listeners:
            self._request_listeners.remove(handler)

    def emit_validate_request(self) -> None:
        request = FakeRequest(url=self.url, resource_type="xhr")
        self._last_validate_request = request
        for handler in list(self._request_listeners):
            handler(request)  # type: ignore[operator]

    def build_validate_response(self) -> FakeResponse:
        request = self._last_validate_request or FakeRequest(
            url=self.url, resource_type="xhr"
        )
        return FakeResponse(self.url, self.expect_response_status, request)

    def expect_response(
        self,
        predicate: object,
        *,
        timeout: int,
    ) -> FakeExpectResponse:
        del predicate
        self.expect_response_calls += 1
        self.expect_response_timeout = timeout
        return FakeExpectResponse(self)

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

    async def new_cdp_session(self, page: object) -> FakeCdpSession:
        del page
        return FakeCdpSession()


class FakeCdpSession:
    async def send(self, method: str, params: dict[str, object] | None = None) -> dict[str, object]:
        del method, params
        return {}


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
        self._managed_hold_patch = patch(
            "src.services.scraper._CF_MANAGED_CHALLENGE_HOLD_MS",
            0,
        )
        self._stability_patch.start()
        self._poll_patch.start()
        self._retry_patch.start()
        self._queue_wait_patch.start()
        self._cf_hold_patch.start()
        self._managed_hold_patch.start()

    async def asyncTearDown(self) -> None:
        self._managed_hold_patch.stop()
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
        self.assertIn("select", page.locator_selectors)
        self.assertIn("option", page.locator_selectors)
        self.assertEqual(page.select_option_calls, [0, 1])
        self.assertEqual(page.select_option_timeout, 15_000)
        self.assertEqual(page.option_wait_calls[0]["state"], "attached")
        self.assertEqual(page.option_wait_calls[0]["timeout"], 10_000)
        self.assertEqual(page.expect_response_calls, 1)
        self.assertEqual(page.expect_response_timeout, 15_000)
        self.assertEqual(page.wait_for_function_calls, 0)
        self.assertEqual(page.wait_for_load_state_calls, 0)
        self.assertEqual(context.new_page_calls, 0)
        self.assertEqual(_QUEUE_UI_WAIT_MS, 20_000)
        self.assertEqual(_QUEUE_UI_NETWORKIDLE_MS, 10_000)
        self.assertEqual(_SERVICE_SELECT_TIMEOUT_MS, 15_000)
        self.assertEqual(_SERVICE_VALIDATE_RESPONSE_TIMEOUT_MS, 15_000)
        self.assertEqual(_CF_MANAGED_CHALLENGE_HOLD_MS, 15_000)

    async def test_challenge_latches_and_does_not_reload_again(self) -> None:
        page = FakePage(TARGET_URL, challenge_evidence())
        scraper = SlotScraper(settings())
        scraper._browser = FakeBrowser([FakeContext([page])])  # type: ignore[assignment]

        delayed = await scraper.check_availability()
        self.assertEqual(delayed.status, SlotStatus.UNKNOWN)
        self.assertEqual(
            delayed.failure_code,
            ScraperFailureCode.CLOUDFLARE_DELAYED,
        )
        health = await scraper.get_health_snapshot()
        self.assertEqual(health.status, ScraperHealthStatus.DEGRADED)
        self.assertEqual(
            health.failure_code,
            ScraperFailureCode.CLOUDFLARE_DELAYED,
        )
        self.assertEqual(page.reload_calls, 1)

        with self.assertRaises(CloudflareChallengeError):
            await scraper.check_availability()
        with self.assertRaises(CloudflareChallengeError):
            await scraper.check_availability()

        self.assertEqual(page.reload_calls, 2)
        self.assertEqual(page.wait_for_function_calls, 3)
        health = await scraper.get_health_snapshot()
        self.assertEqual(health.status, ScraperHealthStatus.NEEDS_HUMAN)
        self.assertEqual(
            health.failure_code,
            ScraperFailureCode.CLOUDFLARE_CHALLENGE,
        )

    async def test_ukrainian_turnstile_fails_before_queue_ui_wait(self) -> None:
        raw = {
            "title": "Трохи зачекайте...",
            "url": TARGET_URL,
            "visibleText": "Триває перевірка безпеки. Підтвердьте, що ви людина.",
            "occupiedBannerVisible": False,
            "serviceSelectVisible": False,
            "selectPlaceholderVisible": False,
            "telInputVisible": False,
            "serviceOptionSelected": False,
            "challengeVisible": False,
        }
        page = FakePage(TARGET_URL, raw)
        page.html = (
            "<p>Триває перевірка безпеки</p>"
            '<iframe src="https://challenges.cloudflare.com/'
            'cdn-cgi/challenge-platform/h/b/cf-chl-widget/abc"></iframe>'
        )
        scraper = SlotScraper(settings())
        scraper._browser = FakeBrowser([FakeContext([page])])  # type: ignore[assignment]

        delayed = await scraper.check_availability()
        self.assertEqual(delayed.status, SlotStatus.UNKNOWN)
        self.assertEqual(
            delayed.failure_code,
            ScraperFailureCode.CLOUDFLARE_DELAYED,
        )
        self.assertEqual(page.reload_calls, 1)
        self.assertEqual(page.wait_for_selector_calls, 0)
        self.assertEqual(page.wait_for_function_calls, 1)
        health = await scraper.get_health_snapshot()
        self.assertEqual(health.status, ScraperHealthStatus.DEGRADED)
        self.assertEqual(
            health.failure_code,
            ScraperFailureCode.CLOUDFLARE_DELAYED,
        )
        with self.assertRaises(CloudflareChallengeError):
            await scraper.check_availability()
        self.assertEqual(page.reload_calls, 2)
        self.assertEqual(page.wait_for_function_calls, 2)
        health = await scraper.get_health_snapshot()
        self.assertEqual(health.status, ScraperHealthStatus.NEEDS_HUMAN)
        self.assertEqual(
            health.failure_code,
            ScraperFailureCode.CLOUDFLARE_CHALLENGE,
        )
        with self.assertRaises(CloudflareChallengeError):
            await scraper.check_availability()
        self.assertEqual(page.reload_calls, 2)
        self.assertEqual(page.wait_for_function_calls, 3)

    async def test_waiting_room_is_treated_as_cloudflare_challenge(self) -> None:
        raw = {
            "title": "Waiting Room",
            "url": TARGET_URL,
            "visibleText": (
                "Вас додано до черги. Орієнтовний час очікування. "
                "Ви у віртуальну чергу."
            ),
            "occupiedBannerVisible": False,
            "serviceSelectVisible": False,
            "selectPlaceholderVisible": False,
            "telInputVisible": False,
            "serviceOptionSelected": False,
            "challengeVisible": False,
        }
        page = FakePage(TARGET_URL, raw)
        page.html = (
            "<h1>Waiting Room</h1>"
            "<p>Вас додано до черги</p>"
            "<p>Орієнтовний час очікування</p>"
            "<p>віртуальну чергу</p>"
        )
        scraper = SlotScraper(settings())
        scraper._browser = FakeBrowser([FakeContext([page])])  # type: ignore[assignment]

        delayed = await scraper.check_availability()
        self.assertEqual(delayed.status, SlotStatus.UNKNOWN)
        self.assertEqual(
            delayed.failure_code,
            ScraperFailureCode.CLOUDFLARE_DELAYED,
        )
        self.assertEqual(page.wait_for_selector_calls, 0)
        with self.assertRaises(CloudflareChallengeError):
            await scraper.check_availability()
        health = await scraper.get_health_snapshot()
        self.assertEqual(health.status, ScraperHealthStatus.NEEDS_HUMAN)
        self.assertEqual(
            health.failure_code,
            ScraperFailureCode.CLOUDFLARE_CHALLENGE,
        )

    async def test_managed_challenge_clears_before_select(self) -> None:
        page = FakePage(TARGET_URL, challenge_evidence())
        page.wait_for_function_succeeds = True
        scraper = SlotScraper(settings())
        scraper._browser = FakeBrowser([FakeContext([page])])  # type: ignore[assignment]

        result = await scraper.check_availability()

        self.assertEqual(result.status, SlotStatus.FREE_SLOTS_AVAILABLE)
        self.assertEqual(page.wait_for_function_calls, 1)
        self.assertEqual(page.select_option_calls, [0, 1])
        health = await scraper.get_health_snapshot()
        self.assertEqual(health.status, ScraperHealthStatus.READY)
        self.assertIsNone(health.failure_code)

    async def test_latched_challenge_recovers_when_form_is_present(self) -> None:
        page = FakePage(TARGET_URL, challenge_evidence())
        scraper = SlotScraper(settings())
        scraper._browser = FakeBrowser([FakeContext([page])])  # type: ignore[assignment]

        delayed = await scraper.check_availability()
        self.assertEqual(
            delayed.failure_code,
            ScraperFailureCode.CLOUDFLARE_DELAYED,
        )

        page.evidence = free_evidence()
        page.page_title = ""
        page.html = ""
        result = await scraper.check_availability()

        self.assertEqual(result.status, SlotStatus.FREE_SLOTS_AVAILABLE)
        self.assertEqual(page.reload_calls, 2)
        health = await scraper.get_health_snapshot()
        self.assertEqual(health.status, ScraperHealthStatus.READY)
        self.assertIsNone(health.failure_code)

    async def test_booking_form_turnstile_iframe_does_not_latch_challenge(self) -> None:
        page = FakePage(TARGET_URL)
        page.html = (
            "<label>Послуга</label><select></select>"
            '<iframe src="https://challenges.cloudflare.com/'
            'cdn-cgi/challenge-platform/h/b/cf-chl-widget/abc"></iframe>'
        )
        scraper = SlotScraper(settings())
        scraper._browser = FakeBrowser([FakeContext([page])])  # type: ignore[assignment]

        result = await scraper.check_availability()

        self.assertEqual(result.status, SlotStatus.FREE_SLOTS_AVAILABLE)
        self.assertEqual(page.reload_calls, 1)
        self.assertEqual(page.wait_for_selector_calls, 1)
        self.assertEqual(page.wait_for_function_calls, 0)
        self.assertEqual(page.select_option_calls, [0, 1])
        health = await scraper.get_health_snapshot()
        self.assertEqual(health.status, ScraperHealthStatus.READY)
        self.assertIsNone(health.failure_code)

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
        self.assertEqual(page.wait_for_load_state_calls, 1)
        self.assertEqual(page.wait_for_load_state_state, "networkidle")
        self.assertEqual(page.select_option_calls, [0, 1])
        self.assertEqual(page.reload_calls, 1)

    async def test_queue_ui_retries_after_selector_timeout(self) -> None:
        page = FakePage(TARGET_URL)
        page.wait_for_selector_errors = [PlaywrightTimeoutError("Timeout")]
        scraper = SlotScraper(settings())
        scraper._browser = FakeBrowser([FakeContext([page])])  # type: ignore[assignment]

        result = await scraper.check_availability()

        self.assertEqual(result.status, SlotStatus.FREE_SLOTS_AVAILABLE)
        self.assertEqual(page.wait_for_selector_calls, 2)
        self.assertEqual(page.wait_for_load_state_calls, 1)
        self.assertEqual(page.wait_for_load_state_state, "networkidle")
        self.assertEqual(page.wait_for_load_state_timeout, 10_000)

    async def test_rate_limit_message_raises_typed_failure(self) -> None:
        raw = free_evidence()
        raw["visibleText"] = (
            "Too many requests, please try again later!     /g"
        )
        page = FakePage(TARGET_URL, raw)
        context = FakeContext([page])
        scraper = SlotScraper(settings())
        scraper._browser = FakeBrowser([context])  # type: ignore[assignment]

        with self.assertRaises(RateLimitException):
            await scraper.check_availability()

        self.assertEqual(page.reload_calls, 1)
        self.assertEqual(page.wait_for_selector_calls, 0)
        self.assertEqual(context.clear_cookies_calls, 1)
        health = await scraper.get_health_snapshot()
        self.assertEqual(health.status, ScraperHealthStatus.DEGRADED)
        self.assertEqual(health.failure_code, ScraperFailureCode.RATE_LIMITED)

    async def test_rate_limit_hard_reloads_on_next_cycle(self) -> None:
        raw = free_evidence()
        raw["visibleText"] = (
            "Too many requests, please try again later!     /g"
        )
        page = FakePage(TARGET_URL, raw)
        context = FakeContext([page])
        scraper = SlotScraper(settings())
        scraper._browser = FakeBrowser([context])  # type: ignore[assignment]

        with self.assertRaises(RateLimitException):
            await scraper.check_availability()

        page.evidence = free_evidence()
        page.html = ""
        result = await scraper.check_availability()

        self.assertEqual(result.status, SlotStatus.FREE_SLOTS_AVAILABLE)
        self.assertEqual(page.reload_calls, 1)
        self.assertEqual(page.goto_calls, 1)
        self.assertEqual(page.goto_url, TARGET_URL)
        self.assertGreaterEqual(context.clear_cookies_calls, 2)

    async def test_hard_reload_retries_if_goto_fails(self) -> None:
        page = FakePage(TARGET_URL)
        context = FakeContext([page])
        scraper = SlotScraper(settings())
        scraper._browser = FakeBrowser([context])  # type: ignore[assignment]
        await scraper.arm_hard_reload()
        page.goto_error = PlaywrightTimeoutError("Timeout")

        result = await scraper.check_availability()
        self.assertEqual(result.failure_code, ScraperFailureCode.NAVIGATION_TIMEOUT)
        self.assertEqual(page.goto_calls, 1)
        self.assertTrue(scraper._hard_reload_pending)

        page.goto_error = None
        recovered = await scraper.check_availability()
        self.assertEqual(recovered.status, SlotStatus.FREE_SLOTS_AVAILABLE)
        self.assertEqual(page.goto_calls, 2)
        self.assertFalse(scraper._hard_reload_pending)

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

        delayed = await scraper.check_availability()
        self.assertEqual(
            delayed.failure_code,
            ScraperFailureCode.CLOUDFLARE_DELAYED,
        )
        with self.assertRaises(CloudflareChallengeError):
            await scraper.check_availability()
        page.evidence = free_evidence()
        result = await scraper.check_availability()

        self.assertEqual(result.status, SlotStatus.FREE_SLOTS_AVAILABLE)
        self.assertEqual(page.reload_calls, 2)
        health = await scraper.get_health_snapshot()
        self.assertEqual(health.status, ScraperHealthStatus.READY)
        self.assertIsNone(health.failure_code)

    async def test_inconclusive_recovery_probe_keeps_needs_human_health(self) -> None:
        page = FakePage(TARGET_URL, challenge_evidence())
        scraper = SlotScraper(settings())
        scraper._browser = FakeBrowser([FakeContext([page])])  # type: ignore[assignment]
        delayed = await scraper.check_availability()
        self.assertEqual(
            delayed.failure_code,
            ScraperFailureCode.CLOUDFLARE_DELAYED,
        )
        with self.assertRaises(CloudflareChallengeError):
            await scraper.check_availability()

        incomplete = free_evidence()
        incomplete["serviceOptionSelected"] = False
        incomplete["serviceSelectVisible"] = False
        page.evidence = incomplete
        with patch("src.services.scraper._DOM_SIGNAL_TIMEOUT_MS", 0):
            result = await scraper.check_availability()

        self.assertEqual(result.status, SlotStatus.UNKNOWN)
        health = await scraper.get_health_snapshot()
        self.assertEqual(health.status, ScraperHealthStatus.NEEDS_HUMAN)
        self.assertTrue(health.target_tab_present)
        self.assertEqual(
            health.failure_code,
            ScraperFailureCode.CLOUDFLARE_CHALLENGE,
        )

    async def test_latched_probe_waits_for_occupied_banner_after_select(self) -> None:
        occupied = free_evidence()
        occupied.update(
            {
                "visibleText": "Вибачте, на даний момент всі місця зайняті!",
                "occupiedBannerVisible": True,
            }
        )
        page = FakePage(TARGET_URL, challenge_evidence())
        scraper = SlotScraper(settings())
        scraper._browser = FakeBrowser([FakeContext([page])])  # type: ignore[assignment]

        delayed = await scraper.check_availability()
        self.assertEqual(
            delayed.failure_code,
            ScraperFailureCode.CLOUDFLARE_DELAYED,
        )
        with self.assertRaises(CloudflareChallengeError):
            await scraper.check_availability()

        page.evidence_sequence = [
            free_evidence(),
            free_evidence(),
            occupied,
            occupied,
        ]
        result = await scraper.check_availability()

        self.assertEqual(result.status, SlotStatus.NO_SLOTS)
        self.assertEqual(page.reload_calls, 2)
        self.assertEqual(page.select_option_calls, [0, 1])
        health = await scraper.get_health_snapshot()
        self.assertEqual(health.status, ScraperHealthStatus.READY)
        self.assertIsNone(health.failure_code)

    async def test_latched_probe_raises_when_challenge_still_present(self) -> None:
        page = FakePage(TARGET_URL, challenge_evidence())
        scraper = SlotScraper(settings())
        scraper._browser = FakeBrowser([FakeContext([page])])  # type: ignore[assignment]

        delayed = await scraper.check_availability()
        self.assertEqual(
            delayed.failure_code,
            ScraperFailureCode.CLOUDFLARE_DELAYED,
        )
        with self.assertRaises(CloudflareChallengeError):
            await scraper.check_availability()

        self.assertEqual(page.reload_calls, 2)
        health = await scraper.get_health_snapshot()
        self.assertEqual(health.status, ScraperHealthStatus.NEEDS_HUMAN)
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
        raw["serviceOptionSelected"] = False
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
                    "Вибачте, на даний момент всі місця зайняті!"
                ),
                "occupiedBannerVisible": True,
                "serviceSelectVisible": True,
                "selectPlaceholderVisible": True,
                "telInputVisible": True,
                "serviceOptionSelected": True,
            }
        )
        page = FakePage(TARGET_URL)
        page.evidence_sequence = [free_evidence(), occupied, occupied]
        scraper = SlotScraper(settings())
        scraper._browser = FakeBrowser([FakeContext([page])])  # type: ignore[assignment]

        result = await scraper.check_availability()

        self.assertEqual(result.status, SlotStatus.NO_SLOTS)

    async def test_xhr_500_returns_server_error_without_dom_poll(self) -> None:
        page = FakePage(TARGET_URL)
        page.expect_response_status = 500
        scraper = SlotScraper(settings())
        scraper._browser = FakeBrowser([FakeContext([page])])  # type: ignore[assignment]

        result = await scraper.check_availability()

        self.assertEqual(result.status, SlotStatus.UNKNOWN)
        self.assertEqual(result.failure_code, ScraperFailureCode.SERVER_ERROR)
        self.assertEqual(page.select_option_calls, [0, 1])
        self.assertEqual(page.expect_response_calls, 1)
        health = await scraper.get_health_snapshot()
        self.assertEqual(health.failure_code, ScraperFailureCode.SERVER_ERROR)

    async def test_xhr_429_raises_rate_limit(self) -> None:
        page = FakePage(TARGET_URL)
        page.expect_response_status = 429
        scraper = SlotScraper(settings())
        scraper._browser = FakeBrowser([FakeContext([page])])  # type: ignore[assignment]

        with self.assertRaises(RateLimitException):
            await scraper.check_availability()

        health = await scraper.get_health_snapshot()
        self.assertEqual(health.failure_code, ScraperFailureCode.RATE_LIMITED)
        self.assertEqual(page.context.clear_cookies_calls, 1)

    async def test_xhr_4xx_returns_service_validate_error(self) -> None:
        page = FakePage(TARGET_URL)
        page.expect_response_status = 404
        scraper = SlotScraper(settings())
        scraper._browser = FakeBrowser([FakeContext([page])])  # type: ignore[assignment]

        result = await scraper.check_availability()

        self.assertEqual(result.status, SlotStatus.UNKNOWN)
        self.assertEqual(result.failure_code, ScraperFailureCode.SERVICE_VALIDATE_ERROR)
        self.assertEqual(page.select_option_calls, [0, 1])

    async def test_stale_validate_response_is_inconclusive(self) -> None:
        page = FakePage(TARGET_URL)
        page.stale_request_on_listen = True
        scraper = SlotScraper(settings())
        scraper._browser = FakeBrowser([FakeContext([page])])  # type: ignore[assignment]

        result = await scraper.check_availability()

        self.assertEqual(result.status, SlotStatus.UNKNOWN)
        self.assertEqual(result.failure_code, ScraperFailureCode.INCONCLUSIVE_PAGE)
        self.assertEqual(result.details, "stale response matched")

    async def test_missing_validate_request_is_inconclusive(self) -> None:
        page = FakePage(TARGET_URL)
        page.skip_validate_request = True
        scraper = SlotScraper(settings())
        scraper._browser = FakeBrowser([FakeContext([page])])  # type: ignore[assignment]

        result = await scraper.check_availability()

        self.assertEqual(result.status, SlotStatus.UNKNOWN)
        self.assertEqual(result.failure_code, ScraperFailureCode.INCONCLUSIVE_PAGE)
        self.assertEqual(result.details, "stale response matched")

    async def test_validate_response_timeout_is_inconclusive(self) -> None:
        page = FakePage(TARGET_URL)
        page.expect_response_error = PlaywrightTimeoutError("Timeout")
        scraper = SlotScraper(settings())
        scraper._browser = FakeBrowser([FakeContext([page])])  # type: ignore[assignment]

        result = await scraper.check_availability()

        self.assertEqual(result.status, SlotStatus.UNKNOWN)
        self.assertEqual(result.failure_code, ScraperFailureCode.INCONCLUSIVE_PAGE)
        self.assertEqual(result.details, "service validation response timed out")


if __name__ == "__main__":
    unittest.main()
