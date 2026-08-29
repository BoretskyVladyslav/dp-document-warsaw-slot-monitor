from __future__ import annotations

import unittest

from src.services.scraper import (
    has_cf_clearance_cookie,
    is_target_closed_error,
    is_turnstile_frame_url,
    resolve_browser_profile_dir,
)


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

    def test_relative_profile_resolves_under_repo(self) -> None:
        resolved = resolve_browser_profile_dir("data/browser_profile")
        self.assertTrue(resolved.is_absolute())
        self.assertEqual(resolved.name, "browser_profile")
        self.assertEqual(resolved.parent.name, "data")

    def test_cf_clearance_cookie_detection(self) -> None:
        self.assertTrue(has_cf_clearance_cookie([{"name": "cf_clearance", "value": "x"}]))
        self.assertFalse(has_cf_clearance_cookie([{"name": "__cf_bm", "value": "x"}]))


if __name__ == "__main__":
    unittest.main()
