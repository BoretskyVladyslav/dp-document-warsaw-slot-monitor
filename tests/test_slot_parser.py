from __future__ import annotations

import unittest
from datetime import datetime, timezone

from src.services.slot_parser import parse_slot_page
from src.core.models import SlotStatus


class ParseSlotPageTests(unittest.TestCase):
    def test_no_slots_phrase(self) -> None:
        result = parse_slot_page(
            html="<div>На жаль, немає вільних дат для запису</div>",
            title="Електронна черга",
            json_payloads=[],
            checked_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
        )
        self.assertEqual(result.status, SlotStatus.NO_SLOTS)

    def test_available_data_date(self) -> None:
        result = parse_slot_page(
            html='<button data-date="2099-09-10">10</button>',
            title="Queue",
            json_payloads=[],
            checked_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
        )
        self.assertEqual(result.status, SlotStatus.FREE_SLOTS_AVAILABLE)
        self.assertIn("2099-09-10", result.slots)

    def test_json_slots(self) -> None:
        result = parse_slot_page(
            html="<html></html>",
            title="Queue",
            json_payloads=[{"slots": [{"date": "2099-10-01", "time": "10:00", "available": True}]}],
            checked_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
        )
        self.assertEqual(result.status, SlotStatus.FREE_SLOTS_AVAILABLE)
        self.assertEqual(result.slots[0], "2099-10-01 10:00")

    def test_empty_json_slots_is_no_slots(self) -> None:
        result = parse_slot_page(
            html="<html></html>",
            title="Queue",
            json_payloads=[{"slots": []}],
            checked_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
        )
        self.assertEqual(result.status, SlotStatus.NO_SLOTS)

    def test_cloudflare_is_unknown(self) -> None:
        result = parse_slot_page(
            html="<div id='challenge-platform'>Just a moment...</div>",
            title="Just a moment...",
            json_payloads=[],
            checked_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
        )
        self.assertEqual(result.status, SlotStatus.UNKNOWN)
        self.assertEqual(result.error, "cloudflare_challenge")

    def test_cf_chl_token_is_challenge(self) -> None:
        result = parse_slot_page(
            html='<script>window._cf_chl = 1; __cf_chl_tk="x"</script>',
            title="Queue",
            json_payloads=[],
            checked_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
        )
        self.assertEqual(result.status, SlotStatus.UNKNOWN)
        self.assertEqual(result.error, "cloudflare_challenge")

    def test_turnstile_iframe_alone_is_not_challenge(self) -> None:
        result = parse_slot_page(
            html='<iframe src="https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile"></iframe><p>немає вільних дат</p>',
            title="Електронна черга",
            json_payloads=[],
            checked_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
        )
        self.assertEqual(result.status, SlotStatus.NO_SLOTS)

    def test_is_not_disabled_calendar_day(self) -> None:
        result = parse_slot_page(
            html='<td class="vc-day is-not-disabled" data-date="2099-12-15">15</td>',
            title="Queue",
            json_payloads=[],
            checked_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
        )
        self.assertEqual(result.status, SlotStatus.FREE_SLOTS_AVAILABLE)
        self.assertIn("2099-12-15", result.slots)

    def test_data_available_inner_date(self) -> None:
        result = parse_slot_page(
            html='<button data-available="true">2099-08-20 09:30</button>',
            title="Queue",
            json_payloads=[],
            checked_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
        )
        self.assertEqual(result.status, SlotStatus.FREE_SLOTS_AVAILABLE)
        self.assertTrue(any("2099-08-20" in item for item in result.slots))

    def test_data_available_disabled_ignored(self) -> None:
        result = parse_slot_page(
            html='<button disabled data-available="true">2099-08-20 09:30</button>',
            title="Queue",
            json_payloads=[],
            checked_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
        )
        self.assertNotEqual(result.status, SlotStatus.FREE_SLOTS_AVAILABLE)
        self.assertFalse(any("2099-08-20" in item for item in result.slots))

    def test_data_available_aria_disabled_ignored(self) -> None:
        result = parse_slot_page(
            html=(
                '<button aria-disabled="true" data-available="true">2099-08-20 09:30</button>'
                '<button data-available="true">2099-08-21 10:00</button>'
            ),
            title="Queue",
            json_payloads=[],
            checked_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
        )
        self.assertEqual(result.status, SlotStatus.FREE_SLOTS_AVAILABLE)
        self.assertFalse(any("2099-08-20" in item for item in result.slots))
        self.assertTrue(any("2099-08-21" in item for item in result.slots))

    def test_calendar_available_class(self) -> None:
        result = parse_slot_page(
            html='<td class="calendar-day is-available" data-date="2099-12-01">1</td>',
            title="Queue",
            json_payloads=[],
            checked_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
        )
        self.assertEqual(result.status, SlotStatus.FREE_SLOTS_AVAILABLE)
        self.assertIn("2099-12-01", result.slots)

    def test_no_slots_places_phrase(self) -> None:
        result = parse_slot_page(
            html="<p>На жаль, немає вільних місць</p>",
            title="Черга",
            json_payloads=[],
            checked_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
        )
        self.assertEqual(result.status, SlotStatus.NO_SLOTS)


if __name__ == "__main__":
    unittest.main()
