from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from src.core.models import ScraperFailureCode, SlotCheckResult, SlotStatus

_WHITESPACE_RE = re.compile(r"\s+")
_DASH_RE = re.compile(r"[–—−]")

OCCUPIED_HEADING = "наразі всі місця зайняті"
OCCUPIED_INSTRUCTION = "будь ласка, спробуйте в інший час або день"
SERVICE_LABEL = "послуга"
SELECT_PLACEHOLDER = "- обрати -"
RATE_LIMIT_MESSAGE = "too many requests, please try again later"
_SERVER_ERROR_VISIBLE_MARKERS: tuple[str, ...] = (
    "datetimezone",
    "500 internal server error",
    "виникла помилка",
    "0 - ",
)
_SERVER_ERROR_HTML_MARKERS: tuple[str, ...] = (
    "datetimezone::__construct",
    "datetimezone",
    "500 internal server error",
)

_CF_TITLE_MARKERS: tuple[str, ...] = (
    "just a moment",
    "checking your browser",
    "attention required",
    "трохи зачекайте",
)
_CF_BODY_MARKERS: tuple[str, ...] = (
    "триває перевірка безпеки",
    "підтвердьте, що ви людина",
)
_CF_HTML_MARKERS: tuple[str, ...] = (
    "cf-chl-widget",
)
_CF_URL_MARKERS: tuple[str, ...] = (
    "/cdn-cgi/challenge-platform",
)


@dataclass(frozen=True, slots=True)
class SlotPageEvidence:
    title: str
    url: str
    visible_text: str
    occupied_banner_visible: bool
    service_select_visible: bool
    select_placeholder_visible: bool
    tel_input_visible: bool
    challenge_visible: bool


def normalize_visible_text(value: str) -> str:
    normalized_dashes = _DASH_RE.sub("-", value.casefold())
    return _WHITESPACE_RE.sub(" ", normalized_dashes).strip()


def has_cloudflare_challenge(evidence: SlotPageEvidence) -> bool:
    title = normalize_visible_text(evidence.title)
    visible = normalize_visible_text(evidence.visible_text)
    url = evidence.url.casefold()
    return (
        evidence.challenge_visible
        or any(marker in title for marker in _CF_TITLE_MARKERS)
        or any(marker in visible for marker in _CF_BODY_MARKERS)
        or any(marker in url for marker in _CF_URL_MARKERS)
    )


def has_cloudflare_source(*, title: str = "", html: str = "") -> bool:
    normalized_title = normalize_visible_text(title)
    normalized_html = normalize_visible_text(html)
    return (
        any(marker in normalized_title for marker in _CF_TITLE_MARKERS)
        or any(marker in normalized_html for marker in _CF_TITLE_MARKERS)
        or any(marker in normalized_html for marker in _CF_BODY_MARKERS)
        or any(marker in normalized_html for marker in _CF_HTML_MARKERS)
    )


def has_rate_limit_message(evidence: SlotPageEvidence) -> bool:
    return RATE_LIMIT_MESSAGE in normalize_visible_text(evidence.visible_text)


def has_server_error_source(
    *,
    title: str = "",
    visible_text: str = "",
    html: str = "",
) -> bool:
    visible = " ".join(
        (
            normalize_visible_text(title),
            normalize_visible_text(visible_text),
        )
    )
    if any(marker in visible for marker in _SERVER_ERROR_VISIBLE_MARKERS):
        return True
    normalized_html = normalize_visible_text(html)
    return bool(normalized_html) and any(
        marker in normalized_html for marker in _SERVER_ERROR_HTML_MARKERS
    )


def has_server_error_page(evidence: SlotPageEvidence) -> bool:
    return has_server_error_source(
        title=evidence.title,
        visible_text=evidence.visible_text,
    )


def classify_slot_evidence(
    evidence: SlotPageEvidence,
    *,
    checked_at: datetime | None = None,
) -> SlotCheckResult:
    moment = checked_at or datetime.now(timezone.utc)
    if has_cloudflare_challenge(evidence):
        return SlotCheckResult(
            status=SlotStatus.UNKNOWN,
            checked_at=moment,
            error=ScraperFailureCode.CLOUDFLARE_CHALLENGE.value,
            failure_code=ScraperFailureCode.CLOUDFLARE_CHALLENGE,
            details="Cloudflare challenge page detected",
        )
    if has_server_error_page(evidence):
        return SlotCheckResult(
            status=SlotStatus.UNKNOWN,
            checked_at=moment,
            error=ScraperFailureCode.SERVER_ERROR.value,
            failure_code=ScraperFailureCode.SERVER_ERROR,
            details="site_backend_error",
        )

    visible_text = normalize_visible_text(evidence.visible_text)
    occupied_text_present = (
        OCCUPIED_HEADING in visible_text
        and OCCUPIED_INSTRUCTION in visible_text
    )
    occupied = evidence.occupied_banner_visible and occupied_text_present
    complete_form = (
        evidence.service_select_visible
        and evidence.select_placeholder_visible
        and evidence.tel_input_visible
    )

    if occupied and complete_form:
        return SlotCheckResult(
            status=SlotStatus.UNKNOWN,
            checked_at=moment,
            error=ScraperFailureCode.INCONCLUSIVE_PAGE.value,
            failure_code=ScraperFailureCode.INCONCLUSIVE_PAGE,
            details="conflicting visible occupied banner and booking form",
        )
    if occupied:
        return SlotCheckResult(
            status=SlotStatus.NO_SLOTS,
            checked_at=moment,
            details="visible occupied banner",
        )
    if complete_form:
        return SlotCheckResult(
            status=SlotStatus.FREE_SLOTS_AVAILABLE,
            checked_at=moment,
            details="visible service selector and telephone field",
        )
    return SlotCheckResult(
        status=SlotStatus.UNKNOWN,
        checked_at=moment,
        error=ScraperFailureCode.INCONCLUSIVE_PAGE.value,
        failure_code=ScraperFailureCode.INCONCLUSIVE_PAGE,
        details="page did not contain a complete visible slot-state signal",
    )
