from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from src.core.models import ScraperFailureCode, SlotCheckResult, SlotStatus

_WHITESPACE_RE = re.compile(r"\s+")
_DASH_RE = re.compile(r"[–—−]")

OCCUPIED_HEADING = "вибачте, на даний момент всі місця зайняті!"
SERVICE_LABEL = "послуга"
SELECT_PLACEHOLDER = "- обрати -"
TOO_MANY_REQUESTS_PHRASE = "too many requests, please try again later!"
RATE_LIMIT_MARKERS: tuple[str, ...] = (
    TOO_MANY_REQUESTS_PHRASE,
    "too many requests, please try again later",
    "too many requests",
)
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

CF_TITLE_MARKERS: tuple[str, ...] = (
    "just a moment",
    "трохи зачекайте",
    "waiting room",
)
CF_BODY_MARKERS: tuple[str, ...] = (
    "триває перевірка безпеки",
    "підтвердьте, що ви людина",
    "вас додано до черги",
    "waiting room",
    "орієнтовний час очікування",
    "віртуальну чергу",
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
    service_option_selected: bool
    challenge_visible: bool


def normalize_visible_text(value: str) -> str:
    normalized_dashes = _DASH_RE.sub("-", value.casefold())
    return _WHITESPACE_RE.sub(" ", normalized_dashes).strip()


def has_cloudflare_challenge(evidence: SlotPageEvidence) -> bool:
    title = normalize_visible_text(evidence.title)
    visible = normalize_visible_text(evidence.visible_text)
    return (
        any(marker in title for marker in CF_TITLE_MARKERS)
        or any(marker in visible for marker in CF_BODY_MARKERS)
    )


def has_cloudflare_source(*, title: str = "", html: str = "") -> bool:
    normalized_title = normalize_visible_text(title)
    normalized_html = normalize_visible_text(html)
    return (
        any(marker in normalized_title for marker in CF_TITLE_MARKERS)
        or any(marker in normalized_html for marker in CF_TITLE_MARKERS)
        or any(marker in normalized_html for marker in CF_BODY_MARKERS)
    )


def has_rate_limit_source(
    *,
    title: str = "",
    visible_text: str = "",
    html: str = "",
) -> bool:
    blob = " ".join(
        (
            normalize_visible_text(title),
            normalize_visible_text(visible_text),
            normalize_visible_text(html),
        )
    )
    return any(marker in blob for marker in RATE_LIMIT_MARKERS)


def has_rate_limit_message(evidence: SlotPageEvidence) -> bool:
    return has_rate_limit_source(
        title=evidence.title,
        visible_text=evidence.visible_text,
    )


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
    if has_rate_limit_message(evidence):
        return SlotCheckResult(
            status=SlotStatus.UNKNOWN,
            checked_at=moment,
            error=ScraperFailureCode.TOO_MANY_REQUESTS.value,
            failure_code=ScraperFailureCode.TOO_MANY_REQUESTS,
            details="too_many_requests",
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
    occupied = (
        evidence.occupied_banner_visible
        and OCCUPIED_HEADING in visible_text
    )

    if occupied:
        return SlotCheckResult(
            status=SlotStatus.NO_SLOTS,
            checked_at=moment,
            details="visible occupied banner",
        )
    if evidence.service_option_selected:
        return SlotCheckResult(
            status=SlotStatus.FREE_SLOTS_AVAILABLE,
            checked_at=moment,
            details="service selected without occupied banner",
        )
    return SlotCheckResult(
        status=SlotStatus.UNKNOWN,
        checked_at=moment,
        error=ScraperFailureCode.INCONCLUSIVE_PAGE.value,
        failure_code=ScraperFailureCode.INCONCLUSIVE_PAGE,
        details="page did not contain a complete visible slot-state signal",
    )
