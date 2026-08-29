from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

import aiosqlite

from src.core.models import (
    DeliveryStatus,
    NotificationEventType,
    NotificationRecipient,
    PendingNotificationDelivery,
    ScraperFailureCode,
    SlotStatus,
)
from src.database.connection import Database


def _parse_timestamp(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _validate_event(
    *,
    city_key: str,
    event_key: str,
    text: str,
) -> None:
    if not city_key.strip():
        raise ValueError("city_key is required")
    if not event_key.strip():
        raise ValueError("event_key is required")
    if not text:
        raise ValueError("notification text is required")


async def _enqueue_event_uncommitted(
    connection: aiosqlite.Connection,
    *,
    city_key: str,
    event_key: str,
    event_type: NotificationEventType,
    text: str,
    recipients: Sequence[NotificationRecipient],
    created_at: datetime,
) -> int:
    _validate_event(city_key=city_key, event_key=event_key, text=text)
    await connection.execute(
        """
        INSERT INTO notification_events (city_key, event_key, event_type, text, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(city_key, event_key) DO NOTHING
        """,
        (city_key, event_key, event_type.value, text, created_at.isoformat()),
    )
    cursor = await connection.execute(
        """
        SELECT id, event_type, text
        FROM notification_events
        WHERE city_key = ? AND event_key = ?
        """,
        (city_key, event_key),
    )
    row = await cursor.fetchone()
    await cursor.close()
    if row is None:
        raise RuntimeError("notification event upsert did not persist")
    if str(row["event_type"]) != event_type.value or str(row["text"]) != text:
        raise ValueError("event_key already exists with a different payload")

    event_id = int(row["id"])
    unique_recipients: dict[int, int | None] = {}
    for recipient in recipients:
        existing_user_id = unique_recipients.get(recipient.chat_id)
        if recipient.chat_id not in unique_recipients or existing_user_id is None:
            unique_recipients[recipient.chat_id] = recipient.user_id
    if unique_recipients:
        await connection.executemany(
            """
            INSERT INTO notification_deliveries (event_id, chat_id, user_id)
            VALUES (?, ?, ?)
            ON CONFLICT(event_id, chat_id) DO UPDATE SET
                user_id = COALESCE(notification_deliveries.user_id, excluded.user_id)
            """,
            (
                (event_id, chat_id, user_id)
                for chat_id, user_id in unique_recipients.items()
            ),
        )
    return event_id


async def enqueue_notification_event(
    connection: aiosqlite.Connection,
    *,
    city_key: str,
    event_key: str,
    event_type: NotificationEventType,
    text: str,
    recipients: Sequence[NotificationRecipient],
    created_at: datetime,
) -> int:
    try:
        event_id = await _enqueue_event_uncommitted(
            connection,
            city_key=city_key,
            event_key=event_key,
            event_type=event_type,
            text=text,
            recipients=recipients,
            created_at=created_at,
        )
        await connection.commit()
    except (aiosqlite.Error, RuntimeError, ValueError):
        await connection.rollback()
        raise
    return event_id


async def persist_verified_result(
    database: Database,
    *,
    city_key: str,
    slot_state: SlotStatus,
    verified_at: datetime,
    details: str,
    notification_text: str,
    recipients: Sequence[NotificationRecipient],
) -> tuple[SlotStatus, bool, int | None]:
    if slot_state is SlotStatus.UNKNOWN:
        raise ValueError("verified slot state cannot be UNKNOWN")
    async with database.transaction() as connection:
        cursor = await connection.execute(
            "SELECT last_verified_state FROM monitor_state WHERE city_key = ?",
            (city_key,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        previous = (
            SlotStatus(str(row["last_verified_state"]))
            if row is not None
            else SlotStatus.UNKNOWN
        )
        transitioned = previous is not slot_state
        verified = verified_at.isoformat()
        await connection.execute(
            """
            INSERT INTO monitor_state (
                city_key,
                slot_state,
                last_check_at,
                last_details,
                last_error,
                last_verified_state,
                last_verified_at,
                last_attempt_at,
                human_action_incident_key,
                human_action_incident_notified_at
            )
            VALUES (?, ?, ?, ?, NULL, ?, ?, ?, NULL, NULL)
            ON CONFLICT(city_key) DO UPDATE SET
                slot_state = excluded.slot_state,
                last_check_at = excluded.last_check_at,
                last_details = excluded.last_details,
                last_error = NULL,
                last_verified_state = excluded.last_verified_state,
                last_verified_at = excluded.last_verified_at,
                last_attempt_at = excluded.last_attempt_at,
                human_action_incident_key = NULL,
                human_action_incident_notified_at = NULL,
                cooldown_until = NULL
            """,
            (
                city_key,
                slot_state.value,
                verified,
                details,
                slot_state.value,
                verified,
                verified,
            ),
        )
        event_id: int | None = None
        if transitioned and slot_state is SlotStatus.FREE_SLOTS_AVAILABLE:
            subscriber_cursor = await connection.execute(
                """
                SELECT user_id, chat_id
                FROM subscribers
                WHERE is_active = 1
                ORDER BY created_at
                """
            )
            subscriber_rows = await subscriber_cursor.fetchall()
            await subscriber_cursor.close()
            event_recipients = [
                NotificationRecipient(
                    user_id=int(subscriber["user_id"]),
                    chat_id=int(subscriber["chat_id"]),
                )
                for subscriber in subscriber_rows
            ]
            event_recipients.extend(recipients)
            event_id = await _enqueue_event_uncommitted(
                connection,
                city_key=city_key,
                event_key=f"slots_available:{verified}",
                event_type=NotificationEventType.SLOTS_AVAILABLE,
                text=notification_text,
                recipients=event_recipients,
                created_at=verified_at,
            )
    return previous, transitioned, event_id


async def persist_human_action_incident(
    database: Database,
    *,
    city_key: str,
    failure_code: ScraperFailureCode,
    attempted_at: datetime,
    details: str,
    notification_text: str,
    recipients: Sequence[NotificationRecipient],
    cooldown_until: datetime | None = None,
) -> tuple[bool, int | None]:
    async with database.transaction() as connection:
        cursor = await connection.execute(
            """
            SELECT human_action_incident_key
            FROM monitor_state
            WHERE city_key = ?
            """,
            (city_key,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        current_key = (
            str(row["human_action_incident_key"])
            if row is not None and row["human_action_incident_key"] is not None
            else None
        )
        incident_key = failure_code.value
        is_new = current_key != incident_key
        attempted = attempted_at.isoformat()
        cooldown = cooldown_until.isoformat() if cooldown_until is not None else None
        notified_at = attempted if is_new and recipients else None
        await connection.execute(
            """
            INSERT INTO monitor_state (
                city_key,
                slot_state,
                last_check_at,
                last_details,
                last_error,
                last_verified_state,
                last_attempt_at,
                human_action_incident_key,
                human_action_incident_notified_at,
                cooldown_until
            )
            VALUES (?, 'UNKNOWN', ?, ?, ?, 'UNKNOWN', ?, ?, ?, ?)
            ON CONFLICT(city_key) DO UPDATE SET
                last_check_at = excluded.last_check_at,
                last_attempt_at = excluded.last_attempt_at,
                last_details = excluded.last_details,
                last_error = excluded.last_error,
                human_action_incident_key = excluded.human_action_incident_key,
                human_action_incident_notified_at = CASE
                    WHEN monitor_state.human_action_incident_key =
                         excluded.human_action_incident_key
                    THEN monitor_state.human_action_incident_notified_at
                    ELSE excluded.human_action_incident_notified_at
                END,
                cooldown_until = excluded.cooldown_until
            """,
            (
                city_key,
                attempted,
                details,
                failure_code.value,
                attempted,
                incident_key,
                notified_at,
                cooldown,
            ),
        )
        event_id: int | None = None
        if is_new and recipients:
            event_id = await _enqueue_event_uncommitted(
                connection,
                city_key=city_key,
                event_key=f"incident:{incident_key}:{attempted}",
                event_type=NotificationEventType.HUMAN_ACTION_REQUIRED,
                text=notification_text,
                recipients=recipients,
                created_at=attempted_at,
            )
    return is_new, event_id


async def list_pending_deliveries(
    connection: aiosqlite.Connection,
    *,
    city_key: str | None = None,
    limit: int = 100,
) -> list[PendingNotificationDelivery]:
    if limit < 1:
        raise ValueError("limit must be positive")
    cursor = await connection.execute(
        """
        SELECT
            delivery.event_id,
            event.event_key,
            event.event_type,
            event.city_key,
            delivery.chat_id,
            delivery.user_id,
            event.text,
            delivery.attempts,
            event.created_at
        FROM notification_deliveries AS delivery
        JOIN notification_events AS event ON event.id = delivery.event_id
        WHERE delivery.status = ?
          AND (? IS NULL OR event.city_key = ?)
        ORDER BY event.id, delivery.chat_id
        LIMIT ?
        """,
        (DeliveryStatus.PENDING.value, city_key, city_key, limit),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return [
        PendingNotificationDelivery(
            event_id=int(row["event_id"]),
            event_key=str(row["event_key"]),
            event_type=NotificationEventType(str(row["event_type"])),
            city_key=str(row["city_key"]),
            chat_id=int(row["chat_id"]),
            user_id=int(row["user_id"]) if row["user_id"] is not None else None,
            text=str(row["text"]),
            attempts=int(row["attempts"]),
            created_at=_parse_timestamp(row["created_at"]),
        )
        for row in rows
    ]


async def mark_delivery_delivered(
    connection: aiosqlite.Connection,
    *,
    event_id: int,
    chat_id: int,
    delivered_at: datetime,
) -> bool:
    cursor = await connection.execute(
        """
        UPDATE notification_deliveries
        SET status = ?,
            attempts = attempts + 1,
            last_error = NULL,
            delivered_at = ?
        WHERE event_id = ? AND chat_id = ? AND status = ?
        """,
        (
            DeliveryStatus.DELIVERED.value,
            delivered_at.isoformat(),
            event_id,
            chat_id,
            DeliveryStatus.PENDING.value,
        ),
    )
    await connection.commit()
    changed = cursor.rowcount == 1
    await cursor.close()
    return changed


async def mark_delivery_unreachable(
    connection: aiosqlite.Connection,
    *,
    event_id: int,
    chat_id: int,
    error: str,
) -> bool:
    cursor = await connection.execute(
        """
        UPDATE notification_deliveries
        SET status = ?,
            attempts = attempts + 1,
            last_error = ?,
            delivered_at = NULL
        WHERE event_id = ? AND chat_id = ? AND status = ?
        """,
        (
            DeliveryStatus.UNREACHABLE.value,
            error,
            event_id,
            chat_id,
            DeliveryStatus.PENDING.value,
        ),
    )
    await connection.commit()
    changed = cursor.rowcount == 1
    await cursor.close()
    return changed


async def mark_delivery_failed(
    connection: aiosqlite.Connection,
    *,
    event_id: int,
    chat_id: int,
    error: str,
) -> bool:
    cursor = await connection.execute(
        """
        UPDATE notification_deliveries
        SET attempts = attempts + 1,
            last_error = ?
        WHERE event_id = ? AND chat_id = ? AND status = ?
        """,
        (
            error,
            event_id,
            chat_id,
            DeliveryStatus.PENDING.value,
        ),
    )
    await connection.commit()
    changed = cursor.rowcount == 1
    await cursor.close()
    return changed
