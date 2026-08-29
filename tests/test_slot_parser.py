from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.core.models import ScraperFailureCode, SlotStatus
from src.services.slot_parser import (
    SlotPageEvidence,
    classify_slot_evidence,
    normalize_visible_text,
)

CHECKED_AT = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)


def evidence(
    *,
    title: str = "Електронна черга",
    url: str = "https://warszawa.pasport.org.ua/solutions/e-queue",
    visible_text: str = "",
    occupied_banner_visible: bool = False,
    service_select_visible: bool = False,
    select_placeholder_visible: bool = False,
    tel_input_visible: bool = False,
    challenge_visible: bool = False,
) -> SlotPageEvidence:
    return SlotPageEvidence(
        title=title,
        url=url,
        visible_text=visible_text,
        occupied_banner_visible=occupied_banner_visible,
        service_select_visible=service_select_visible,
        select_placeholder_visible=select_placeholder_visible,
        tel_input_visible=tel_input_visible,
        challenge_visible=challenge_visible,
    )


def test_visible_occupied_banner_is_no_slots() -> None:
    result = classify_slot_evidence(
        evidence(
            visible_text=(
                "Наразі всі місця зайняті.\n"
                "Будь ласка, спробуйте в інший час або день."
            ),
            occupied_banner_visible=True,
        ),
        checked_at=CHECKED_AT,
    )

    assert result.status is SlotStatus.NO_SLOTS
    assert result.error is None


def test_complete_visible_booking_form_is_free() -> None:
    result = classify_slot_evidence(
        evidence(
            visible_text="Послуга *\n- Обрати -\nТелефон",
            service_select_visible=True,
            select_placeholder_visible=True,
            tel_input_visible=True,
        ),
        checked_at=CHECKED_AT,
    )

    assert result.status is SlotStatus.FREE_SLOTS_AVAILABLE
    assert result.error is None


@pytest.mark.parametrize(
    ("service_select", "placeholder", "telephone"),
    [
        (False, True, True),
        (True, False, True),
        (True, True, False),
        (False, False, True),
    ],
)
def test_partial_form_is_unknown(
    service_select: bool,
    placeholder: bool,
    telephone: bool,
) -> None:
    result = classify_slot_evidence(
        evidence(
            visible_text="Послуга * - Обрати - Телефон",
            service_select_visible=service_select,
            select_placeholder_visible=placeholder,
            tel_input_visible=telephone,
        ),
        checked_at=CHECKED_AT,
    )

    assert result.status is SlotStatus.UNKNOWN
    assert result.failure_code is ScraperFailureCode.INCONCLUSIVE_PAGE


def test_hidden_banner_text_is_not_no_slots() -> None:
    result = classify_slot_evidence(
        evidence(
            visible_text=(
                "Наразі всі місця зайняті. "
                "Будь ласка, спробуйте в інший час або день."
            ),
            occupied_banner_visible=False,
        ),
        checked_at=CHECKED_AT,
    )

    assert result.status is SlotStatus.UNKNOWN


def test_visible_banner_without_complete_text_is_unknown() -> None:
    result = classify_slot_evidence(
        evidence(
            visible_text="Наразі всі місця зайняті.",
            occupied_banner_visible=True,
        ),
        checked_at=CHECKED_AT,
    )

    assert result.status is SlotStatus.UNKNOWN


def test_conflicting_visible_states_are_unknown() -> None:
    result = classify_slot_evidence(
        evidence(
            visible_text=(
                "Наразі всі місця зайняті. "
                "Будь ласка, спробуйте в інший час або день. "
                "Послуга * - Обрати - Телефон"
            ),
            occupied_banner_visible=True,
            service_select_visible=True,
            select_placeholder_visible=True,
            tel_input_visible=True,
        ),
        checked_at=CHECKED_AT,
    )

    assert result.status is SlotStatus.UNKNOWN
    assert result.failure_code is ScraperFailureCode.INCONCLUSIVE_PAGE
    assert "conflicting" in result.details


@pytest.mark.parametrize(
    "challenge_evidence",
    [
        evidence(title="Just a moment..."),
        evidence(
            url=(
                "https://warszawa.pasport.org.ua/cdn-cgi/challenge-platform/"
                "h/g/orchestrate"
            )
        ),
        evidence(challenge_visible=True),
    ],
)
def test_cloudflare_signals_are_unknown(
    challenge_evidence: SlotPageEvidence,
) -> None:
    result = classify_slot_evidence(
        challenge_evidence,
        checked_at=CHECKED_AT,
    )

    assert result.status is SlotStatus.UNKNOWN
    assert result.failure_code is ScraperFailureCode.CLOUDFLARE_CHALLENGE


def test_stale_cloudflare_query_token_does_not_override_visible_form() -> None:
    result = classify_slot_evidence(
        evidence(
            url=(
                "https://warszawa.pasport.org.ua/solutions/e-queue?"
                "__cf_chl_tk=stale"
            ),
            visible_text="Послуга * - Обрати - Телефон",
            service_select_visible=True,
            select_placeholder_visible=True,
            tel_input_visible=True,
        ),
        checked_at=CHECKED_AT,
    )

    assert result.status is SlotStatus.FREE_SLOTS_AVAILABLE


def test_normalizer_casefolds_whitespace_and_dash_variants() -> None:
    assert normalize_visible_text("  - ОБРАТИ —\n Послуга  ") == "- обрати - послуга"
