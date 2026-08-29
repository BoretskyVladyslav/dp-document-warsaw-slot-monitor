from __future__ import annotations

from datetime import datetime, timezone

import aiosqlite

from src.core.models import Subscriber


def _parse_created_at(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _row_to_subscriber(row: aiosqlite.Row) -> Subscriber:
    return Subscriber(
        user_id=int(row["user_id"]),
        chat_id=int(row["chat_id"]),
        username=row["username"],
        is_active=bool(row["is_active"]),
        created_at=_parse_created_at(str(row["created_at"])),
    )


async def add_subscriber(
    connection: aiosqlite.Connection,
    *,
    user_id: int,
    chat_id: int,
    username: str | None,
) -> Subscriber:
    now = datetime.now(timezone.utc).isoformat()
    await connection.execute(
        """
        INSERT INTO subscribers (user_id, chat_id, username, is_active, created_at)
        VALUES (?, ?, ?, 1, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            chat_id = excluded.chat_id,
            username = excluded.username,
            is_active = 1
        """,
        (user_id, chat_id, username, now),
    )
    await connection.commit()
    row = await get_subscriber(connection, user_id)
    if row is None:
        raise RuntimeError("subscriber upsert did not persist")
    return row


async def remove_subscriber(
    connection: aiosqlite.Connection,
    user_id: int,
) -> bool:
    cursor = await connection.execute(
        "UPDATE subscribers SET is_active = 0 WHERE user_id = ?",
        (user_id,),
    )
    await connection.commit()
    return cursor.rowcount > 0


async def get_subscriber(
    connection: aiosqlite.Connection,
    user_id: int,
) -> Subscriber | None:
    cursor = await connection.execute(
        "SELECT user_id, chat_id, username, is_active, created_at FROM subscribers WHERE user_id = ?",
        (user_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return _row_to_subscriber(row)


async def get_all_active_subscribers(
    connection: aiosqlite.Connection,
) -> list[Subscriber]:
    cursor = await connection.execute(
        """
        SELECT user_id, chat_id, username, is_active, created_at
        FROM subscribers
        WHERE is_active = 1
        ORDER BY created_at ASC
        """
    )
    rows = await cursor.fetchall()
    return [_row_to_subscriber(row) for row in rows]


async def count_active_subscribers(connection: aiosqlite.Connection) -> int:
    cursor = await connection.execute(
        "SELECT COUNT(*) AS cnt FROM subscribers WHERE is_active = 1"
    )
    row = await cursor.fetchone()
    if row is None:
        return 0
    return int(row["cnt"])


async def toggle_subscription(
    connection: aiosqlite.Connection,
    user_id: int,
) -> Subscriber | None:
    current = await get_subscriber(connection, user_id)
    if current is None:
        return None
    new_active = 0 if current.is_active else 1
    await connection.execute(
        "UPDATE subscribers SET is_active = ? WHERE user_id = ?",
        (new_active, user_id),
    )
    await connection.commit()
    return await get_subscriber(connection, user_id)
