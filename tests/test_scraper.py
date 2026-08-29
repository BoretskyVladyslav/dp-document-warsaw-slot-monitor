from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from src.core.config import Settings
from src.services.scraper import (
    SlotScraper,
    chrome_launch_args,
    has_cf_clearance_cookie,
    is_target_closed_error,
    normalize_cdp_url,
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


class FakeBrowser:
    def __init__(self) -> None:
        self.closed = False
        self._connected = True
        self.contexts: list[object] = []

    def is_connected(self) -> bool:
        return self._connected

    async def close(self) -> None:
        self.closed = True
        self._connected = False


class CdpLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def test_normalize_cdp_url(self) -> None:
        self.assertIsNone(normalize_cdp_url(None))
        self.assertIsNone(normalize_cdp_url(""))
        self.assertIsNone(normalize_cdp_url("  "))
        self.assertEqual(normalize_cdp_url(" http://localhost:9222 "), "http://localhost:9222")

    async def test_stop_does_not_close_cdp_browser(self) -> None:
        settings = Settings(
            bot_token="1234567890:TESTTOKENVALUE",
            target_url="https://warszawa.pasport.org.ua/solutions/e-queue",
            cdp_url="http://localhost:9222",
            _env_file=None,
        )
        scraper = SlotScraper(settings)
        browser = FakeBrowser()
        scraper._browser = browser  # type: ignore[assignment]
        scraper._owns_browser = False
        scraper._cdp_attached = True
        await scraper.stop()
        self.assertFalse(browser.closed)
        self.assertIsNone(scraper._browser)
        self.assertFalse(scraper._cdp_attached)

    async def test_stop_closes_owned_browser(self) -> None:
        settings = Settings(
            bot_token="1234567890:TESTTOKENVALUE",
            target_url="https://warszawa.pasport.org.ua/solutions/e-queue",
            _env_file=None,
        )
        scraper = SlotScraper(settings)
        browser = FakeBrowser()
        scraper._browser = browser  # type: ignore[assignment]
        scraper._owns_browser = True
        scraper._cdp_attached = False
        await scraper.stop()
        self.assertTrue(browser.closed)
        self.assertIsNone(scraper._browser)

    async def test_cdp_open_page_does_not_close_user_context(self) -> None:
        class Ctx:
            def __init__(self) -> None:
                self.pages: list[object] = ["existing-tab"]

            async def new_page(self) -> object:
                page = object()
                self.pages.append(page)
                return page

        settings = Settings(
            bot_token="1234567890:TESTTOKENVALUE",
            target_url="https://warszawa.pasport.org.ua/solutions/e-queue",
            cdp_url="http://localhost:9222",
            _env_file=None,
        )
        scraper = SlotScraper(settings)
        context = Ctx()
        browser = FakeBrowser()
        browser.contexts = [context]
        scraper._browser = browser  # type: ignore[assignment]
        scraper._cdp_attached = True
        opened_context, _page, close_page, close_context = await scraper._open_worker_page()
        self.assertIs(opened_context, context)
        self.assertTrue(close_page)
        self.assertFalse(close_context)
        self.assertEqual(len(context.pages), 2)


if __name__ == "__main__":
    unittest.main()
