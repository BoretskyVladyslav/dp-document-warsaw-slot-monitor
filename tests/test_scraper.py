from __future__ import annotations

import sys
import unittest

from src.core.config import Settings
from src.services.scraper import (
    chrome_launch_args,
    has_cf_clearance_cookie,
    is_target_closed_error,
    is_turnstile_frame_url,
    persistent_context_kwargs,
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

    def test_headed_launch_args_are_clean(self) -> None:
        settings = Settings(
            bot_token="1234567890:TESTTOKENVALUE",
            target_url="https://warszawa.pasport.org.ua/solutions/e-queue",
        )
        kwargs = persistent_context_kwargs(settings, "data/browser_profile", headless=False)
        self.assertEqual(kwargs["ignore_default_args"], ["--enable-automation"])
        self.assertTrue(kwargs["no_viewport"])
        self.assertEqual(kwargs["args"], ["--lang=uk-UA,uk"])
        banned = {
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-infobars",
            "--disable-blink-features=AutomationControlled",
        }
        self.assertTrue(banned.isdisjoint(kwargs["args"]))

    def test_linux_headless_keeps_container_sandbox_flags(self) -> None:
        args = chrome_launch_args(headless=True)
        if sys.platform == "win32":
            self.assertEqual(args, ["--lang=uk-UA,uk"])
            return
        self.assertIn("--no-sandbox", args)
        self.assertIn("--disable-dev-shm-usage", args)
        self.assertNotIn("--disable-infobars", args)


if __name__ == "__main__":
    unittest.main()
