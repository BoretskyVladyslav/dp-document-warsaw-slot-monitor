from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import StrEnum

from src.core.exceptions import DeliveryError, RecipientUnreachableError
from src.core.models import SlotCheckResult, SlotStatus, Subscriber
from src.core.protocols import MessageSender
from src.database.connection import Database
from src.database.subscribers import get_all_active_subscribers, remove_subscriber

logger = logging.getLogger(__name__)

_DISPATCH_CONCURRENCY = 20


class _SendOutcome(StrEnum):
    DELIVERED = "delivered"
    UNREACHABLE = "unreachable"
    TRANSIENT = "transient"


@dataclass(frozen=True, slots=True)
class _DispatchOutcome:
    delivered: int = 0
    unreachable: int = 0
    transient_failed: int = 0

    @property
    def should_commit(self) -> bool:
        if self.delivered > 0:
            return True
        if self.transient_failed > 0:
            return False
        return True


def format_slots_available(
    *,
    city_name: str,
    target_url: str,
    details: str,
    slots: tuple[str, ...],
) -> str:
    slot_lines = "\n".join(f"• {item}" for item in slots[:15]) or details
    extra = ""
    if len(slots) > 15:
        extra = f"\n… ще {len(slots) - 15}"
    return (
        f"З'явилися вільні слоти — {city_name}\n\n"
        f"{slot_lines}{extra}\n\n"
        f"Запис: {target_url}"
    )


def format_slots_gone(*, city_name: str) -> str:
    return (
        f"Вільні слоти в {city_name} вже зайняті. "
        "Бот знову повідомить, коли з'являться нові."
    )


class Notifier:
    def __init__(
        self,
        *,
        database: Database,
        sender: MessageSender,
        city_name: str,
        target_url: str,
    ) -> None:
        self._database = database
        self._sender = sender
        self._city_name = city_name
        self._target_url = target_url
        self._previous: SlotStatus | None = None

    def prime_previous(self, state: SlotStatus | None) -> None:
        self._previous = state

    async def handle_check(self, result: SlotCheckResult) -> SlotStatus | None:
        if result.status is SlotStatus.UNKNOWN:
            return self._previous

        previous = self._previous if self._previous is not None else SlotStatus.NO_SLOTS
        if previous is result.status:
            self._previous = result.status
            return result.status

        subscribers = await get_all_active_subscribers(self._database.connection)
        text: str | None = None
        event_name: str | None = None
        if previous is SlotStatus.NO_SLOTS and result.status is SlotStatus.FREE_SLOTS_AVAILABLE:
            text = format_slots_available(
                city_name=self._city_name,
                target_url=self._target_url,
                details=result.details,
                slots=result.slots,
            )
            event_name = "notified_slots_available"
        elif previous is SlotStatus.FREE_SLOTS_AVAILABLE and result.status is SlotStatus.NO_SLOTS:
            text = format_slots_gone(city_name=self._city_name)
            event_name = "notified_slots_gone"

        if text is None:
            self._previous = result.status
            return result.status

        outcome = await self._dispatch(subscribers, text)
        if not outcome.should_commit:
            logger.warning(
                "notify_deferred",
                extra={
                    "city": self._city_name,
                    "delivered": outcome.delivered,
                    "unreachable": outcome.unreachable,
                    "transient_failed": outcome.transient_failed,
                    "pending_status": result.status.value,
                },
            )
            return self._previous

        self._previous = result.status
        logger.info(
            event_name,
            extra={"city": self._city_name, "recipients": len(subscribers)},
        )
        return result.status

    async def _dispatch(self, subscribers: list[Subscriber], text: str) -> _DispatchOutcome:
        semaphore = asyncio.Semaphore(_DISPATCH_CONCURRENCY)

        async def _send_one(subscriber: Subscriber) -> _SendOutcome:
            async with semaphore:
                try:
                    await self._sender.send(subscriber.chat_id, text)
                    return _SendOutcome.DELIVERED
                except RecipientUnreachableError:
                    logger.warning(
                        "unsubscribed_unreachable",
                        extra={"user_id": subscriber.user_id, "chat_id": subscriber.chat_id},
                    )
                    await remove_subscriber(self._database.connection, subscriber.user_id)
                    return _SendOutcome.UNREACHABLE
                except (DeliveryError, OSError, TimeoutError) as exc:
                    logger.warning(
                        "notify_failed",
                        extra={
                            "user_id": subscriber.user_id,
                            "chat_id": subscriber.chat_id,
                            "error": str(exc),
                        },
                    )
                    return _SendOutcome.TRANSIENT
                except Exception as exc:
                    logger.exception(
                        "notify_unexpected",
                        extra={
                            "user_id": subscriber.user_id,
                            "chat_id": subscriber.chat_id,
                            "error": str(exc),
                        },
                    )
                    return _SendOutcome.TRANSIENT

        raw_results = await asyncio.gather(
            *(_send_one(item) for item in subscribers),
            return_exceptions=True,
        )
        delivered = 0
        unreachable = 0
        transient_failed = 0
        for item in raw_results:
            if item is _SendOutcome.DELIVERED:
                delivered += 1
            elif item is _SendOutcome.UNREACHABLE:
                unreachable += 1
            else:
                transient_failed += 1
        return _DispatchOutcome(
            delivered=delivered,
            unreachable=unreachable,
            transient_failed=transient_failed,
        )
