from __future__ import annotations

from datetime import datetime, timezone

import aiosqlite

from src.core.models import SlotStatus
from src.core.models import MonitorStateRecord


def _parse_timestamp(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    parsed = datetime.fromisoformat(str(value))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


async def get_monitor_state(
    connection: aiosqlite.Connection,
    city_key: str,
) -> MonitorStateRecord | None:
    cursor = await connection.execute(
        """
        SELECT
            city_key,
            last_verified_state,
            last_verified_at,
            last_attempt_at,
            last_details,
            last_error,
            human_action_incident_key,
            human_action_incident_notified_at
        FROM monitor_state
        WHERE city_key = ?
        """,
        (city_key,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    if row is None:
        return None
    return MonitorStateRecord(
        city_key=str(row["city_key"]),
        verified_state=SlotStatus(str(row["last_verified_state"])),
        last_verified_at=_parse_timestamp(row["last_verified_at"]),
        last_attempt_at=_parse_timestamp(row["last_attempt_at"]),
        last_details=str(row["last_details"] or ""),
        last_error=str(row["last_error"]) if row["last_error"] is not None else None,
        human_action_incident_key=(
            str(row["human_action_incident_key"])
            if row["human_action_incident_key"] is not None
            else None
        ),
        human_action_incident_notified_at=_parse_timestamp(
            row["human_action_incident_notified_at"]
        ),
    )


async def get_slot_state(
    connection: aiosqlite.Connection,
    city_key: str,
) -> tuple[SlotStatus | None, datetime | None, str, str | None]:
    state = await get_monitor_state(connection, city_key)
    if state is None:
        return None, None, "", None
    legacy_state = (
        None if state.verified_state is SlotStatus.UNKNOWN else state.verified_state
    )
    return (
        legacy_state,
        state.last_attempt_at,
        state.last_details,
        state.last_error,
    )


async def record_check_attempt(
    connection: aiosqlite.Connection,
    *,
    city_key: str,
    attempted_at: datetime,
    details: str,
    error: str | None,
) -> None:
    attempted = attempted_at.isoformat()
    await connection.execute(
        """
        INSERT INTO monitor_state (
            city_key,
            slot_state,
            last_check_at,
            last_details,
            last_error,
            last_verified_state,
            last_attempt_at
        )
        VALUES (?, 'UNKNOWN', ?, ?, ?, 'UNKNOWN', ?)
        ON CONFLICT(city_key) DO UPDATE SET
            last_check_at = excluded.last_check_at,
            last_attempt_at = excluded.last_attempt_at,
            last_details = excluded.last_details,
            last_error = excluded.last_error
        """,
        (city_key, attempted, details, error, attempted),
    )
    await connection.commit()


async def record_verified_state(
    connection: aiosqlite.Connection,
    *,
    city_key: str,
    slot_state: SlotStatus,
    verified_at: datetime,
    details: str,
) -> None:
    if slot_state is SlotStatus.UNKNOWN:
        raise ValueError("verified slot state cannot be UNKNOWN")
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
            last_attempt_at
        )
        VALUES (?, ?, ?, ?, NULL, ?, ?, ?)
        ON CONFLICT(city_key) DO UPDATE SET
            slot_state = excluded.slot_state,
            last_check_at = excluded.last_check_at,
            last_details = excluded.last_details,
            last_error = NULL,
            last_verified_state = excluded.last_verified_state,
            last_verified_at = excluded.last_verified_at,
            last_attempt_at = excluded.last_attempt_at
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
    await connection.commit()


async def set_human_action_incident(
    connection: aiosqlite.Connection,
    *,
    city_key: str,
    incident_key: str,
    attempted_at: datetime,
    details: str,
    error: str,
) -> bool:
    current = await get_monitor_state(connection, city_key)
    is_new = current is None or current.human_action_incident_key != incident_key
    attempted = attempted_at.isoformat()
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
            human_action_incident_notified_at
        )
        VALUES (?, 'UNKNOWN', ?, ?, ?, 'UNKNOWN', ?, ?, NULL)
        ON CONFLICT(city_key) DO UPDATE SET
            last_check_at = excluded.last_check_at,
            last_attempt_at = excluded.last_attempt_at,
            last_details = excluded.last_details,
            last_error = excluded.last_error,
            human_action_incident_key = excluded.human_action_incident_key,
            human_action_incident_notified_at = CASE
                WHEN monitor_state.human_action_incident_key = excluded.human_action_incident_key
                THEN monitor_state.human_action_incident_notified_at
                ELSE NULL
            END
        """,
        (city_key, attempted, details, error, attempted, incident_key),
    )
    await connection.commit()
    return is_new


async def claim_human_action_incident_notification(
    connection: aiosqlite.Connection,
    *,
    city_key: str,
    incident_key: str,
    notified_at: datetime,
) -> bool:
    cursor = await connection.execute(
        """
        UPDATE monitor_state
        SET human_action_incident_notified_at = ?
        WHERE city_key = ?
          AND human_action_incident_key = ?
          AND human_action_incident_notified_at IS NULL
        """,
        (notified_at.isoformat(), city_key, incident_key),
    )
    await connection.commit()
    claimed = cursor.rowcount == 1
    await cursor.close()
    return claimed


async def clear_human_action_incident(
    connection: aiosqlite.Connection,
    *,
    city_key: str,
) -> None:
    await connection.execute(
        """
        UPDATE monitor_state
        SET human_action_incident_key = NULL,
            human_action_incident_notified_at = NULL
        WHERE city_key = ?
        """,
        (city_key,),
    )
    await connection.commit()


async def upsert_slot_state(
    connection: aiosqlite.Connection,
    *,
    city_key: str,
    slot_state: SlotStatus,
    last_check_at: datetime,
    last_details: str,
    last_error: str | None,
) -> None:
    if last_error is not None or slot_state is SlotStatus.UNKNOWN:
        await record_check_attempt(
            connection,
            city_key=city_key,
            attempted_at=last_check_at,
            details=last_details,
            error=last_error,
        )
        return
    await record_verified_state(
        connection,
        city_key=city_key,
        slot_state=slot_state,
        verified_at=last_check_at,
        details=last_details,
    )
