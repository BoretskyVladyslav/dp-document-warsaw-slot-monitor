from __future__ import annotations

from src.core.models import Subscriber
from src.database.connection import Database
from src.database.subscribers import add_subscriber, remove_subscriber, toggle_subscription


class SubscriptionService:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def subscribe(
        self,
        *,
        user_id: int,
        chat_id: int,
        username: str | None,
    ) -> Subscriber:
        async with self._database.write_lock:
            return await add_subscriber(
                self._database.connection,
                user_id=user_id,
                chat_id=chat_id,
                username=username,
            )

    async def unsubscribe(self, user_id: int) -> bool:
        async with self._database.write_lock:
            return await remove_subscriber(self._database.connection, user_id)

    async def toggle(self, user_id: int) -> Subscriber | None:
        async with self._database.write_lock:
            return await toggle_subscription(self._database.connection, user_id)
