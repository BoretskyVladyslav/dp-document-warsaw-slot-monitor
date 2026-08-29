from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)

from src.core.exceptions import DeliveryError, RecipientUnreachableError

logger = logging.getLogger(__name__)

_UNREACHABLE_TOKENS = (
    "chat not found",
    "bot was blocked",
    "bot blocked by the user",
    "user is deactivated",
    "deactivated",
    "forbidden",
)


class AiogramMessageSender:
    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def send(self, chat_id: int, text: str) -> None:
        try:
            await self._deliver(chat_id, text)
        except TelegramRetryAfter as exc:
            logger.warning(
                "telegram_retry_after",
                extra={"chat_id": chat_id, "retry_after": exc.retry_after},
            )
            await asyncio.sleep(exc.retry_after)
            try:
                await self._deliver(chat_id, text)
            except TelegramAPIError as retry_exc:
                self._raise_mapped(chat_id, retry_exc)
        except TelegramAPIError as exc:
            self._raise_mapped(chat_id, exc)

    async def _deliver(self, chat_id: int, text: str) -> None:
        await self._bot.send_message(chat_id, text, disable_web_page_preview=True)

    def _raise_mapped(self, chat_id: int, exc: TelegramAPIError) -> None:
        if isinstance(exc, TelegramForbiddenError) or _is_unreachable(exc):
            raise RecipientUnreachableError(chat_id) from exc
        raise DeliveryError(chat_id, reason=str(exc)) from exc


def _is_unreachable(exc: TelegramAPIError) -> bool:
    if isinstance(exc, TelegramBadRequest):
        lowered = str(exc).lower()
        return any(token in lowered for token in _UNREACHABLE_TOKENS)
    if isinstance(exc, TelegramNetworkError):
        return False
    lowered = str(exc).lower()
    return any(token in lowered for token in _UNREACHABLE_TOKENS)
