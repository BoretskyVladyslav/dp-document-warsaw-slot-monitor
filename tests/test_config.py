from __future__ import annotations

import unittest

from src.core.config import Settings


class SettingsTests(unittest.TestCase):
    def test_parses_comma_separated_admin_ids(self) -> None:
        settings = Settings(
            bot_token="1234567890:TESTTOKENVALUE",
            admin_ids="10, 20,30",
            target_url="https://warszawa.pasport.org.ua/solutions/e-queue",
            _env_file=None,
        )
        self.assertEqual(settings.admin_ids, [10, 20, 30])
        self.assertEqual(settings.check_interval_seconds, 600)

    def test_parses_single_admin_id_as_list(self) -> None:
        settings = Settings(
            bot_token="1234567890:TESTTOKENVALUE",
            admin_ids=42,
            target_url="https://warszawa.pasport.org.ua/solutions/e-queue",
        )
        self.assertEqual(settings.admin_ids, [42])

    def test_defaults_for_strict_cdp_mode(self) -> None:
        settings = Settings(
            bot_token="1234567890:TESTTOKENVALUE",
            admin_ids="",
            target_url="https://warszawa.pasport.org.ua/solutions/e-queue",
            _env_file=None,
        )
        self.assertEqual(settings.admin_ids, [])
        self.assertIsNone(settings.cdp_url)
        self.assertFalse(settings.check_once)

    def test_empty_cdp_url_becomes_none(self) -> None:
        settings = Settings(
            bot_token="1234567890:TESTTOKENVALUE",
            target_url="https://warszawa.pasport.org.ua/solutions/e-queue",
            cdp_url="",
            _env_file=None,
        )
        self.assertIsNone(settings.cdp_url)

    def test_cdp_url_is_kept(self) -> None:
        settings = Settings(
            bot_token="1234567890:TESTTOKENVALUE",
            target_url="https://warszawa.pasport.org.ua/solutions/e-queue",
            cdp_url="http://localhost:9222",
            _env_file=None,
        )
        self.assertEqual(settings.cdp_url, "http://localhost:9222")


if __name__ == "__main__":
    unittest.main()
