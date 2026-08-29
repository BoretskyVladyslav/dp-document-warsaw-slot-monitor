from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.core.models import NotificationEventType, NotificationRecipient
from src.database.connection import Database
from src.database.notification_outbox import (
    enqueue_notification_event,
    list_pending_deliveries,
    mark_delivery_delivered,
    mark_delivery_failed,
    mark_delivery_unreachable,
)
from src.database.schema import init_schema


class NotificationOutboxTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._tmp.name) / "outbox.db"))
        await self.db.connect()
        await init_schema(self.db.connection)
        self.created_at = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._tmp.cleanup()

    async def test_enqueue_is_idempotent_and_deduplicates_chat_ids(self) -> None:
        recipients = [
            NotificationRecipient(chat_id=100, user_id=1),
            NotificationRecipient(chat_id=100),
            NotificationRecipient(chat_id=200, user_id=2),
        ]
        first_id = await enqueue_notification_event(
            self.db.connection,
            city_key="warsaw",
            event_key="free:2026-08-29T12:00:00Z",
            event_type=NotificationEventType.SLOTS_AVAILABLE,
            text="slots available",
            recipients=recipients,
            created_at=self.created_at,
        )
        second_id = await enqueue_notification_event(
            self.db.connection,
            city_key="warsaw",
            event_key="free:2026-08-29T12:00:00Z",
            event_type=NotificationEventType.SLOTS_AVAILABLE,
            text="slots available",
            recipients=[
                *recipients,
                NotificationRecipient(chat_id=300, user_id=3),
            ],
            created_at=self.created_at,
        )

        pending = await list_pending_deliveries(self.db.connection)

        self.assertEqual(first_id, second_id)
        self.assertEqual([item.chat_id for item in pending], [100, 200, 300])
        self.assertEqual(pending[0].user_id, 1)
        self.assertEqual(pending[0].attempts, 0)

    async def test_delivery_updates_only_the_selected_recipient(self) -> None:
        event_id = await enqueue_notification_event(
            self.db.connection,
            city_key="warsaw",
            event_key="free:1",
            event_type=NotificationEventType.SLOTS_AVAILABLE,
            text="slots available",
            recipients=[
                NotificationRecipient(chat_id=100, user_id=1),
                NotificationRecipient(chat_id=200, user_id=2),
                NotificationRecipient(chat_id=300, user_id=3),
            ],
            created_at=self.created_at,
        )

        self.assertTrue(
            await mark_delivery_delivered(
                self.db.connection,
                event_id=event_id,
                chat_id=100,
                delivered_at=self.created_at,
            )
        )
        self.assertTrue(
            await mark_delivery_failed(
                self.db.connection,
                event_id=event_id,
                chat_id=200,
                error="temporary network error",
            )
        )
        self.assertTrue(
            await mark_delivery_unreachable(
                self.db.connection,
                event_id=event_id,
                chat_id=300,
                error="bot blocked",
            )
        )

        pending = await list_pending_deliveries(self.db.connection)

        self.assertEqual([item.chat_id for item in pending], [200])
        self.assertEqual(pending[0].attempts, 1)
        self.assertFalse(
            await mark_delivery_delivered(
                self.db.connection,
                event_id=event_id,
                chat_id=100,
                delivered_at=self.created_at,
            )
        )

    async def test_event_key_cannot_be_reused_for_different_payload(self) -> None:
        await enqueue_notification_event(
            self.db.connection,
            city_key="warsaw",
            event_key="incident:target_tab_missing",
            event_type=NotificationEventType.HUMAN_ACTION_REQUIRED,
            text="Open the target tab",
            recipients=[NotificationRecipient(chat_id=42)],
            created_at=self.created_at,
        )

        with self.assertRaises(ValueError):
            await enqueue_notification_event(
                self.db.connection,
                city_key="warsaw",
                event_key="incident:target_tab_missing",
                event_type=NotificationEventType.HUMAN_ACTION_REQUIRED,
                text="Different message",
                recipients=[NotificationRecipient(chat_id=42)],
                created_at=self.created_at,
            )

    async def test_pending_limit_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            await list_pending_deliveries(self.db.connection, limit=0)


if __name__ == "__main__":
    unittest.main()
