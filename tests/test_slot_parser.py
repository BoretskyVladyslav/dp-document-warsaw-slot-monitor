from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.core.models import ScraperFailureCode, SlotStatus
from src.services.slot_parser import (
    SlotPageEvidence,
    classify_slot_evidence,
    has_cloudflare_source,
    has_rate_limit_message,
    has_server_error_page,
    has_server_error_source,
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
    service_option_selected: bool = False,
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
        service_option_selected=service_option_selected,
        challenge_visible=challenge_visible,
    )


def test_visible_occupied_banner_is_no_slots() -> None:
    result = classify_slot_evidence(
        evidence(
            visible_text="Вибачте, на даний момент всі місця зайняті!",
            occupied_banner_visible=True,
            service_select_visible=True,
            select_placeholder_visible=True,
            tel_input_visible=True,
            service_option_selected=True,
        ),
        checked_at=CHECKED_AT,
    )

    assert result.status is SlotStatus.NO_SLOTS
    assert result.error is None


def test_passive_booking_form_is_not_free() -> None:
    result = classify_slot_evidence(
        evidence(
            visible_text="Послуга *\n- Обрати -\nТелефон",
            service_select_visible=True,
            select_placeholder_visible=True,
            tel_input_visible=True,
        ),
        checked_at=CHECKED_AT,
    )

    assert result.status is SlotStatus.UNKNOWN
    assert result.failure_code is ScraperFailureCode.INCONCLUSIVE_PAGE


def test_selected_service_without_occupied_banner_is_free() -> None:
    result = classify_slot_evidence(
        evidence(
            visible_text="Послуга *\nЗакордонний паспорт та (або) ID-картка\nТелефон",
            service_select_visible=True,
            tel_input_visible=True,
            service_option_selected=True,
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
            visible_text="Вибачте, на даний момент всі місця зайняті!",
            occupied_banner_visible=False,
            service_option_selected=True,
        ),
        checked_at=CHECKED_AT,
    )

    assert result.status is SlotStatus.FREE_SLOTS_AVAILABLE


def test_visible_banner_without_occupied_text_is_not_no_slots() -> None:
    result = classify_slot_evidence(
        evidence(
            visible_text="Вибачте",
            occupied_banner_visible=True,
        ),
        checked_at=CHECKED_AT,
    )

    assert result.status is SlotStatus.UNKNOWN
    assert result.failure_code is ScraperFailureCode.INCONCLUSIVE_PAGE


def test_occupied_banner_with_visible_form_is_no_slots() -> None:
    result = classify_slot_evidence(
        evidence(
            visible_text=(
                "Вибачте, на даний момент всі місця зайняті! "
                "Послуга * - Обрати - Телефон"
            ),
            occupied_banner_visible=True,
            service_select_visible=True,
            select_placeholder_visible=True,
            tel_input_visible=True,
            service_option_selected=True,
        ),
        checked_at=CHECKED_AT,
    )

    assert result.status is SlotStatus.NO_SLOTS
    assert result.error is None


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
        evidence(title="Трохи зачекайте..."),
        evidence(visible_text="Триває перевірка безпеки"),
        evidence(visible_text="Підтвердьте, що ви людина"),
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


def test_ukrainian_turnstile_iframe_is_detected_in_html() -> None:
    assert has_cloudflare_source(
        title="Електронна черга",
        html='<iframe src="https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/cf-chl-widget/abc"></iframe>',
    )
    assert has_cloudflare_source(title="Трохи зачекайте...")


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
            service_option_selected=True,
        ),
        checked_at=CHECKED_AT,
    )

    assert result.status is SlotStatus.FREE_SLOTS_AVAILABLE


def test_rate_limit_message_is_detected_case_insensitively() -> None:
    assert has_rate_limit_message(
        evidence(visible_text="TOO MANY REQUESTS,\nplease try again later")
    )


@pytest.mark.parametrize(
    "visible_text",
    [
        "DateTimeZone::__construct(): Unknown or bad timezone ()",
        "500 Internal Server Error",
        "Виникла помилка при обробці вашого запиту",
        "Виникла помилка при обробці",
        "Виникла помилка",
    ],
)
def test_backend_error_pages_are_transient_server_error(visible_text: str) -> None:
    page = evidence(visible_text=visible_text)
    assert has_server_error_page(page)
    result = classify_slot_evidence(page, checked_at=CHECKED_AT)
    assert result.status is SlotStatus.UNKNOWN
    assert result.failure_code is ScraperFailureCode.SERVER_ERROR
    assert result.details == "site_backend_error"


def test_backend_error_is_detected_in_title_and_html_source() -> None:
    assert has_server_error_page(evidence(title="0 - "))
    assert has_server_error_source(
        title="Електронна черга",
        html="<pre>DateTimeZone::__construct(): Unknown or bad timezone ()</pre>",
    )
    assert has_server_error_source(title="Виникла помилка", html="")
    assert not has_server_error_source(
        title="Електронна черга",
        html="<script>window.msg='Виникла помилка у шаблоні'</script>",
    )


def test_normalizer_casefolds_whitespace_and_dash_variants() -> None:
    assert normalize_visible_text("  - ОБРАТИ —\n Послуга  ") == "- обрати - послуга"
