from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.core.exceptions import DeliveryError, RecipientUnreachableError
from src.core.models import SlotCheckResult, SlotStatus
from src.database.connection import Database
from src.database.schema import init_schema
from src.database.subscribers import add_subscriber
from src.services.notifier import Notifier


class _FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))


class _FailingSender:
    async def send(self, chat_id: int, text: str) -> None:
        raise RecipientUnreachableError(chat_id)


class _SequenceSender:
    def __init__(self, fail_on_calls: frozenset[int]) -> None:
        self._fail_on_calls = fail_on_calls
        self.calls = 0
        self.sent: list[str] = []

    async def send(self, chat_id: int, text: str) -> None:
        self.calls += 1
        if self.calls in self._fail_on_calls:
            raise DeliveryError(chat_id, reason="telegram network error")
        self.sent.append(text)


class _PerChatSender:
    def __init__(self, failures: dict[int, BaseException]) -> None:
        self._failures = failures
        self.sent: list[int] = []

    async def send(self, chat_id: int, text: str) -> None:
        failure = self._failures.get(chat_id)
        if failure is not None:
            raise failure
        self.sent.append(chat_id)


class NotifierTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db_path = str(Path(self._tmp.name) / "test.db")
        self.db = Database(db_path)
        await self.db.connect()
        await init_schema(self.db.connection)
        await add_subscriber(
            self.db.connection, user_id=1, chat_id=100, username="alice"
        )

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._tmp.cleanup()

    async def test_notifies_on_no_slots_to_free(self) -> None:
        sender = _FakeSender()
        notifier = Notifier(
            database=self.db,
            sender=sender,
            city_name="Warsaw",
            target_url="https://example.test/queue",
        )
        result = SlotCheckResult(
            status=SlotStatus.FREE_SLOTS_AVAILABLE,
            checked_at=datetime.now(timezone.utc),
            details="2099-09-10 10:00",
            slots=("2099-09-10 10:00",),
        )
        await notifier.handle_check(result)
        self.assertEqual(len(sender.sent), 1)
        self.assertIn("https://example.test/queue", sender.sent[0][1])

        await notifier.handle_check(result)
        self.assertEqual(len(sender.sent), 1)

    async def test_notifies_once_when_slots_gone(self) -> None:
        sender = _FakeSender()
        notifier = Notifier(
            database=self.db,
            sender=sender,
            city_name="Warsaw",
            target_url="https://example.test/queue",
        )
        notifier.prime_previous(SlotStatus.FREE_SLOTS_AVAILABLE)
        gone = SlotCheckResult(
            status=SlotStatus.NO_SLOTS,
            checked_at=datetime.now(timezone.utc),
        )
        await notifier.handle_check(gone)
        await notifier.handle_check(gone)
        self.assertEqual(len(sender.sent), 1)
        self.assertIn("зайняті", sender.sent[0][1])

    async def test_unknown_does_not_notify(self) -> None:
        sender = _FakeSender()
        notifier = Notifier(
            database=self.db,
            sender=sender,
            city_name="Warsaw",
            target_url="https://example.test/queue",
        )
        await notifier.handle_check(
            SlotCheckResult(
                status=SlotStatus.UNKNOWN,
                checked_at=datetime.now(timezone.utc),
                error="timeout",
            )
        )
        self.assertEqual(sender.sent, [])

    async def test_unreachable_recipient_is_deactivated(self) -> None:
        notifier = Notifier(
            database=self.db,
            sender=_FailingSender(),
            city_name="Warsaw",
            target_url="https://example.test/queue",
        )
        await notifier.handle_check(
            SlotCheckResult(
                status=SlotStatus.FREE_SLOTS_AVAILABLE,
                checked_at=datetime.now(timezone.utc),
                slots=("2099-09-10",),
            )
        )
        from src.database.subscribers import get_subscriber

        row = await get_subscriber(self.db.connection, 1)
        assert row is not None
        self.assertFalse(row.is_active)

    async def test_transient_delivery_error_does_not_abort_or_deactivate(self) -> None:
        await add_subscriber(self.db.connection, user_id=2, chat_id=200, username="bob")
        sender = _PerChatSender(
            {
                100: DeliveryError(100, reason="telegram network error"),
            }
        )
        notifier = Notifier(
            database=self.db,
            sender=sender,
            city_name="Warsaw",
            target_url="https://example.test/queue",
        )
        result = await notifier.handle_check(
            SlotCheckResult(
                status=SlotStatus.FREE_SLOTS_AVAILABLE,
                checked_at=datetime.now(timezone.utc),
                slots=("2099-09-10",),
            )
        )
        self.assertEqual(result, SlotStatus.FREE_SLOTS_AVAILABLE)
        self.assertEqual(sender.sent, [200])
        from src.database.subscribers import get_subscriber

        alice = await get_subscriber(self.db.connection, 1)
        bob = await get_subscriber(self.db.connection, 2)
        assert alice is not None and bob is not None
        self.assertTrue(alice.is_active)
        self.assertTrue(bob.is_active)

    async def test_unexpected_send_error_does_not_abort_handle_check(self) -> None:
        await add_subscriber(self.db.connection, user_id=2, chat_id=200, username="bob")
        sender = _PerChatSender({100: RuntimeError("boom")})
        notifier = Notifier(
            database=self.db,
            sender=sender,
            city_name="Warsaw",
            target_url="https://example.test/queue",
        )
        result = await notifier.handle_check(
            SlotCheckResult(
                status=SlotStatus.FREE_SLOTS_AVAILABLE,
                checked_at=datetime.now(timezone.utc),
                slots=("2099-09-10",),
            )
        )
        self.assertEqual(result, SlotStatus.FREE_SLOTS_AVAILABLE)
        self.assertEqual(sender.sent, [200])


    def _free_result(self, slot: str = "2099-09-10") -> SlotCheckResult:
        return SlotCheckResult(
            status=SlotStatus.FREE_SLOTS_AVAILABLE,
            checked_at=datetime.now(timezone.utc),
            details=slot,
            slots=(slot,),
        )

    def _gone_result(self) -> SlotCheckResult:
        return SlotCheckResult(
            status=SlotStatus.NO_SLOTS,
            checked_at=datetime.now(timezone.utc),
        )

    async def test_all_transient_failures_keep_state_for_retry(self) -> None:
        sender = _PerChatSender({100: DeliveryError(100, reason="telegram network error")})
        notifier = Notifier(
            database=self.db,
            sender=sender,
            city_name="Warsaw",
            target_url="https://example.test/queue",
        )
        first = await notifier.handle_check(self._free_result())
        self.assertIsNone(first)
        self.assertEqual(sender.sent, [])

        sender._failures.clear()
        second = await notifier.handle_check(self._free_result())
        self.assertEqual(second, SlotStatus.FREE_SLOTS_AVAILABLE)
        self.assertEqual(sender.sent, [100])

        third = await notifier.handle_check(self._free_result())
        self.assertEqual(third, SlotStatus.FREE_SLOTS_AVAILABLE)
        self.assertEqual(sender.sent, [100])

    async def test_unreachable_commits_state_without_retry(self) -> None:
        sender = _FailingSender()
        notifier = Notifier(
            database=self.db,
            sender=sender,
            city_name="Warsaw",
            target_url="https://example.test/queue",
        )
        first = await notifier.handle_check(self._free_result())
        self.assertEqual(first, SlotStatus.FREE_SLOTS_AVAILABLE)
        second = await notifier.handle_check(self._free_result())
        self.assertEqual(second, SlotStatus.FREE_SLOTS_AVAILABLE)

    async def test_oscillation_drops_stale_gone_and_sends_fresh_free(self) -> None:
        sender = _SequenceSender(fail_on_calls=frozenset({2}))
        notifier = Notifier(
            database=self.db,
            sender=sender,
            city_name="Warsaw",
            target_url="https://example.test/queue",
        )
        first = await notifier.handle_check(self._free_result("2099-09-10"))
        self.assertEqual(first, SlotStatus.FREE_SLOTS_AVAILABLE)
        self.assertEqual(len(sender.sent), 1)
        self.assertIn("2099-09-10", sender.sent[0])

        deferred = await notifier.handle_check(self._gone_result())
        self.assertEqual(deferred, SlotStatus.FREE_SLOTS_AVAILABLE)
        self.assertEqual(len(sender.sent), 1)
        self.assertTrue(all("зайняті" not in text for text in sender.sent))

        reconciled = await notifier.handle_check(self._free_result("2099-09-11"))
        self.assertEqual(reconciled, SlotStatus.FREE_SLOTS_AVAILABLE)
        self.assertEqual(len(sender.sent), 2)
        self.assertIn("2099-09-11", sender.sent[1])
        self.assertTrue(all("зайняті" not in text for text in sender.sent))


if __name__ == "__main__":
    unittest.main()
