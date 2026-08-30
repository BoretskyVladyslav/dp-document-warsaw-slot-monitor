from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum

import aiosqlite

from src.core.exceptions import (
    DeliveryError,
    HumanActionRequiredError,
    RateLimitException,
    RecipientUnreachableError,
)
from src.core.models import (
    NotificationEventType,
    NotificationRecipient,
    PendingNotificationDelivery,
    ScraperFailureCode,
    SlotCheckResult,
    SlotStatus,
)
from src.core.protocols import MessageSender
from src.database.connection import Database
from src.database.notification_outbox import (
    enqueue_notification_event,
    list_pending_deliveries,
    mark_delivery_delivered,
    mark_delivery_failed,
    mark_delivery_unreachable,
    persist_human_action_incident,
    persist_verified_result,
)
from src.database.subscribers import remove_subscriber

logger = logging.getLogger(__name__)
_DISPATCH_CONCURRENCY = 20
_OUTBOX_BATCH_SIZE = 500


def format_rate_limit_alert(
    *,
    city_name: str,
    cooldown_seconds: int,
    cooldown_until: datetime,
) -> str:
    until = cooldown_until.isoformat(sep=" ", timespec="seconds")
    return (
        f"⏸️ Rate limit — {city_name}\n\n"
        f"Cooldown: {cooldown_seconds} seconds\n"
        f"Until: {until} UTC\n"
        "Scheduled and /check_now checks will skip until then."
    )


class _SendOutcome(StrEnum):
    DELIVERED = "delivered"
    UNREACHABLE = "unreachable"
    TRANSIENT = "transient"


@dataclass(frozen=True, slots=True)
class _SendResult:
    delivery: PendingNotificationDelivery
    outcome: _SendOutcome
    error: str = ""


@dataclass(frozen=True, slots=True)
class DispatchSummary:
    delivered: int = 0
    unreachable: int = 0
    transient_failed: int = 0


def format_slots_available(
    *,
    city_name: str,
    target_url: str,
    details: str,
    slots: tuple[str, ...],
) -> str:
    slot_lines = "\n".join(f"• {item}" for item in slots[:15]) or details
    extra = f"\n… ще {len(slots) - 15}" if len(slots) > 15 else ""
    return (
        f"З'явилися вільні слоти — {city_name}\n\n"
        f"{slot_lines}{extra}\n\n"
        f"Запис: {target_url}"
    )


def format_human_action_required(
    *,
    city_name: str,
    target_url: str,
    failure_code: ScraperFailureCode,
) -> str:
    actions = {
        ScraperFailureCode.CDP_UNAVAILABLE: (
            "Запустіть dedicated CDP Chrome і перевірте CDP_URL."
        ),
        ScraperFailureCode.TARGET_TAB_MISSING: (
            "Відкрийте сторінку запису в dedicated CDP Chrome."
        ),
        ScraperFailureCode.TARGET_TAB_CLOSED: (
            "Повторно відкрийте сторінку запису в dedicated CDP Chrome."
        ),
        ScraperFailureCode.CLOUDFLARE_CHALLENGE: (
            "Вручну пройдіть перевірку Cloudflare у відкритій вкладці."
        ),
    }
    action = actions.get(
        failure_code,
        "Перевірте вкладку запису в dedicated CDP Chrome.",
    )
    return (
        f"⚠️ Моніторинг потребує втручання — {city_name}\n\n"
        f"Причина: {failure_code.value}\n"
        f"{action}\n\n"
        f"URL: {target_url}"
    )


def format_circuit_breaker_alert(
    *,
    city_name: str,
    consecutive_failures: int,
    next_interval_seconds: int,
) -> str:
    return (
        f"⚠️ Circuit breaker — {city_name}\n\n"
        f"{consecutive_failures} consecutive UNKNOWN/server_error results.\n"
        f"Next poll interval is {next_interval_seconds} seconds "
        "(double the configured interval) until a conclusive verification."
    )


class Notifier:
    def __init__(
        self,
        *,
        database: Database,
        sender: MessageSender,
        city_name: str,
        target_url: str,
        admin_ids: list[int] | None = None,
    ) -> None:
        self._database = database
        self._sender = sender
        self._city_name = city_name
        self._city_key = city_name.strip().lower()
        self._target_url = target_url
        self._admin_ids = tuple(dict.fromkeys(admin_ids or ()))
        self._drain_lock = asyncio.Lock()

    async def handle_verified_result(self, result: SlotCheckResult) -> bool:
        if result.status is SlotStatus.UNKNOWN:
            raise ValueError("verified result cannot be UNKNOWN")
        text = format_slots_available(
            city_name=self._city_name,
            target_url=self._target_url,
            details=result.details,
            slots=result.slots,
        )
        admin_recipients = [
            NotificationRecipient(chat_id=admin_id)
            for admin_id in self._admin_ids
        ]
        previous, transitioned, event_id = await persist_verified_result(
            self._database,
            city_key=self._city_key,
            slot_state=result.status,
            verified_at=result.checked_at,
            details=result.details,
            notification_text=text,
            recipients=admin_recipients,
        )
        logger.info(
            "verified_slot_state_persisted",
            extra={
                "city": self._city_name,
                "previous": previous.value,
                "current": result.status.value,
                "transitioned": transitioned,
                "event_id": event_id,
            },
        )
        return transitioned

    async def handle_human_action_required(
        self,
        error: HumanActionRequiredError,
        *,
        attempted_at: datetime,
    ) -> bool:
        text = format_human_action_required(
            city_name=self._city_name,
            target_url=self._target_url,
            failure_code=error.failure_code,
        )
        recipients = [
            NotificationRecipient(chat_id=admin_id)
            for admin_id in self._admin_ids
        ]
        is_new, event_id = await persist_human_action_incident(
            self._database,
            city_key=self._city_key,
            failure_code=error.failure_code,
            attempted_at=attempted_at,
            details=str(error),
            notification_text=text,
            recipients=recipients,
        )
        logger.warning(
            "human_action_incident_recorded",
            extra={
                "city": self._city_name,
                "failure_code": error.failure_code.value,
                "new_incident": is_new,
                "event_id": event_id,
            },
        )
        return is_new

    async def handle_rate_limit(
        self,
        error: RateLimitException,
        *,
        attempted_at: datetime,
        cooldown_until: datetime,
    ) -> bool:
        cooldown_seconds = max(
            0,
            int((cooldown_until - attempted_at).total_seconds()),
        )
        recipients = [
            NotificationRecipient(chat_id=admin_id)
            for admin_id in self._admin_ids
        ]
        is_new, event_id = await persist_human_action_incident(
            self._database,
            city_key=self._city_key,
            failure_code=error.failure_code,
            attempted_at=attempted_at,
            details=str(error),
            notification_text=format_rate_limit_alert(
                city_name=self._city_name,
                cooldown_seconds=cooldown_seconds,
                cooldown_until=cooldown_until,
            ),
            recipients=recipients,
            cooldown_until=cooldown_until,
        )
        logger.warning(
            "rate_limit_cooldown_recorded",
            extra={
                "city": self._city_name,
                "cooldown_until": cooldown_until.isoformat(),
                "cooldown_seconds": cooldown_seconds,
                "new_incident": is_new,
                "event_id": event_id,
            },
        )
        return is_new

    async def handle_server_error(self, result: SlotCheckResult) -> bool:
        logger.warning(
            "site_backend_error_telegram_muted",
            extra={
                "city": self._city_name,
                "details": result.details,
                "error": result.error,
            },
        )
        return False

    async def handle_circuit_breaker(
        self,
        *,
        attempted_at: datetime,
        consecutive_failures: int,
        next_interval_seconds: int,
    ) -> bool:
        recipients = [
            NotificationRecipient(chat_id=admin_id)
            for admin_id in self._admin_ids
        ]
        if not recipients:
            return False
        text = format_circuit_breaker_alert(
            city_name=self._city_name,
            consecutive_failures=consecutive_failures,
            next_interval_seconds=next_interval_seconds,
        )
        async with self._database.write_lock:
            event_id = await enqueue_notification_event(
                self._database.connection,
                city_key=self._city_key,
                event_key=f"incident:circuit_breaker:{attempted_at.isoformat()}",
                event_type=NotificationEventType.HUMAN_ACTION_REQUIRED,
                text=text,
                recipients=recipients,
                created_at=attempted_at,
            )
        logger.warning(
            "circuit_breaker_recorded",
            extra={
                "city": self._city_name,
                "consecutive_failures": consecutive_failures,
                "next_interval_seconds": next_interval_seconds,
                "event_id": event_id,
            },
        )
        return True

    async def drain_outbox(self) -> DispatchSummary:
        async with self._drain_lock:
            try:
                deliveries = await list_pending_deliveries(
                    self._database.connection,
                    city_key=self._city_key,
                    limit=_OUTBOX_BATCH_SIZE,
                )
            except aiosqlite.Error as exc:
                logger.exception(
                    "outbox_read_failed",
                    extra={"city": self._city_name, "error": str(exc)},
                )
                return DispatchSummary(transient_failed=1)
            if not deliveries:
                return DispatchSummary()

            semaphore = asyncio.Semaphore(_DISPATCH_CONCURRENCY)

            async def _send(
                delivery: PendingNotificationDelivery,
            ) -> _SendResult:
                async with semaphore:
                    try:
                        await self._sender.send(delivery.chat_id, delivery.text)
                        return _SendResult(delivery, _SendOutcome.DELIVERED)
                    except RecipientUnreachableError as exc:
                        return _SendResult(
                            delivery,
                            _SendOutcome.UNREACHABLE,
                            str(exc),
                        )
                    except (DeliveryError, OSError, TimeoutError) as exc:
                        return _SendResult(
                            delivery,
                            _SendOutcome.TRANSIENT,
                            str(exc),
                        )
                    except Exception as exc:
                        logger.exception(
                            "notify_unexpected",
                            extra={
                                "city": self._city_name,
                                "event_id": delivery.event_id,
                                "chat_id": delivery.chat_id,
                                "error": str(exc),
                            },
                        )
                        return _SendResult(
                            delivery,
                            _SendOutcome.TRANSIENT,
                            str(exc),
                        )

            results = await asyncio.gather(*(_send(item) for item in deliveries))
            delivered = 0
            unreachable = 0
            transient_failed = 0
            for result in results:
                try:
                    async with self._database.write_lock:
                        if result.outcome is _SendOutcome.DELIVERED:
                            await mark_delivery_delivered(
                                self._database.connection,
                                event_id=result.delivery.event_id,
                                chat_id=result.delivery.chat_id,
                                delivered_at=datetime.now(timezone.utc),
                            )
                            delivered += 1
                        elif result.outcome is _SendOutcome.UNREACHABLE:
                            await mark_delivery_unreachable(
                                self._database.connection,
                                event_id=result.delivery.event_id,
                                chat_id=result.delivery.chat_id,
                                error=result.error,
                            )
                            if result.delivery.user_id is not None:
                                await remove_subscriber(
                                    self._database.connection,
                                    result.delivery.user_id,
                                )
                            unreachable += 1
                        else:
                            await mark_delivery_failed(
                                self._database.connection,
                                event_id=result.delivery.event_id,
                                chat_id=result.delivery.chat_id,
                                error=result.error,
                            )
                            transient_failed += 1
                except aiosqlite.Error as exc:
                    transient_failed += 1
                    logger.exception(
                        "outbox_update_failed",
                        extra={
                            "city": self._city_name,
                            "event_id": result.delivery.event_id,
                            "chat_id": result.delivery.chat_id,
                            "error": str(exc),
                        },
                    )
            summary = DispatchSummary(
                delivered=delivered,
                unreachable=unreachable,
                transient_failed=transient_failed,
            )
            logger.info(
                "outbox_drained",
                extra={
                    "city": self._city_name,
                    "delivered": summary.delivered,
                    "unreachable": summary.unreachable,
                    "transient_failed": summary.transient_failed,
                },
            )
            return summary
