from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.core.config import Settings
from src.services.scraper import worker_context_kwargs
from src.services.storage_state import (
    build_storage_state,
    cookie_domain_from_url,
    is_usable_storage_state,
    parse_cookie_pairs,
    write_storage_state_file,
)


class StorageStateTests(unittest.TestCase):
    def test_domain_from_target_url(self) -> None:
        self.assertEqual(
            cookie_domain_from_url("https://warszawa.pasport.org.ua/solutions/e-queue"),
            "warszawa.pasport.org.ua",
        )

    def test_build_cf_clearance_only(self) -> None:
        payload = build_storage_state(
            cf_clearance="clear-token",
            extra_pairs=[],
            domain="warszawa.pasport.org.ua",
        )
        self.assertEqual(payload["origins"], [])
        cookie = payload["cookies"][0]
        self.assertEqual(cookie["name"], "cf_clearance")
        self.assertEqual(cookie["value"], "clear-token")
        self.assertEqual(cookie["domain"], "warszawa.pasport.org.ua")
        self.assertEqual(cookie["path"], "/")
        self.assertTrue(cookie["httpOnly"])
        self.assertTrue(cookie["secure"])
        self.assertEqual(cookie["sameSite"], "None")

    def test_preserves_padding_in_raw_clearance_value(self) -> None:
        payload = build_storage_state(
            cf_clearance="abc==",
            extra_pairs=[],
            domain="warszawa.pasport.org.ua",
        )
        self.assertEqual(payload["cookies"][0]["value"], "abc==")

    def test_accepts_prefixed_clearance_and_extra_pairs(self) -> None:
        payload = build_storage_state(
            cf_clearance="cf_clearance=abc",
            extra_pairs=parse_cookie_pairs("session=1; other=2"),
            domain="warszawa.pasport.org.ua",
        )
        names = [item["name"] for item in payload["cookies"]]
        self.assertEqual(names, ["cf_clearance", "session", "other"])
        self.assertEqual(payload["cookies"][0]["value"], "abc")

    def test_skips_duplicate_cf_clearance_in_extras(self) -> None:
        payload = build_storage_state(
            cf_clearance="abc",
            extra_pairs=[("cf_clearance", "ignored"), ("sid", "1")],
            domain="warszawa.pasport.org.ua",
        )
        self.assertEqual([item["name"] for item in payload["cookies"]], ["cf_clearance", "sid"])
        self.assertEqual(payload["cookies"][0]["value"], "abc")

    def test_rejects_empty_clearance(self) -> None:
        with self.assertRaises(ValueError):
            build_storage_state(cf_clearance="  ", extra_pairs=[], domain="example.test")

    def test_parse_rejects_line_without_equals(self) -> None:
        with self.assertRaises(ValueError):
            parse_cookie_pairs("not-a-cookie")

    def test_worker_skips_unreadable_storage_state(self) -> None:
        settings = Settings(
            bot_token="1234567890:TESTTOKENVALUE",
            target_url="https://warszawa.pasport.org.ua/solutions/e-queue",
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "storage_state.json"
            path.write_text("{not-json", encoding="utf-8")
            self.assertFalse(is_usable_storage_state(path))
            kwargs = worker_context_kwargs(settings, path)
            self.assertNotIn("storage_state", kwargs)

    def test_worker_loads_written_storage_state(self) -> None:
        settings = Settings(
            bot_token="1234567890:TESTTOKENVALUE",
            target_url="https://warszawa.pasport.org.ua/solutions/e-queue",
        )
        payload = build_storage_state(
            cf_clearance="token",
            extra_pairs=[],
            domain="warszawa.pasport.org.ua",
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "storage_state.json"
            write_storage_state_file(path, payload)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["cookies"][0]["name"], "cf_clearance")
            self.assertTrue(is_usable_storage_state(path))
            kwargs = worker_context_kwargs(settings, path)
            self.assertEqual(kwargs["storage_state"], str(path))


if __name__ == "__main__":
    unittest.main()
