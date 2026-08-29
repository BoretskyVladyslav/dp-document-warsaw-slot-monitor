from __future__ import annotations

import unittest

from src.core.config import Settings


class SettingsTests(unittest.TestCase):
    def test_parses_comma_separated_admin_ids(self) -> None:
        settings = Settings(
            bot_token="1234567890:TESTTOKENVALUE",
            admin_ids="10, 20,30",
            target_url="https://warszawa.pasport.org.ua/solutions/e-queue",
        )
        self.assertEqual(settings.admin_ids, [10, 20, 30])
        self.assertEqual(settings.check_interval_seconds, 180)
        self.assertTrue(settings.headless)

    def test_parses_single_admin_id_as_list(self) -> None:
        settings = Settings(
            bot_token="1234567890:TESTTOKENVALUE",
            admin_ids=42,
            target_url="https://warszawa.pasport.org.ua/solutions/e-queue",
        )
        self.assertEqual(settings.admin_ids, [42])

    def test_headless_false_from_bool(self) -> None:
        settings = Settings(
            bot_token="1234567890:TESTTOKENVALUE",
            target_url="https://warszawa.pasport.org.ua/solutions/e-queue",
            headless=False,
        )
        self.assertFalse(settings.headless)

    def test_empty_proxy_becomes_none(self) -> None:
        settings = Settings(
            bot_token="1234567890:TESTTOKENVALUE",
            admin_ids="",
            target_url="https://warszawa.pasport.org.ua/solutions/e-queue",
            proxy_url="",
        )
        self.assertEqual(settings.admin_ids, [])
        self.assertIsNone(settings.proxy_url)


if __name__ == "__main__":
    unittest.main()
