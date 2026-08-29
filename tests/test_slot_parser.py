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


if __name__ == "__main__":
    unittest.main()
