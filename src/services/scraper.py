from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

from playwright.async_api import Browser, Locator, Page, Playwright
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from src.core.config import Settings
from src.core.exceptions import (
    CdpUnavailableError,
    CloudflareChallengeError,
    HumanActionRequiredError,
    RateLimitException,
    TargetTabClosedError,
    TargetTabMissingError,
)
from src.core.models import (
    ScraperFailureCode,
    ScraperHealthSnapshot,
    ScraperHealthStatus,
    SlotCheckResult,
    SlotStatus,
)
from src.services.slot_parser import (
    OCCUPIED_HEADING,
    SELECT_PLACEHOLDER,
    SERVICE_LABEL,
    SlotPageEvidence,
    classify_slot_evidence,
    has_cloudflare_challenge,
    has_cloudflare_source,
    has_rate_limit_message,
    has_server_error_page,
    has_server_error_source,
)

logger = logging.getLogger(__name__)

_CDP_CONNECT_TIMEOUT_MS = 15_000
_CDP_RELOAD_TIMEOUT_MS = 15_000
_DOM_SIGNAL_TIMEOUT_MS = 15_000
_QUEUE_UI_WAIT_MS = 20_000
_CONTEXT_RETRY_SECONDS = 3.0
_DOM_POLL_SECONDS = 0.25
_DOM_STABILITY_SECONDS = 3.0
_SERVICE_SELECT_TIMEOUT_MS = 5_000
_SERVICE_PLACEHOLDER_INDEX = 0
_SERVICE_OPTION_INDEX = 1
_SERVICE_OPTION_ATTACH_TIMEOUT_MS = 10_000
_SERVICE_VALIDATE_RESPONSE_TIMEOUT_MS = 10_000
_SUSPICIOUS_VALIDATE_SECONDS = 0.2
_CF_INTERSTITIAL_HOLD_SECONDS = 5.0
_QUEUE_UI_SELECTOR = "select"

_DOM_EVIDENCE_SCRIPT = f"""
() => {{
  const normalize = (value) => String(value || "")
    .toLocaleLowerCase("uk-UA")
    .replace(/[–—−]/g, "-")
    .replace(/\\s+/g, " ")
    .trim();
  const isVisible = (element) => {{
    if (!(element instanceof Element)) return false;
    const style = window.getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== "none"
      && style.visibility !== "hidden"
      && style.opacity !== "0"
      && rect.width > 0
      && rect.height > 0;
  }};

  const bodyText = document.body ? document.body.innerText : "";
  const normalizedBody = normalize(bodyText);
  const occupiedContainers = Array.from(
    document.querySelectorAll("main, section, article, div")
  );
  const occupiedBannerVisible = occupiedContainers.some((element) => {{
    if (!isVisible(element)) return false;
    const text = normalize(element.innerText);
    return text.includes({OCCUPIED_HEADING!r});
  }});

  const visibleSelects = Array.from(document.querySelectorAll("select"))
    .filter(isVisible);
  const serviceSelectVisible = normalizedBody.includes({SERVICE_LABEL!r})
    && visibleSelects.length > 0;
  const selectPlaceholderVisible = visibleSelects.some((select) =>
    Array.from(select.options).some(
      (option) => normalize(option.textContent).includes({SELECT_PLACEHOLDER!r})
    )
  );
  const telInputVisible = Array.from(
    document.querySelectorAll('input[type="tel"]')
  ).some(isVisible);
  const serviceOptionSelected = visibleSelects.some(
    (select) => select.selectedIndex >= 1
  );
  const challengeVisible = Array.from(document.querySelectorAll(
    "#challenge-running, #challenge-platform, #cf-spinner, .cf-browser-verification"
  )).some(isVisible)
    || Array.from(document.querySelectorAll("iframe")).some((iframe) =>
      String(iframe.getAttribute("src") || "").includes("cf-chl-widget")
    );

  return {{
    title: document.title || "",
    url: window.location.href,
    visibleText: bodyText,
    occupiedBannerVisible,
    serviceSelectVisible,
    selectPlaceholderVisible,
    telInputVisible,
    serviceOptionSelected,
    challengeVisible,
  }};
}}
"""


def is_target_closed_error(exc: BaseException) -> bool:
    if type(exc).__name__ == "TargetClosedError":
        return True
    message = str(exc).lower()
    return (
        "target closed" in message
        or "has been closed" in message
        or "browser has been closed" in message
    )


def is_execution_context_destroyed(exc: BaseException) -> bool:
    message = str(exc).lower()
    return (
        "execution context was destroyed" in message
        or "most likely because of a navigation" in message
    )


def normalize_cdp_url(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = raw.strip()
    return value or None


def _canonical_page_identity(raw_url: str) -> tuple[str, str, int, str] | None:
    parsed = urlparse(raw_url)
    scheme = parsed.scheme.casefold()
    hostname = parsed.hostname.casefold() if parsed.hostname is not None else ""
    if scheme not in {"http", "https"} or not hostname:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    effective_port = port or (443 if scheme == "https" else 80)
    path = parsed.path.rstrip("/") or "/"
    return scheme, hostname, effective_port, path


def _redacted_page_location(raw_url: str) -> str:
    identity = _canonical_page_identity(raw_url)
    if identity is None:
        return "non_http_page"
    scheme, hostname, port, path = identity
    default_port = 443 if scheme == "https" else 80
    authority = hostname if port == default_port else f"{hostname}:{port}"
    return f"{scheme}://{authority}{path}"


def page_matches_target_url(page_url: str, target_url: str) -> bool:
    page_identity = _canonical_page_identity(page_url)
    target_identity = _canonical_page_identity(target_url)
    return page_identity is not None and page_identity == target_identity


def page_matches_challenge_url(page_url: str, target_url: str) -> bool:
    page = urlparse(page_url)
    target = urlparse(target_url)
    if (
        page.hostname is None
        or target.hostname is None
        or page.hostname.lower() != target.hostname.lower()
    ):
        return False
    lowered = page_url.casefold()
    return "/cdn-cgi/challenge-platform" in lowered or "__cf_chl" in lowered


def cdp_tab_matches(page_url: str, target_url: str) -> bool:
    return page_matches_target_url(page_url, target_url)


async def collect_dom_evidence(page: Page) -> SlotPageEvidence:
    try:
        raw = await page.evaluate(_DOM_EVIDENCE_SCRIPT)
    except PlaywrightError as exc:
        if not is_execution_context_destroyed(exc):
            raise
        logger.warning(
            "dom_evaluate_context_destroyed",
            extra={"error": str(exc)},
        )
        await asyncio.sleep(_CONTEXT_RETRY_SECONDS)
        raw = await page.evaluate(_DOM_EVIDENCE_SCRIPT)
    if not isinstance(raw, dict):
        raise PlaywrightError("DOM evidence script returned a non-object value")
    return SlotPageEvidence(
        title=str(raw.get("title", "")),
        url=str(raw.get("url", "")),
        visible_text=str(raw.get("visibleText", "")),
        occupied_banner_visible=bool(raw.get("occupiedBannerVisible")),
        service_select_visible=bool(raw.get("serviceSelectVisible")),
        select_placeholder_visible=bool(raw.get("selectPlaceholderVisible")),
        tel_input_visible=bool(raw.get("telInputVisible")),
        service_option_selected=bool(raw.get("serviceOptionSelected")),
        challenge_visible=bool(raw.get("challengeVisible")),
    )


class SlotScraper:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._operation_lock = asyncio.Lock()
        self._latched_failure: ScraperFailureCode | None = None
        self._health = ScraperHealthSnapshot(
            status=ScraperHealthStatus.STOPPED,
            cdp_connected=False,
            target_tab_present=False,
            updated_at=datetime.now(timezone.utc),
        )

    async def get_health_snapshot(self) -> ScraperHealthSnapshot:
        return self._health

    async def start(self) -> None:
        async with self._operation_lock:
            await self._start_unlocked()

    async def stop(self) -> None:
        async with self._operation_lock:
            await self._disconnect_unlocked()
            self._set_health(
                status=ScraperHealthStatus.STOPPED,
                cdp_connected=False,
                target_tab_present=False,
            )

    async def check_availability(self) -> SlotCheckResult:
        async with self._operation_lock:
            try:
                await self._start_unlocked()
                page = self._find_target_page()
                if self._latched_failure is not None:
                    return await self._probe_latched_page(page)
                return await self._reload_and_classify(page)
            except RateLimitException as exc:
                self._set_health(
                    status=ScraperHealthStatus.DEGRADED,
                    cdp_connected=self._browser is not None
                    and self._browser.is_connected(),
                    target_tab_present=True,
                    failure_code=exc.failure_code,
                    details=str(exc),
                )
                raise
            except HumanActionRequiredError as exc:
                self._latch_human_action(exc)
                raise
            except asyncio.CancelledError:
                raise
            except PlaywrightError as exc:
                if is_target_closed_error(exc):
                    failure = TargetTabClosedError(
                        "target queue tab closed during DOM inspection"
                    )
                    self._latch_human_action(failure)
                    raise failure from exc
                if self._browser is None or not self._browser.is_connected():
                    failure = CdpUnavailableError(
                        "CDP Chrome disconnected during DOM inspection"
                    )
                    await self._disconnect_unlocked()
                    self._latch_human_action(failure)
                    raise failure from exc
                logger.exception(
                    "scraper_failed",
                    extra={"city": self._settings.city_name, "error": str(exc)},
                )
                return self._unknown_result(
                    ScraperFailureCode.SCRAPER_ERROR,
                    str(exc),
                )

    async def _start_unlocked(self) -> None:
        if self._browser is not None and self._browser.is_connected():
            return
        await self._disconnect_unlocked()
        cdp_url = normalize_cdp_url(self._settings.cdp_url)
        if cdp_url is None:
            raise CdpUnavailableError("CDP_URL is required in strict CDP mode")
        self._set_health(
            status=ScraperHealthStatus.STARTING,
            cdp_connected=False,
            target_tab_present=False,
        )
        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.connect_over_cdp(
                cdp_url,
                timeout=_CDP_CONNECT_TIMEOUT_MS,
            )
        except (PlaywrightError, OSError) as exc:
            await self._disconnect_unlocked()
            raise CdpUnavailableError(
                f"cannot connect to configured CDP endpoint: {cdp_url}"
            ) from exc
        logger.info(
            "scraper_started",
            extra={
                "city": self._settings.city_name,
                "channel": "cdp",
                "cdp_url": cdp_url,
            },
        )
        self._set_health(
            status=ScraperHealthStatus.READY,
            cdp_connected=True,
            target_tab_present=False,
        )

    async def _disconnect_unlocked(self) -> None:
        self._browser = None
        playwright = self._playwright
        self._playwright = None
        if playwright is None:
            return
        try:
            await playwright.stop()
        except PlaywrightError as exc:
            logger.warning(
                "playwright_stop_failed",
                extra={"city": self._settings.city_name, "error": str(exc)},
            )

    def _find_target_page(self) -> Page:
        browser = self._require_cdp_connected()
        target_url = str(self._settings.target_url)
        challenge_page_found = False
        open_locations: list[str] = []
        for context in browser.contexts:
            for page in context.pages:
                if self._page_is_closed(page):
                    continue
                page_url = self._page_url(page)
                open_locations.append(_redacted_page_location(page_url))
                if page_matches_target_url(page_url, target_url):
                    latched = self._latched_failure is not None
                    self._set_health(
                        status=(
                            ScraperHealthStatus.NEEDS_HUMAN
                            if latched
                            else ScraperHealthStatus.READY
                        ),
                        cdp_connected=True,
                        target_tab_present=True,
                        failure_code=self._latched_failure,
                        details=self._health.details if latched else "",
                    )
                    return page
                challenge_page_found = (
                    challenge_page_found
                    or page_matches_challenge_url(page_url, target_url)
                )
        if challenge_page_found:
            raise CloudflareChallengeError(
                "target tab is showing a Cloudflare challenge"
            )
        logger.warning(
            "cdp_target_tab_not_found",
            extra={
                "city": self._settings.city_name,
                "open_tabs": len(open_locations),
                "tab_locations": open_locations,
            },
        )
        raise TargetTabMissingError(
            "CDP Chrome has no exact TARGET_URL tab; open and clear it manually"
        )

    async def _reload_and_classify(self, page: Page) -> SlotCheckResult:
        self._require_cdp_connected()
        try:
            await page.reload(
                wait_until="domcontentloaded",
                timeout=_CDP_RELOAD_TIMEOUT_MS,
            )
        except PlaywrightTimeoutError as exc:
            if self._page_is_closed(page):
                raise TargetTabClosedError("target tab closed during soft reload") from exc
            immediate = await self._peek_terminal_page_state(page)
            if immediate is not None:
                return immediate
            evidence = await collect_dom_evidence(page)
            self._raise_if_rate_limited(evidence)
            if has_cloudflare_challenge(evidence):
                raise CloudflareChallengeError(
                    "Cloudflare challenge appeared during soft reload"
                ) from exc
            return self._unknown_result(
                ScraperFailureCode.NAVIGATION_TIMEOUT,
                "target tab soft reload exceeded 15 seconds",
            )
        except PlaywrightError as exc:
            if is_target_closed_error(exc) or self._page_is_closed(page):
                raise TargetTabClosedError("target tab closed during soft reload") from exc
            raise
        immediate = await self._peek_terminal_page_state(page)
        if immediate is not None:
            return immediate
        await self._wait_for_queue_ui(page)
        select_result = await self._select_first_service(page)
        if select_result is not None:
            return select_result
        return await self._wait_for_decisive_evidence(page)

    async def _peek_terminal_page_state(self, page: Page) -> SlotCheckResult | None:
        self._require_cdp_connected()
        source = await self._read_page_source(page)
        if source is not None:
            title, html = source
            if has_cloudflare_source(title=title, html=html):
                raise CloudflareChallengeError(
                    "Cloudflare challenge page detected"
                )
            if has_server_error_source(title=title, html=html):
                return await self._emit_server_error(page)
        try:
            evidence = await collect_dom_evidence(page)
        except PlaywrightError as exc:
            if is_execution_context_destroyed(exc):
                return None
            raise
        self._raise_if_rate_limited(evidence)
        if has_server_error_page(evidence):
            return await self._emit_server_error(page)
        return None

    async def _read_page_source(self, page: Page) -> tuple[str, str] | None:
        try:
            title = await page.title()
            html = await page.content()
        except PlaywrightError as exc:
            if is_target_closed_error(exc) or self._page_is_closed(page):
                raise TargetTabClosedError(
                    "target tab closed during DOM inspection"
                ) from exc
            if is_execution_context_destroyed(exc):
                logger.warning(
                    "page_source_context_destroyed",
                    extra={"city": self._settings.city_name, "error": str(exc)},
                )
                return None
            raise
        return title, html

    async def _emit_server_error(self, page: Page) -> SlotCheckResult:
        await self._clear_target_cookies(page)
        return self._unknown_result(
            ScraperFailureCode.SERVER_ERROR,
            "site_backend_error",
        )

    async def _clear_target_cookies(self, page: Page) -> None:
        try:
            await page.context.clear_cookies()
        except PlaywrightError as exc:
            logger.warning(
                "target_cookie_clear_failed",
                extra={"city": self._settings.city_name, "error": str(exc)},
            )

    async def _wait_for_queue_ui(self, page: Page) -> None:
        self._require_cdp_connected()
        try:
            await page.wait_for_selector(
                _QUEUE_UI_SELECTOR,
                timeout=_QUEUE_UI_WAIT_MS,
                state="visible",
            )
        except PlaywrightTimeoutError:
            logger.info(
                "queue_ui_wait_timeout",
                extra={"city": self._settings.city_name, "timeout_ms": _QUEUE_UI_WAIT_MS},
            )
        except PlaywrightError as exc:
            if is_target_closed_error(exc) or self._page_is_closed(page):
                raise TargetTabClosedError(
                    "target tab closed while waiting for queue UI"
                ) from exc
            if not is_execution_context_destroyed(exc):
                raise
            logger.warning(
                "queue_ui_wait_context_destroyed",
                extra={"city": self._settings.city_name, "error": str(exc)},
            )
            await asyncio.sleep(_CONTEXT_RETRY_SECONDS)
            self._require_cdp_connected()
            if self._page_is_closed(page):
                raise TargetTabClosedError(
                    "target tab closed while waiting for queue UI"
                ) from exc
            try:
                await page.wait_for_selector(
                    _QUEUE_UI_SELECTOR,
                    timeout=_QUEUE_UI_WAIT_MS,
                    state="visible",
                )
            except PlaywrightTimeoutError:
                logger.info(
                    "queue_ui_wait_timeout",
                    extra={
                        "city": self._settings.city_name,
                        "timeout_ms": _QUEUE_UI_WAIT_MS,
                    },
                )
            except PlaywrightError as retry_exc:
                if is_target_closed_error(retry_exc) or self._page_is_closed(page):
                    raise TargetTabClosedError(
                        "target tab closed while waiting for queue UI"
                    ) from retry_exc
                if is_execution_context_destroyed(retry_exc):
                    return
                raise

    async def _select_first_service(self, page: Page) -> SlotCheckResult | None:
        self._require_cdp_connected()
        try:
            return await self._select_first_service_attempt(page)
        except PlaywrightTimeoutError:
            logger.info(
                "service_select_timeout",
                extra={
                    "city": self._settings.city_name,
                    "timeout_ms": _SERVICE_SELECT_TIMEOUT_MS,
                    "option_index": _SERVICE_OPTION_INDEX,
                },
            )
            return self._unknown_result(
                ScraperFailureCode.INCONCLUSIVE_PAGE,
                "service select or option attach timed out",
            )
        except PlaywrightError as exc:
            if is_target_closed_error(exc) or self._page_is_closed(page):
                raise TargetTabClosedError(
                    "target tab closed while selecting a service"
                ) from exc
            if not is_execution_context_destroyed(exc):
                raise
            logger.warning(
                "service_select_context_destroyed",
                extra={"city": self._settings.city_name, "error": str(exc)},
            )
            await asyncio.sleep(_CONTEXT_RETRY_SECONDS)
            self._require_cdp_connected()
            if self._page_is_closed(page):
                raise TargetTabClosedError(
                    "target tab closed while selecting a service"
                ) from exc
            try:
                return await self._select_first_service_attempt(page)
            except PlaywrightTimeoutError:
                logger.info(
                    "service_select_timeout",
                    extra={
                        "city": self._settings.city_name,
                        "timeout_ms": _SERVICE_SELECT_TIMEOUT_MS,
                        "option_index": _SERVICE_OPTION_INDEX,
                    },
                )
                return self._unknown_result(
                    ScraperFailureCode.INCONCLUSIVE_PAGE,
                    "service select or option attach timed out",
                )
            except PlaywrightError as retry_exc:
                if is_target_closed_error(retry_exc) or self._page_is_closed(page):
                    raise TargetTabClosedError(
                        "target tab closed while selecting a service"
                    ) from retry_exc
                if is_execution_context_destroyed(retry_exc):
                    return self._unknown_result(
                        ScraperFailureCode.INCONCLUSIVE_PAGE,
                        "service select context destroyed",
                    )
                raise

    async def _select_first_service_attempt(self, page: Page) -> SlotCheckResult | None:
        select = page.locator("select").first
        await select.locator("option").nth(_SERVICE_OPTION_INDEX).wait_for(
            state="attached",
            timeout=_SERVICE_OPTION_ATTACH_TIMEOUT_MS,
        )
        await select.select_option(
            index=_SERVICE_PLACEHOLDER_INDEX,
            timeout=_SERVICE_SELECT_TIMEOUT_MS,
        )
        return await self._await_service_validation(page, select)

    async def _await_service_validation(
        self,
        page: Page,
        select: Locator,
    ) -> SlotCheckResult | None:
        parsed = urlparse(page.url)
        page_origin = f"{parsed.scheme}://{parsed.netloc}"
        request_started_at: dict[int, float] = {}

        def _on_request(req: object) -> None:
            resource_type = getattr(req, "resource_type", "")
            request_url = str(getattr(req, "url", ""))
            if resource_type in ("xhr", "fetch") and request_url.startswith(page_origin):
                request_started_at[id(req)] = time.monotonic()

        def _is_fresh_validate_response(response: object) -> bool:
            request = getattr(response, "request", None)
            resource_type = getattr(request, "resource_type", "")
            response_url = str(getattr(response, "url", ""))
            started_at = request_started_at.get(id(request))
            return (
                resource_type in ("xhr", "fetch")
                and response_url.startswith(page_origin)
                and started_at is not None
                and started_at >= select_ts
            )

        page.on("request", _on_request)
        select_ts = time.monotonic()
        try:
            try:
                async with page.expect_response(
                    _is_fresh_validate_response,
                    timeout=_SERVICE_VALIDATE_RESPONSE_TIMEOUT_MS,
                ) as pending:
                    await select.select_option(
                        index=_SERVICE_OPTION_INDEX,
                        timeout=_SERVICE_SELECT_TIMEOUT_MS,
                    )
                response = await pending.value
            except PlaywrightTimeoutError:
                logger.info(
                    "service_validate_timeout",
                    extra={
                        "city": self._settings.city_name,
                        "timeout_ms": _SERVICE_VALIDATE_RESPONSE_TIMEOUT_MS,
                    },
                )
                return self._unknown_result(
                    ScraperFailureCode.INCONCLUSIVE_PAGE,
                    "service validation response timed out",
                )
        finally:
            page.remove_listener("request", _on_request)

        elapsed = time.monotonic() - select_ts
        captured_request_start = request_started_at.get(id(getattr(response, "request", None)))
        if captured_request_start is None or captured_request_start < select_ts:
            logger.warning(
                "stale_validate_response",
                extra={
                    "city": self._settings.city_name,
                    "response_url": str(getattr(response, "url", "")),
                },
            )
            return self._unknown_result(
                ScraperFailureCode.INCONCLUSIVE_PAGE,
                "stale response matched",
            )
        status = int(getattr(response, "status", 0))
        if status >= 500:
            return await self._emit_server_error(page)
        if status == 429:
            raise RateLimitException("XHR Rate Limit")
        if not bool(getattr(response, "ok", False)):
            return self._unknown_result(
                ScraperFailureCode.SERVICE_VALIDATE_ERROR,
                "service validation rejected",
            )
        if elapsed < _SUSPICIOUS_VALIDATE_SECONDS:
            logger.warning(
                "suspiciously_fast_validate_response",
                extra={
                    "city": self._settings.city_name,
                    "elapsed_ms": elapsed * 1000,
                },
            )
        service_text = await select.locator("option").nth(
            _SERVICE_OPTION_INDEX
        ).inner_text()
        logger.info(
            "service_option_selected",
            extra={
                "city": self._settings.city_name,
                "option_index": _SERVICE_OPTION_INDEX,
                "service_text": service_text.strip(),
            },
        )
        return None

    async def _probe_latched_page(self, page: Page) -> SlotCheckResult:
        self._require_cdp_connected()
        latched_failure = self._latched_failure
        latched_details = self._health.details
        evidence = await collect_dom_evidence(page)
        self._raise_if_rate_limited(evidence)
        if has_cloudflare_challenge(evidence):
            raise CloudflareChallengeError(
                "target tab still requires Cloudflare verification"
            )
        if has_server_error_page(evidence):
            return await self._emit_server_error(page)
        if evidence.service_select_visible:
            select_result = await self._select_first_service(page)
            if select_result is not None:
                if select_result.failure_code is ScraperFailureCode.INCONCLUSIVE_PAGE:
                    self._latched_failure = latched_failure
                    self._set_health(
                        status=ScraperHealthStatus.NEEDS_HUMAN,
                        cdp_connected=True,
                        target_tab_present=True,
                        failure_code=latched_failure,
                        details=latched_details,
                    )
                return select_result
        result = await self._wait_for_decisive_evidence(page)
        if result.status is SlotStatus.UNKNOWN:
            if result.failure_code is ScraperFailureCode.INCONCLUSIVE_PAGE:
                self._latched_failure = latched_failure
                self._set_health(
                    status=ScraperHealthStatus.NEEDS_HUMAN,
                    cdp_connected=True,
                    target_tab_present=True,
                    failure_code=latched_failure,
                    details=latched_details,
                )
            return result
        logger.info(
            "scraper_human_action_recovered",
            extra={"city": self._settings.city_name},
        )
        return result

    async def _wait_for_decisive_evidence(self, page: Page) -> SlotCheckResult:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (_DOM_SIGNAL_TIMEOUT_MS / 1000)
        last_result: SlotCheckResult | None = None
        candidate_status: SlotStatus | None = None
        candidate_since: float | None = None
        challenge_since: float | None = None
        while True:
            self._require_cdp_connected()
            evidence = await collect_dom_evidence(page)
            self._raise_if_rate_limited(evidence)
            if has_cloudflare_challenge(evidence):
                candidate_status = None
                candidate_since = None
                if challenge_since is None:
                    challenge_since = loop.time()
                if loop.time() - challenge_since >= _CF_INTERSTITIAL_HOLD_SECONDS:
                    raise CloudflareChallengeError(
                        "Cloudflare challenge appeared after soft reload"
                    )
                if loop.time() >= deadline:
                    raise CloudflareChallengeError(
                        "Cloudflare challenge appeared after soft reload"
                    )
                await asyncio.sleep(_DOM_POLL_SECONDS)
                continue
            challenge_since = None
            last_result = classify_slot_evidence(evidence)
            if last_result.failure_code is ScraperFailureCode.SERVER_ERROR:
                return await self._emit_server_error(page)
            if last_result.status is not SlotStatus.UNKNOWN:
                if candidate_status is not last_result.status:
                    candidate_status = last_result.status
                    candidate_since = loop.time()
                elif (
                    candidate_since is not None
                    and loop.time() - candidate_since >= _DOM_STABILITY_SECONDS
                ):
                    self._latched_failure = None
                    self._set_health(
                        status=ScraperHealthStatus.READY,
                        cdp_connected=True,
                        target_tab_present=True,
                    )
                    return last_result
            else:
                candidate_status = None
                candidate_since = None
            if loop.time() >= deadline:
                return self._unknown_result(
                    ScraperFailureCode.INCONCLUSIVE_PAGE,
                    (
                        "visible slot-state evidence did not remain stable"
                        if candidate_status is not None
                        else last_result.details
                    ),
                )
            await asyncio.sleep(_DOM_POLL_SECONDS)

    def _raise_if_rate_limited(self, evidence: SlotPageEvidence) -> None:
        if has_rate_limit_message(evidence):
            raise RateLimitException(
                "Too many requests, please try again later"
            )

    def _require_cdp_connected(self) -> Browser:
        browser = self._browser
        if browser is None or not browser.is_connected():
            raise CdpUnavailableError("CDP Chrome is not connected")
        return browser

    def _unknown_result(
        self,
        failure_code: ScraperFailureCode,
        details: str,
    ) -> SlotCheckResult:
        self._set_health(
            status=ScraperHealthStatus.DEGRADED,
            cdp_connected=self._browser is not None and self._browser.is_connected(),
            target_tab_present=failure_code is not ScraperFailureCode.TARGET_TAB_MISSING,
            failure_code=failure_code,
            details=details,
        )
        return SlotCheckResult(
            status=SlotStatus.UNKNOWN,
            checked_at=datetime.now(timezone.utc),
            error=failure_code.value,
            failure_code=failure_code,
            details=details,
        )

    def _latch_human_action(self, exc: HumanActionRequiredError) -> None:
        self._latched_failure = exc.failure_code
        connected = self._browser is not None and self._browser.is_connected()
        self._set_health(
            status=ScraperHealthStatus.NEEDS_HUMAN,
            cdp_connected=connected,
            target_tab_present=exc.failure_code
            not in {
                ScraperFailureCode.CDP_UNAVAILABLE,
                ScraperFailureCode.TARGET_TAB_MISSING,
            },
            failure_code=exc.failure_code,
            details=str(exc),
        )
        logger.warning(
            "scraper_human_action_required",
            extra={
                "city": self._settings.city_name,
                "failure_code": exc.failure_code.value,
                "error": str(exc),
            },
        )

    def _set_health(
        self,
        *,
        status: ScraperHealthStatus,
        cdp_connected: bool,
        target_tab_present: bool,
        failure_code: ScraperFailureCode | None = None,
        details: str = "",
    ) -> None:
        self._health = ScraperHealthSnapshot(
            status=status,
            cdp_connected=cdp_connected,
            target_tab_present=target_tab_present,
            updated_at=datetime.now(timezone.utc),
            failure_code=failure_code,
            details=details,
        )

    def _page_is_closed(self, page: Page) -> bool:
        try:
            return page.is_closed()
        except PlaywrightError as exc:
            if is_target_closed_error(exc):
                return True
            raise

    def _page_url(self, page: Page) -> str:
        try:
            return page.url or ""
        except PlaywrightError as exc:
            if is_target_closed_error(exc):
                return ""
            raise
