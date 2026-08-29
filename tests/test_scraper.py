from __future__ import annotations

import unittest

from src.services.scraper import is_target_closed_error, is_turnstile_frame_url


class TargetClosedError(Exception):
    pass


class ScraperHelperTests(unittest.TestCase):
    def test_turnstile_frame_urls(self) -> None:
        self.assertTrue(
            is_turnstile_frame_url(
                "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile"
            )
        )
        self.assertTrue(is_turnstile_frame_url("https://example.com/widget?turnstile=1"))
        self.assertFalse(is_turnstile_frame_url("https://warszawa.pasport.org.ua/solutions/e-queue"))

    def test_target_closed_by_type_and_message(self) -> None:
        self.assertTrue(is_target_closed_error(TargetClosedError("BrowserContext.new_page")))
        self.assertTrue(
            is_target_closed_error(
                RuntimeError("BrowserContext.new_page: Target page, context or browser has been closed")
            )
        )
        self.assertFalse(is_target_closed_error(RuntimeError("net::ERR_CONNECTION_RESET")))


if __name__ == "__main__":
    unittest.main()
