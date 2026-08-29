from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from src.handlers.texts import format_help, format_start_confirmation, format_stop_confirmation
from src.services.subscription import SubscriptionService

router = Router(name="user_commands")


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    subscription_service: SubscriptionService,
    city_name: str,
) -> None:
    user = message.from_user
    if user is None:
        return
    await subscription_service.subscribe(
        user_id=user.id,
        chat_id=message.chat.id,
        username=user.username,
    )
    await message.answer(format_start_confirmation(city_name))


@router.message(Command("stop"))
async def cmd_stop(message: Message, subscription_service: SubscriptionService) -> None:
    user = message.from_user
    if user is None:
        return
    await subscription_service.unsubscribe(user.id)
    await message.answer(format_stop_confirmation())


@router.message(Command("help"))
async def cmd_help(message: Message, city_name: str) -> None:
    await message.answer(format_help(city_name))
