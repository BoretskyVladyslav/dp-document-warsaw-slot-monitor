from __future__ import annotations

import aiosqlite

SCHEMA_STATEMENTS: tuple[str, ...] = (
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
        slot_state TEXT NOT NULL,
        last_check_at TEXT,
        last_details TEXT,
        last_error TEXT
    )
    """,
)


async def init_schema(connection: aiosqlite.Connection) -> None:
    for statement in SCHEMA_STATEMENTS:
        await connection.execute(statement)
    await connection.commit()
