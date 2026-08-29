from __future__ import annotations

from datetime import datetime, timezone

import aiosqlite

from src.core.models import SlotStatus


async def get_slot_state(
    connection: aiosqlite.Connection,
    city_key: str,
) -> tuple[SlotStatus | None, datetime | None, str, str | None]:
    cursor = await connection.execute(
        """
        SELECT slot_state, last_check_at, last_details, last_error
        FROM monitor_state
        WHERE city_key = ?
        """,
        (city_key,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None, None, "", None
    checked_at = None
    if row["last_check_at"]:
        parsed = datetime.fromisoformat(str(row["last_check_at"]))
        checked_at = (
            parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
        )
    return (
        SlotStatus(str(row["slot_state"])),
        checked_at,
        str(row["last_details"] or ""),
        row["last_error"],
    )


async def upsert_slot_state(
    connection: aiosqlite.Connection,
    *,
    city_key: str,
    slot_state: SlotStatus,
    last_check_at: datetime,
    last_details: str,
    last_error: str | None,
) -> None:
    await connection.execute(
        """
        INSERT INTO monitor_state (city_key, slot_state, last_check_at, last_details, last_error)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(city_key) DO UPDATE SET
            slot_state = excluded.slot_state,
            last_check_at = excluded.last_check_at,
            last_details = excluded.last_details,
            last_error = excluded.last_error
        """,
        (
            city_key,
            slot_state.value,
            last_check_at.isoformat(),
            last_details,
            last_error,
        ),
    )
    await connection.commit()
