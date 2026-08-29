from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from src.handlers.filters import AdminFilter
from src.handlers.texts import format_admin_only, format_status
from src.services.status import StatusService


def build_admin_router(admin_ids: frozenset[int]) -> Router:
    router = Router(name="admin_commands")

    @router.message(Command("status"), AdminFilter(admin_ids))
    async def cmd_status(message: Message, status_service: StatusService) -> None:
        snapshot = await status_service.get_snapshot()
        await message.answer(format_status(snapshot))

    @router.message(Command("status"))
    async def cmd_status_denied(message: Message) -> None:
        await message.answer(format_admin_only())

    return router
