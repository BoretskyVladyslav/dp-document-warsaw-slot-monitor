from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from src.core.config import Settings
from src.services.scraper import (
    chrome_launch_args,
    has_cf_clearance_cookie,
    is_target_closed_error,
    resolve_repo_path,
    worker_context_kwargs,
)


class TargetClosedError(Exception):
    pass


class ScraperHelperTests(unittest.TestCase):
    def test_target_closed_by_type_and_message(self) -> None:
        self.assertTrue(is_target_closed_error(TargetClosedError("BrowserContext.new_page")))
        self.assertTrue(
            is_target_closed_error(
                RuntimeError("BrowserContext.new_page: Target page, context or browser has been closed")
            )
        )
        self.assertFalse(is_target_closed_error(RuntimeError("net::ERR_CONNECTION_RESET")))

    def test_relative_storage_path_resolves_under_repo(self) -> None:
        resolved = resolve_repo_path("data/storage_state.json")
        self.assertTrue(resolved.is_absolute())
        self.assertEqual(resolved.name, "storage_state.json")
        self.assertEqual(resolved.parent.name, "data")

    def test_cf_clearance_cookie_detection(self) -> None:
        self.assertTrue(has_cf_clearance_cookie([{"name": "cf_clearance", "value": "x"}]))
        self.assertFalse(has_cf_clearance_cookie([{"name": "__cf_bm", "value": "x"}]))

    def test_headed_launch_args_are_clean(self) -> None:
        settings = Settings(
            bot_token="1234567890:TESTTOKENVALUE",
            target_url="https://warszawa.pasport.org.ua/solutions/e-queue",
        )
        missing = Path(tempfile.gettempdir()) / "missing-storage-state.json"
        kwargs = worker_context_kwargs(settings, missing)
        self.assertNotIn("storage_state", kwargs)
        self.assertTrue(kwargs.get("no_viewport") or settings.headless)
        banned = {
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-infobars",
        }
        self.assertTrue(banned.isdisjoint(chrome_launch_args(headless=False)))
        self.assertEqual(chrome_launch_args(headless=False), ["--lang=uk-UA,uk"])

    def test_worker_context_uses_storage_state_when_file_exists(self) -> None:
        settings = Settings(
            bot_token="1234567890:TESTTOKENVALUE",
            target_url="https://warszawa.pasport.org.ua/solutions/e-queue",
        )
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as handle:
            path = Path(handle.name)
            handle.write(b'{"cookies":[{"name":"cf_clearance","value":"x"}],"origins":[]}')
        try:
            kwargs = worker_context_kwargs(settings, path)
            self.assertEqual(kwargs["storage_state"], str(path))
        finally:
            path.unlink(missing_ok=True)

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
