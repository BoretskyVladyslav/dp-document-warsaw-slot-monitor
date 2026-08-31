from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from src.core.config import Settings
from src.core.exceptions import (
    CloudflareChallengeError,
    HumanActionRequiredError,
    RateLimitException,
    ScraperError,
)
from src.core.models import (
    ScraperFailureCode,
    ScraperHealthSnapshot,
    ScraperHealthStatus,
    SlotCheckResult,
    SlotStatus,
)
from src.database.connection import Database
from src.database.monitor_state import get_monitor_state
from src.database.schema import init_schema
from src.services.monitor import (
    SlotMonitor,
    _CYCLE_TIMEOUT_SECONDS,
    _jittered_check_delay,
    _rate_limit_cooldown_seconds,
)
from src.services.scraper import (
    _CDP_CONNECT_TIMEOUT_MS,
    _CDP_RELOAD_TIMEOUT_MS,
    _CF_MANAGED_CHALLENGE_HOLD_MS,
    _DOM_SIGNAL_TIMEOUT_MS,
    _QUEUE_UI_NETWORKIDLE_MS,
    _QUEUE_UI_WAIT_MS,
    _SERVICE_OPTION_ATTACH_TIMEOUT_MS,
    _SERVICE_SELECT_TIMEOUT_MS,
    _SERVICE_VALIDATE_RESPONSE_TIMEOUT_MS,
)

TARGET_URL = "https://warszawa.pasport.org.ua/solutions/e-queue"


class BlockingScraper:
    def __init__(
        self,
        result: SlotCheckResult | None = None,
        error: ScraperError | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.arm_hard_reload_calls = 0

    async def check_availability(self) -> SlotCheckResult:
        self.calls += 1
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result

    async def get_health_snapshot(self) -> ScraperHealthSnapshot:
        return ScraperHealthSnapshot(
            status=ScraperHealthStatus.READY,
            cdp_connected=True,
            target_tab_present=True,
            updated_at=datetime.now(timezone.utc),
        )

    async def arm_hard_reload(self) -> None:
        self.arm_hard_reload_calls += 1


class FakeNotifier:
    def __init__(self) -> None:
        self.verified: list[SlotCheckResult] = []
        self.incidents: list[HumanActionRequiredError] = []
        self.rate_limits: list[tuple[RateLimitException, datetime, datetime]] = []
        self.server_errors: list[SlotCheckResult] = []
        self.circuit_breakers: list[int] = []
        self.drain_calls = 0

    async def handle_verified_result(self, result: SlotCheckResult) -> bool:
        self.verified.append(result)
        return True

    async def handle_human_action_required(
        self,
        error: HumanActionRequiredError,
        *,
        attempted_at: datetime,
    ) -> bool:
        del attempted_at
        self.incidents.append(error)
        return True

    async def handle_rate_limit(
        self,
        error: RateLimitException,
        *,
        attempted_at: datetime,
        cooldown_until: datetime,
    ) -> bool:
        self.rate_limits.append((error, attempted_at, cooldown_until))
        return True

    async def handle_server_error(self, result: SlotCheckResult) -> bool:
        self.server_errors.append(result)
        return True

    async def handle_circuit_breaker(
        self,
        *,
        attempted_at: datetime,
        consecutive_failures: int,
        next_interval_seconds: int,
    ) -> bool:
        del attempted_at, next_interval_seconds
        self.circuit_breakers.append(consecutive_failures)
        return True

    async def drain_outbox(self) -> object:
        self.drain_calls += 1
        return object()


class SlotMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._tmp.name) / "monitor.db"))
        await self.db.connect()
        await init_schema(self.db.connection)
        self.settings = Settings(
            bot_token="1234567890:TESTTOKENVALUE",
            target_url=TARGET_URL,
            city_name="Warsaw",
            cdp_url="http://127.0.0.1:9222",
            _env_file=None,
        )
        self.checked_at = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._tmp.cleanup()

    def _monitor(
        self,
        scraper: BlockingScraper,
        notifier: FakeNotifier,
    ) -> SlotMonitor:
        return SlotMonitor(
            settings=self.settings,
            database=self.db,
            scraper=scraper,  # type: ignore[arg-type]
            notifier=notifier,  # type: ignore[arg-type]
            started_at=self.checked_at,
        )

    def test_negative_jitter_respects_minimum_interval(self) -> None:
        with patch("src.services.monitor.random.randint", return_value=-15):
            self.assertEqual(_jittered_check_delay(15), 15)
            self.assertEqual(_jittered_check_delay(300), 285)

    def test_rate_limit_cooldown_doubles_and_caps_at_two_hours(self) -> None:
        self.assertEqual(_rate_limit_cooldown_seconds(1), 900)
        self.assertEqual(_rate_limit_cooldown_seconds(2), 1800)
        self.assertEqual(_rate_limit_cooldown_seconds(3), 3600)
        self.assertEqual(_rate_limit_cooldown_seconds(4), 7200)
        self.assertEqual(_rate_limit_cooldown_seconds(5), 7200)

    def test_cycle_timeout_covers_post_cf_ui_wait_budget(self) -> None:
        budget_ms = (
            _CDP_CONNECT_TIMEOUT_MS
            + _CDP_RELOAD_TIMEOUT_MS
            + _CF_MANAGED_CHALLENGE_HOLD_MS
            + _QUEUE_UI_WAIT_MS
            + _QUEUE_UI_NETWORKIDLE_MS
            + _QUEUE_UI_WAIT_MS
            + _SERVICE_OPTION_ATTACH_TIMEOUT_MS
            + _SERVICE_SELECT_TIMEOUT_MS
            + max(
                _SERVICE_VALIDATE_RESPONSE_TIMEOUT_MS,
                _SERVICE_SELECT_TIMEOUT_MS,
            )
            + _DOM_SIGNAL_TIMEOUT_MS
        )
        self.assertGreaterEqual(_CYCLE_TIMEOUT_SECONDS * 1000, budget_ms)
        self.assertEqual(_CYCLE_TIMEOUT_SECONDS, 180.0)

    async def test_scheduled_and_manual_checks_share_one_inflight_task(self) -> None:
        result = SlotCheckResult(
            status=SlotStatus.NO_SLOTS,
            checked_at=self.checked_at,
            details="visible occupied banner",
        )
        scraper = BlockingScraper(result=result)
        notifier = FakeNotifier()
        monitor = self._monitor(scraper, notifier)

        scheduled = asyncio.create_task(monitor.run_once())
        await scraper.entered.wait()
        manual = asyncio.create_task(monitor.check_now(admin_id=1))
        await asyncio.sleep(0)
        self.assertEqual(scraper.calls, 1)

        scraper.release.set()
        scheduled_result, manual_result = await asyncio.gather(scheduled, manual)

        self.assertIs(scheduled_result, manual_result)
        self.assertEqual(scraper.calls, 1)
        self.assertEqual(notifier.verified, [result])
        self.assertEqual(notifier.drain_calls, 1)

    async def test_manual_check_is_throttled_per_admin(self) -> None:
        result = SlotCheckResult(
            status=SlotStatus.NO_SLOTS,
            checked_at=self.checked_at,
            details="visible occupied banner",
        )
        scraper = BlockingScraper(result=result)
        scraper.release.set()
        notifier = FakeNotifier()
        monitor = self._monitor(scraper, notifier)

        first = await monitor.check_now(admin_id=1)
        throttled = await monitor.check_now(admin_id=1)
        other_admin = await monitor.check_now(admin_id=2)
        monitor._manual_check_started_at[1] -= 30
        retried = await monitor.check_now(admin_id=1)

        self.assertIs(first, result)
        self.assertEqual(throttled.status, SlotStatus.UNKNOWN)
        self.assertIn("retry in", throttled.details)
        self.assertIs(other_admin, result)
        self.assertIs(retried, result)
        self.assertEqual(scraper.calls, 3)

    async def test_human_action_error_becomes_unknown_without_crashing(self) -> None:
        failure = CloudflareChallengeError("challenge visible")
        scraper = BlockingScraper(error=failure)
        notifier = FakeNotifier()
        monitor = self._monitor(scraper, notifier)
        scraper.release.set()

        result = await monitor.run_once()

        self.assertEqual(result.status, SlotStatus.UNKNOWN)
        self.assertEqual(result.error, "cloudflare_challenge")
        self.assertEqual(notifier.incidents, [failure])
        self.assertEqual(notifier.drain_calls, 1)

    async def test_nonconclusive_result_persists_attempt_not_verified_state(
        self,
    ) -> None:
        result = SlotCheckResult(
            status=SlotStatus.UNKNOWN,
            checked_at=self.checked_at,
            details="incomplete DOM",
            error="inconclusive_page",
        )
        scraper = BlockingScraper(result=result)
        notifier = FakeNotifier()
        monitor = self._monitor(scraper, notifier)
        scraper.release.set()

        returned = await monitor.run_once()
        state = await get_monitor_state(self.db.connection, "warsaw")

        self.assertIs(returned, result)
        assert state is not None
        self.assertEqual(state.verified_state, SlotStatus.UNKNOWN)
        self.assertEqual(state.last_attempt_at, self.checked_at)
        self.assertEqual(state.last_error, "inconclusive_page")
        self.assertEqual(notifier.verified, [])

    async def test_backend_error_persists_and_notifies_service_without_slots_alert(
        self,
    ) -> None:
        result = SlotCheckResult(
            status=SlotStatus.UNKNOWN,
            checked_at=self.checked_at,
            details="site_backend_error",
            error="server_error",
            failure_code=ScraperFailureCode.SERVER_ERROR,
        )
        scraper = BlockingScraper(result=result)
        notifier = FakeNotifier()
        monitor = self._monitor(scraper, notifier)
        scraper.release.set()

        returned = await monitor.run_once()
        state = await get_monitor_state(self.db.connection, "warsaw")

        self.assertIs(returned, result)
        assert state is not None
        self.assertEqual(state.last_error, "server_error")
        self.assertEqual(notifier.incidents, [])
        self.assertEqual(notifier.server_errors, [result])
        self.assertEqual(notifier.verified, [])
        self.assertEqual(notifier.drain_calls, 1)

    async def test_three_unknown_results_double_scheduler_delay_until_recovery(
        self,
    ) -> None:
        server_error = SlotCheckResult(
            status=SlotStatus.UNKNOWN,
            checked_at=self.checked_at,
            details="site_backend_error",
            error="server_error",
            failure_code=ScraperFailureCode.SERVER_ERROR,
        )
        scraper = BlockingScraper(result=server_error)
        scraper.release.set()
        notifier = FakeNotifier()
        monitor = self._monitor(scraper, notifier)

        for _ in range(3):
            await monitor.run_once()

        with patch("src.services.monitor.random.randint", return_value=0):
            self.assertEqual(monitor._next_check_delay(), 600)
        self.assertEqual(notifier.circuit_breakers, [3])

        scraper.result = SlotCheckResult(
            status=SlotStatus.FREE_SLOTS_AVAILABLE,
            checked_at=self.checked_at,
        )
        await monitor.run_once()

        with patch("src.services.monitor.random.randint", return_value=0):
            self.assertEqual(monitor._next_check_delay(), 300)

    async def test_rate_limit_starts_cooldown_and_manual_check_respects_it(
        self,
    ) -> None:
        failure = RateLimitException(
            "Too many requests, please try again later"
        )
        scraper = BlockingScraper(error=failure)
        notifier = FakeNotifier()
        monitor = self._monitor(scraper, notifier)
        scraper.release.set()

        detected = await monitor.run_once()
        skipped = await monitor.check_now(admin_id=1)

        self.assertEqual(detected.failure_code, ScraperFailureCode.TOO_MANY_REQUESTS)
        self.assertEqual(skipped.failure_code, ScraperFailureCode.TOO_MANY_REQUESTS)
        self.assertIn("seconds remaining", skipped.details)
        self.assertEqual(scraper.calls, 1)
        self.assertEqual(len(notifier.rate_limits), 1)
        attempted_at, cooldown_until = notifier.rate_limits[0][1:]
        self.assertEqual(
            (cooldown_until - attempted_at).total_seconds(),
            900,
        )
        self.assertEqual(notifier.drain_calls, 2)

    async def test_restore_state_reloads_future_cooldown(self) -> None:
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        await self.db.connection.execute(
            """
            INSERT INTO monitor_state (
                city_key, slot_state, last_verified_state, cooldown_until
            ) VALUES (?, 'NO_SLOTS', 'NO_SLOTS', ?)
            """,
            (self.settings.city_name.strip().lower(), future.isoformat()),
        )
        await self.db.connection.commit()
        scraper = BlockingScraper(
            result=SlotCheckResult(
                status=SlotStatus.NO_SLOTS,
                checked_at=self.checked_at,
            )
        )
        scraper.release.set()
        monitor = self._monitor(scraper, FakeNotifier())

        await monitor.restore_state()
        skipped = await monitor.run_once()

        self.assertEqual(skipped.failure_code, ScraperFailureCode.TOO_MANY_REQUESTS)
        self.assertIn("seconds remaining", skipped.details)
        self.assertEqual(scraper.calls, 0)
        self.assertEqual(scraper.arm_hard_reload_calls, 1)

    async def test_check_once_restore_skips_persisted_cooldown(self) -> None:
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        await self.db.connection.execute(
            """
            INSERT INTO monitor_state (
                city_key, slot_state, last_verified_state, cooldown_until
            ) VALUES (?, 'NO_SLOTS', 'NO_SLOTS', ?)
            """,
            (self.settings.city_name.strip().lower(), future.isoformat()),
        )
        await self.db.connection.commit()
        result = SlotCheckResult(
            status=SlotStatus.NO_SLOTS,
            checked_at=self.checked_at,
            details="visible occupied banner",
        )
        scraper = BlockingScraper(result=result)
        scraper.release.set()
        monitor = self._monitor(scraper, FakeNotifier())

        await monitor.restore_state(restore_cooldown=False)
        probed = await monitor.run_once()

        self.assertEqual(probed.status, SlotStatus.NO_SLOTS)
        self.assertEqual(scraper.calls, 1)
        self.assertEqual(scraper.arm_hard_reload_calls, 1)

    async def test_expired_cooldown_arms_hard_reload_before_probe(self) -> None:
        result = SlotCheckResult(
            status=SlotStatus.NO_SLOTS,
            checked_at=self.checked_at,
            details="visible occupied banner",
        )
        scraper = BlockingScraper(result=result)
        scraper.release.set()
        monitor = self._monitor(scraper, FakeNotifier())
        monitor._cooldown_until = datetime.now(timezone.utc) - timedelta(seconds=1)

        probed = await monitor.run_once()

        self.assertEqual(probed.status, SlotStatus.NO_SLOTS)
        self.assertEqual(scraper.calls, 1)
        self.assertEqual(scraper.arm_hard_reload_calls, 1)

    async def test_check_now_rate_limit_enters_cooldown_and_alerts(self) -> None:
        failure = RateLimitException(
            "Too many requests, please try again later"
        )
        scraper = BlockingScraper(error=failure)
        notifier = FakeNotifier()
        monitor = self._monitor(scraper, notifier)
        scraper.release.set()

        detected = await monitor.check_now(admin_id=1)
        skipped = await monitor.run_once()

        self.assertEqual(detected.failure_code, ScraperFailureCode.TOO_MANY_REQUESTS)
        self.assertEqual(skipped.failure_code, ScraperFailureCode.TOO_MANY_REQUESTS)
        self.assertEqual(scraper.calls, 1)
        self.assertEqual(len(notifier.rate_limits), 1)
        self.assertEqual(notifier.drain_calls, 2)

    async def test_returned_too_many_requests_enters_cooldown(self) -> None:
        scraper = BlockingScraper(
            result=SlotCheckResult(
                status=SlotStatus.UNKNOWN,
                checked_at=self.checked_at,
                details="too_many_requests",
                error="too_many_requests",
                failure_code=ScraperFailureCode.TOO_MANY_REQUESTS,
            )
        )
        notifier = FakeNotifier()
        monitor = self._monitor(scraper, notifier)
        scraper.release.set()

        detected = await monitor.run_once()
        skipped = await monitor.run_once()

        self.assertEqual(detected.failure_code, ScraperFailureCode.TOO_MANY_REQUESTS)
        self.assertEqual(skipped.failure_code, ScraperFailureCode.TOO_MANY_REQUESTS)
        self.assertEqual(scraper.calls, 1)
        self.assertEqual(len(notifier.rate_limits), 1)

    async def test_consecutive_rate_limits_escalate_cooldown(self) -> None:
        scraper = BlockingScraper(
            error=RateLimitException(
                "Too many requests, please try again later"
            )
        )
        scraper.release.set()
        notifier = FakeNotifier()
        monitor = self._monitor(scraper, notifier)

        for expected_seconds in (900, 1800, 3600, 7200):
            result = await monitor.run_once()
            attempted_at, cooldown_until = notifier.rate_limits[-1][1:]

            self.assertEqual(
                result.failure_code,
                ScraperFailureCode.TOO_MANY_REQUESTS,
            )
            self.assertEqual(
                (cooldown_until - attempted_at).total_seconds(),
                expected_seconds,
            )
            monitor._cooldown_until = None

        self.assertEqual(scraper.calls, 4)

    async def test_verified_result_resets_rate_limit_backoff(self) -> None:
        failure = RateLimitException(
            "Too many requests, please try again later"
        )
        scraper = BlockingScraper(error=failure)
        scraper.release.set()
        notifier = FakeNotifier()
        monitor = self._monitor(scraper, notifier)

        await monitor.run_once()
        monitor._cooldown_until = None
        scraper.error = None
        scraper.result = SlotCheckResult(
            status=SlotStatus.NO_SLOTS,
            checked_at=self.checked_at,
        )
        await monitor.run_once()
        scraper.error = failure
        await monitor.run_once()

        cooldowns = [
            (cooldown_until - attempted_at).total_seconds()
            for _, attempted_at, cooldown_until in notifier.rate_limits
        ]
        self.assertEqual(cooldowns, [900, 900])

    async def test_cycle_timeout_returns_unknown_and_allows_next_cycle(self) -> None:
        scraper = BlockingScraper(
            result=SlotCheckResult(
                status=SlotStatus.NO_SLOTS,
                checked_at=self.checked_at,
            )
        )
        notifier = FakeNotifier()
        monitor = self._monitor(scraper, notifier)

        with patch("src.services.monitor._CYCLE_TIMEOUT_SECONDS", 0.01):
            timed_out = await monitor.run_once()

        self.assertEqual(timed_out.status, SlotStatus.UNKNOWN)
        self.assertEqual(
            timed_out.failure_code,
            ScraperFailureCode.SCRAPER_ERROR,
        )
        self.assertTrue(scraper.cancelled.is_set())

        scraper.release.set()
        recovered = await monitor.run_once()

        self.assertEqual(recovered.status, SlotStatus.NO_SLOTS)
        self.assertEqual(scraper.calls, 2)

    async def test_cancelling_monitor_run_cancels_and_awaits_inflight_cycle(
        self,
    ) -> None:
        scraper = BlockingScraper(
            result=SlotCheckResult(
                status=SlotStatus.NO_SLOTS,
                checked_at=self.checked_at,
            )
        )
        notifier = FakeNotifier()
        monitor = self._monitor(scraper, notifier)
        run_task = asyncio.create_task(monitor.run(asyncio.Event()))
        await scraper.entered.wait()

        run_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await run_task

        self.assertTrue(scraper.cancelled.is_set())
        with self.assertRaisesRegex(RuntimeError, "shutting down"):
            await monitor.check_now(admin_id=1)


if __name__ == "__main__":
    unittest.main()
