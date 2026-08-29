from __future__ import annotations

import aiosqlite

SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS subscribers (
        user_id INTEGER PRIMARY KEY,
        chat_id INTEGER NOT NULL,
        username TEXT,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS monitor_state (
        city_key TEXT PRIMARY KEY,
        slot_state TEXT NOT NULL DEFAULT 'UNKNOWN'
            CHECK (slot_state IN ('UNKNOWN', 'NO_SLOTS', 'FREE_SLOTS_AVAILABLE')),
        last_check_at TEXT,
        last_details TEXT,
        last_error TEXT,
        last_verified_state TEXT NOT NULL DEFAULT 'UNKNOWN'
            CHECK (last_verified_state IN ('UNKNOWN', 'NO_SLOTS', 'FREE_SLOTS_AVAILABLE')),
        last_verified_at TEXT,
        last_attempt_at TEXT,
        human_action_incident_key TEXT,
        human_action_incident_notified_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS notification_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        city_key TEXT NOT NULL,
        event_key TEXT NOT NULL,
        event_type TEXT NOT NULL
            CHECK (event_type IN ('SLOTS_AVAILABLE', 'HUMAN_ACTION_REQUIRED')),
        text TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (city_key, event_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS notification_deliveries (
        event_id INTEGER NOT NULL,
        chat_id INTEGER NOT NULL,
        user_id INTEGER,
        status TEXT NOT NULL DEFAULT 'PENDING'
            CHECK (status IN ('PENDING', 'DELIVERED', 'UNREACHABLE')),
        attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
        last_error TEXT,
        delivered_at TEXT,
        PRIMARY KEY (event_id, chat_id),
        FOREIGN KEY (event_id) REFERENCES notification_events(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_notification_deliveries_pending
    ON notification_deliveries(status, event_id, chat_id)
    """,
)

_MONITOR_STATE_ADDITIONS: tuple[tuple[str, str], ...] = (
    (
        "last_verified_state",
        "TEXT NOT NULL DEFAULT 'UNKNOWN' "
        "CHECK (last_verified_state IN ('UNKNOWN', 'NO_SLOTS', 'FREE_SLOTS_AVAILABLE'))",
    ),
    ("last_verified_at", "TEXT"),
    ("last_attempt_at", "TEXT"),
    ("human_action_incident_key", "TEXT"),
    ("human_action_incident_notified_at", "TEXT"),
)


async def init_schema(connection: aiosqlite.Connection) -> None:
    try:
        for statement in SCHEMA_STATEMENTS:
            await connection.execute(statement)
        await _migrate_legacy_monitor_state(connection)
        await connection.execute(
            """
            INSERT OR IGNORE INTO schema_migrations (version, applied_at)
            VALUES (1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            """
        )
        await connection.commit()
    except aiosqlite.Error:
        await connection.rollback()
        raise


async def _migrate_legacy_monitor_state(connection: aiosqlite.Connection) -> None:
    cursor = await connection.execute("PRAGMA table_info(monitor_state)")
    rows = await cursor.fetchall()
    await cursor.close()
    existing = {str(row[1]) for row in rows}

    for column_name, declaration in _MONITOR_STATE_ADDITIONS:
        if column_name in existing:
            continue
        await connection.execute(
            f"ALTER TABLE monitor_state ADD COLUMN {column_name} {declaration}"
        )

    await connection.execute(
        """
        UPDATE monitor_state
        SET last_verified_state = slot_state
        WHERE last_verified_state = 'UNKNOWN'
          AND slot_state IN ('NO_SLOTS', 'FREE_SLOTS_AVAILABLE')
        """
    )
    await connection.execute(
        """
        UPDATE monitor_state
        SET last_verified_at = last_check_at
        WHERE last_verified_at IS NULL
          AND last_verified_state IN ('NO_SLOTS', 'FREE_SLOTS_AVAILABLE')
        """
    )
    await connection.execute(
        """
        UPDATE monitor_state
        SET last_attempt_at = last_check_at
        WHERE last_attempt_at IS NULL
        """
    )
