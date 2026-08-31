from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from src.core.models import (
    MonitorSnapshot,
    ScraperFailureCode,
    ScraperHealthSnapshot,
    ScraperHealthStatus,
    SlotCheckResult,
    SlotStatus,
)
from src.handlers.filters import AdminFilter
from src.handlers.texts import (
    RATE_LIMIT_PAUSE_TEXT,
    format_check_result,
    format_status,
)
from src.services.status import StatusService


class FakeMonitor:
    def __init__(
        self,
        snapshot: MonitorSnapshot,
        result: SlotCheckResult,
    ) -> None:
        self._snapshot = snapshot
        self._result = result
        self.check_calls = 0
        self.admin_ids: list[int] = []

    async def snapshot(self) -> MonitorSnapshot:
        return self._snapshot

    async def check_now(self, admin_id: int) -> SlotCheckResult:
        self.check_calls += 1
        self.admin_ids.append(admin_id)
        return self._result


class StatusFormattingTests(unittest.TestCase):
    def setUp(self) -> None:
        checked_at = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
        self.snapshot = MonitorSnapshot(
            last_check_at=checked_at,
            slot_state=SlotStatus.NO_SLOTS,
            last_details="target tab missing",
            last_error="target_tab_missing",
            active_subscribers=12,
            uptime_seconds=3600,
            city_name="Warsaw",
            target_url="https://warszawa.pasport.org.ua/solutions/e-queue",
            last_attempt_at=checked_at,
            last_verified_at=checked_at,
            scraper_health=ScraperHealthSnapshot(
                status=ScraperHealthStatus.NEEDS_HUMAN,
                cdp_connected=True,
                target_tab_present=False,
                updated_at=checked_at,
                failure_code=ScraperFailureCode.TARGET_TAB_MISSING,
                details="target tab missing",
            ),
        )

    def test_public_status_omits_operational_diagnostics(self) -> None:
        text = format_status(self.snapshot, is_admin=False)

        self.assertIn("Місто: Warsaw", text)
        self.assertIn("Стан слотів:", text)
        self.assertIn("Остання підтверджена перевірка:", text)
        self.assertNotIn("target_tab_missing", text)
        self.assertNotIn("CDP", text)
        self.assertNotIn("Активних підписників", text)

    def test_admin_status_includes_operational_diagnostics(self) -> None:
        text = format_status(self.snapshot, is_admin=True)

        self.assertIn("Стан scraper: NEEDS_HUMAN", text)
        self.assertIn("CDP підключено: так", text)
        self.assertIn("Цільова вкладка: ні", text)
        self.assertIn("target_tab_missing", text)
        self.assertIn("Активних підписників: 12", text)
        self.assertNotIn(RATE_LIMIT_PAUSE_TEXT, text)

    def test_status_reports_too_many_requests_circuit_breaker_pause(self) -> None:
        checked_at = self.snapshot.last_attempt_at
        snapshot = MonitorSnapshot(
            last_check_at=checked_at,
            slot_state=SlotStatus.UNKNOWN,
            last_details="too_many_requests",
            last_error="too_many_requests",
            active_subscribers=12,
            uptime_seconds=3600,
            city_name="Warsaw",
            target_url="https://warszawa.pasport.org.ua/solutions/e-queue",
            last_attempt_at=checked_at,
            last_verified_at=checked_at,
            scraper_health=ScraperHealthSnapshot(
                status=ScraperHealthStatus.DEGRADED,
                cdp_connected=True,
                target_tab_present=True,
                updated_at=checked_at,  # type: ignore[arg-type]
                failure_code=ScraperFailureCode.TOO_MANY_REQUESTS,
                details="too_many_requests",
            ),
            cooldown_until=checked_at,
        )

        public = format_status(snapshot, is_admin=False)
        admin = format_status(snapshot, is_admin=True)

        self.assertIn(RATE_LIMIT_PAUSE_TEXT, public)
        self.assertIn(RATE_LIMIT_PAUSE_TEXT, admin)
        self.assertIn("too_many_requests", admin)

    def test_status_hides_rate_limit_pause_after_cooldown(self) -> None:
        checked_at = self.snapshot.last_attempt_at
        snapshot = MonitorSnapshot(
            last_check_at=checked_at,
            slot_state=SlotStatus.UNKNOWN,
            last_details="too_many_requests",
            last_error="too_many_requests",
            active_subscribers=12,
            uptime_seconds=3600,
            city_name="Warsaw",
            target_url="https://warszawa.pasport.org.ua/solutions/e-queue",
            last_attempt_at=checked_at,
            last_verified_at=checked_at,
            scraper_health=ScraperHealthSnapshot(
                status=ScraperHealthStatus.DEGRADED,
                cdp_connected=True,
                target_tab_present=True,
                updated_at=checked_at,  # type: ignore[arg-type]
                failure_code=ScraperFailureCode.TOO_MANY_REQUESTS,
                details="too_many_requests",
            ),
            cooldown_until=None,
        )

        text = format_status(snapshot, is_admin=True)

        self.assertNotIn(RATE_LIMIT_PAUSE_TEXT, text)

    def test_manual_check_result_includes_unknown_error(self) -> None:
        text = format_check_result(
            SlotCheckResult(
                status=SlotStatus.UNKNOWN,
                checked_at=self.snapshot.last_attempt_at,  # type: ignore[arg-type]
                details="challenge visible",
                error="cloudflare_challenge",
            )
        )

        self.assertIn("Ручну перевірку завершено", text)
        self.assertIn("cloudflare_challenge", text)

    def test_manual_check_result_reports_too_many_requests(self) -> None:
        text = format_check_result(
            SlotCheckResult(
                status=SlotStatus.UNKNOWN,
                checked_at=self.snapshot.last_attempt_at,  # type: ignore[arg-type]
                details="too_many_requests",
                error="too_many_requests",
                failure_code=ScraperFailureCode.TOO_MANY_REQUESTS,
            )
        )

        self.assertIn(RATE_LIMIT_PAUSE_TEXT, text)
        self.assertIn("too_many_requests", text)


class HandlerAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_filter_uses_chat_id(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=42),
            from_user=SimpleNamespace(id=999),
        )

        allowed = await AdminFilter(frozenset({42}))(message)  # type: ignore[arg-type]

        self.assertTrue(allowed)

    async def test_status_service_forwards_manual_check(self) -> None:
        checked_at = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
        result = SlotCheckResult(
            status=SlotStatus.NO_SLOTS,
            checked_at=checked_at,
        )
        snapshot = MonitorSnapshot(
            last_check_at=checked_at,
            slot_state=SlotStatus.NO_SLOTS,
            last_details="",
            last_error=None,
            active_subscribers=0,
            uptime_seconds=1,
            city_name="Warsaw",
            target_url="https://example.test",
        )
        monitor = FakeMonitor(snapshot, result)
        service = StatusService(monitor)  # type: ignore[arg-type]

        returned = await service.check_now(admin_id=42)

        self.assertIs(returned, result)
        self.assertEqual(monitor.check_calls, 1)
        self.assertEqual(monitor.admin_ids, [42])


if __name__ == "__main__":
    unittest.main()
