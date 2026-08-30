from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.core.exceptions import (
    CdpUnavailableError,
    CloudflareChallengeError,
    DeliveryError,
    RateLimitException,
    RecipientUnreachableError,
)
from src.core.models import ScraperFailureCode, SlotCheckResult, SlotStatus
from src.database.connection import Database
from src.database.monitor_state import get_monitor_state
from src.database.notification_outbox import list_pending_deliveries
from src.database.schema import init_schema
from src.database.subscribers import add_subscriber, get_subscriber
from src.services.notifier import Notifier

TARGET_URL = "https://warszawa.pasport.org.ua/solutions/e-queue"


class RecordingSender:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.failures: dict[int, BaseException] = {}

    async def send(self, chat_id: int, text: str) -> None:
        failure = self.failures.get(chat_id)
        if failure is not None:
            raise failure
        self.sent.append((chat_id, text))


class NotifierTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._tmp.name) / "notifier.db"))
        await self.db.connect()
        await init_schema(self.db.connection)
        await add_subscriber(
            self.db.connection,
            user_id=1,
            chat_id=100,
            username="alice",
        )
        self.sender = RecordingSender()
        self.notifier = self._notifier(admin_ids=[42])
        self.checked_at = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._tmp.cleanup()

    def _notifier(self, *, admin_ids: list[int] | None = None) -> Notifier:
        return Notifier(
            database=self.db,
            sender=self.sender,
            city_name="Warsaw",
            target_url=TARGET_URL,
            admin_ids=admin_ids,
        )

    def _result(
        self,
        status: SlotStatus,
        *,
        checked_at: datetime | None = None,
    ) -> SlotCheckResult:
        return SlotCheckResult(
            status=status,
            checked_at=checked_at or self.checked_at,
            details="visible booking form" if status is SlotStatus.FREE_SLOTS_AVAILABLE else "",
        )

    async def test_free_transition_is_queued_once_for_subscribers_and_admin(self) -> None:
        transitioned = await self.notifier.handle_verified_result(
            self._result(SlotStatus.FREE_SLOTS_AVAILABLE)
        )
        self.assertTrue(transitioned)
        self.assertEqual(self.sender.sent, [])

        summary = await self.notifier.drain_outbox()
        self.assertEqual(summary.delivered, 2)
        self.assertEqual([chat_id for chat_id, _ in self.sender.sent], [42, 100])

        repeated = await self.notifier.handle_verified_result(
            self._result(
                SlotStatus.FREE_SLOTS_AVAILABLE,
                checked_at=self.checked_at + timedelta(minutes=3),
            )
        )
        self.assertFalse(repeated)
        await self.notifier.drain_outbox()
        self.assertEqual(len(self.sender.sent), 2)

    async def test_admin_is_not_duplicated_when_also_subscribed(self) -> None:
        await add_subscriber(
            self.db.connection,
            user_id=42,
            chat_id=42,
            username="admin",
        )
        await self.notifier.handle_verified_result(
            self._result(SlotStatus.FREE_SLOTS_AVAILABLE)
        )

        await self.notifier.drain_outbox()

        self.assertEqual([chat_id for chat_id, _ in self.sender.sent].count(42), 1)

    async def test_transient_failure_retries_only_pending_recipient(self) -> None:
        await add_subscriber(
            self.db.connection,
            user_id=2,
            chat_id=200,
            username="bob",
        )
        self.sender.failures[100] = DeliveryError(100, "network")
        await self.notifier.handle_verified_result(
            self._result(SlotStatus.FREE_SLOTS_AVAILABLE)
        )

        first = await self.notifier.drain_outbox()

        self.assertEqual(first.delivered, 2)
        self.assertEqual(first.transient_failed, 1)
        self.assertEqual([chat_id for chat_id, _ in self.sender.sent], [42, 200])
        pending = await list_pending_deliveries(
            self.db.connection,
            city_key="warsaw",
        )
        self.assertEqual([item.chat_id for item in pending], [100])
        self.assertEqual(pending[0].attempts, 1)

        self.sender.failures.clear()
        restarted_notifier = self._notifier(admin_ids=[42])
        second = await restarted_notifier.drain_outbox()

        self.assertEqual(second.delivered, 1)
        self.assertEqual([chat_id for chat_id, _ in self.sender.sent], [42, 200, 100])
        self.assertEqual(
            await list_pending_deliveries(
                self.db.connection,
                city_key="warsaw",
            ),
            [],
        )

    async def test_unreachable_subscriber_is_deactivated(self) -> None:
        self.sender.failures[100] = RecipientUnreachableError(100)
        await self.notifier.handle_verified_result(
            self._result(SlotStatus.FREE_SLOTS_AVAILABLE)
        )

        summary = await self.notifier.drain_outbox()

        self.assertEqual(summary.unreachable, 1)
        subscriber = await get_subscriber(self.db.connection, 1)
        assert subscriber is not None
        self.assertFalse(subscriber.is_active)

    async def test_incident_alert_is_admin_only_and_deduplicated_across_restart(
        self,
    ) -> None:
        failure = CloudflareChallengeError("challenge visible")
        first = await self.notifier.handle_human_action_required(
            failure,
            attempted_at=self.checked_at,
        )
        repeated = await self.notifier.handle_human_action_required(
            failure,
            attempted_at=self.checked_at + timedelta(minutes=3),
        )
        self.assertTrue(first)
        self.assertFalse(repeated)

        restarted_notifier = self._notifier(admin_ids=[42])
        repeated_after_restart = (
            await restarted_notifier.handle_human_action_required(
                failure,
                attempted_at=self.checked_at + timedelta(minutes=6),
            )
        )
        self.assertFalse(repeated_after_restart)
        await restarted_notifier.drain_outbox()

        self.assertEqual([chat_id for chat_id, _ in self.sender.sent], [42])
        self.assertIn("cloudflare_challenge", self.sender.sent[0][1])

    async def test_successful_verification_clears_incident_for_future_alert(self) -> None:
        failure = CdpUnavailableError("CDP offline")
        await self.notifier.handle_human_action_required(
            failure,
            attempted_at=self.checked_at,
        )
        await self.notifier.drain_outbox()
        await self.notifier.handle_verified_result(
            self._result(
                SlotStatus.NO_SLOTS,
                checked_at=self.checked_at + timedelta(minutes=3),
            )
        )
        state = await get_monitor_state(self.db.connection, "warsaw")
        assert state is not None
        self.assertIsNone(state.human_action_incident_key)

        is_new = await self.notifier.handle_human_action_required(
            failure,
            attempted_at=self.checked_at + timedelta(minutes=6),
        )
        await self.notifier.drain_outbox()

        self.assertTrue(is_new)
        self.assertEqual([chat_id for chat_id, _ in self.sender.sent], [42, 42])

    async def test_rate_limit_alert_is_deduplicated_and_cooldown_is_extended(
        self,
    ) -> None:
        failure = RateLimitException(
            "Too many requests, please try again later"
        )
        first_until = self.checked_at + timedelta(minutes=15)
        extended_until = self.checked_at + timedelta(minutes=20)

        first = await self.notifier.handle_rate_limit(
            failure,
            attempted_at=self.checked_at,
            cooldown_until=first_until,
        )
        repeated = await self.notifier.handle_rate_limit(
            failure,
            attempted_at=self.checked_at + timedelta(minutes=5),
            cooldown_until=extended_until,
        )
        await self.notifier.drain_outbox()
        state = await get_monitor_state(self.db.connection, "warsaw")

        self.assertTrue(first)
        self.assertFalse(repeated)
        assert state is not None
        self.assertEqual(state.cooldown_until, extended_until)
        self.assertEqual(len(self.sender.sent), 1)
        self.assertEqual(self.sender.sent[0][0], 42)
        self.assertIn("Cooldown: 900 seconds", self.sender.sent[0][1])
        self.assertIn("2026-08-29 12:15:00", self.sender.sent[0][1])

        await self.notifier.handle_verified_result(
            self._result(
                SlotStatus.NO_SLOTS,
                checked_at=extended_until + timedelta(seconds=1),
            )
        )
        state = await get_monitor_state(self.db.connection, "warsaw")
        assert state is not None
        self.assertIsNone(state.cooldown_until)
        self.assertIsNone(state.human_action_incident_key)

    async def test_no_slots_transition_does_not_broadcast(self) -> None:
        transitioned = await self.notifier.handle_verified_result(
            self._result(SlotStatus.NO_SLOTS)
        )

        summary = await self.notifier.drain_outbox()

        self.assertTrue(transitioned)
        self.assertEqual(summary.delivered, 0)
        self.assertEqual(self.sender.sent, [])

    async def test_server_error_alert_is_admin_only_and_latched(self) -> None:
        result = SlotCheckResult(
            status=SlotStatus.UNKNOWN,
            checked_at=self.checked_at,
            details="site_backend_error",
            error="server_error",
            failure_code=ScraperFailureCode.SERVER_ERROR,
        )
        first = await self.notifier.handle_server_error(result)
        repeated = await self.notifier.handle_server_error(
            SlotCheckResult(
                status=SlotStatus.UNKNOWN,
                checked_at=self.checked_at + timedelta(minutes=3),
                details="site_backend_error",
                error="server_error",
                failure_code=ScraperFailureCode.SERVER_ERROR,
            )
        )
        await self.notifier.drain_outbox()

        self.assertTrue(first)
        self.assertFalse(repeated)
        self.assertEqual([chat_id for chat_id, _ in self.sender.sent], [42])
        self.assertIn("site_backend_error", self.sender.sent[0][1])
        self.assertIn("Cookies", self.sender.sent[0][1])

    async def test_circuit_breaker_alert_is_admin_only(self) -> None:
        await self.notifier.handle_circuit_breaker(
            attempted_at=self.checked_at,
            consecutive_failures=3,
            next_interval_seconds=600,
        )
        await self.notifier.drain_outbox()

        self.assertEqual([chat_id for chat_id, _ in self.sender.sent], [42])
        self.assertIn("Circuit breaker", self.sender.sent[0][1])
        self.assertIn("600 seconds", self.sender.sent[0][1])


if __name__ == "__main__":
    unittest.main()
