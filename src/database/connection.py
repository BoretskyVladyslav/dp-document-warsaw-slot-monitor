from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite


class Database:
    def __init__(self, path: str) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("database is not connected")
        return self._conn

    @property
    def write_lock(self) -> asyncio.Lock:
        return self._write_lock

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._write_lock:
            connection = self.connection
            await connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                await connection.rollback()
                raise
            else:
                await connection.commit()

    async def connect(self) -> None:
        await asyncio.to_thread(
            Path(self._path).parent.mkdir,
            parents=True,
            exist_ok=True,
        )
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.execute("PRAGMA journal_mode = WAL")
        await self._conn.execute("PRAGMA busy_timeout = 5000")

    async def close(self) -> None:
        async with self._write_lock:
            if self._conn is not None:
                await self._conn.close()
                self._conn = None
