from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from src.handlers.filters import AdminFilter
from src.handlers.texts import format_admin_only, format_check_result, format_status
from src.services.status import StatusService


def build_admin_router(admin_ids: frozenset[int]) -> Router:
    router = Router(name="admin_commands")

    @router.message(Command("status"))
    async def cmd_status(message: Message, status_service: StatusService) -> None:
        snapshot = await status_service.get_snapshot()
        await message.answer(
            format_status(
                snapshot,
                is_admin=message.chat.id in admin_ids,
            )
        )

    @router.message(Command("check_now"), AdminFilter(admin_ids))
    async def cmd_check_now(
        message: Message,
        status_service: StatusService,
    ) -> None:
        result = await status_service.check_now(admin_id=message.chat.id)
        await message.answer(format_check_result(result))

    @router.message(Command("check_now"))
    async def cmd_check_now_denied(message: Message) -> None:
        await message.answer(format_admin_only())

    return router
