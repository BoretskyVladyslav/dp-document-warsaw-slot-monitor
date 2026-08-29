from aiogram.filters import BaseFilter
from aiogram.types import Message


class AdminFilter(BaseFilter):
    def __init__(self, admin_ids: frozenset[int]) -> None:
        self._admin_ids = admin_ids

    async def __call__(self, message: Message) -> bool:
        return message.chat.id in self._admin_ids
