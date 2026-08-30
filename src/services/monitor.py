from __future__ import annotations

import asyncio
import logging
import math
import random
from datetime import datetime, timedelta, timezone

import aiosqlite

from src.core.config import Settings
from src.core.exceptions import HumanActionRequiredError, RateLimitException, ScraperError
from src.core.models import (
    MonitorSnapshot,
    MonitorStateRecord,
    ScraperFailureCode,
    SlotCheckResult,
    SlotStatus,
)
from src.database.connection import Database
from src.database.monitor_state import get_monitor_state, record_check_attempt
from src.database.subscribers import count_active_subscribers
from src.services.notifier import Notifier
from src.services.scraper import SlotScraper

logger = logging.getLogger(__name__)
_CYCLE_TIMEOUT_SECONDS = 90.0
_MANUAL_CHECK_INTERVAL_SECONDS = 30
_RATE_LIMIT_BASE_COOLDOWN_SECONDS = 900
_RATE_LIMIT_MAX_COOLDOWN_SECONDS = 7200
_UPSTREAM_FAILURE_THRESHOLD = 3


def _jittered_check_delay(base_interval: int) -> int:
    return max(15, base_interval + random.randint(-15, 15))


def _rate_limit_cooldown_seconds(consecutive: int) -> int:
    if consecutive < 1:
        raise ValueError("consecutive rate limits must be positive")
    return min(
        _RATE_LIMIT_BASE_COOLDOWN_SECONDS * (2 ** (consecutive - 1)),
        _RATE_LIMIT_MAX_COOLDOWN_SECONDS,
    )


class SlotMonitor:
    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        scraper: SlotScraper,
        notifier: Notifier,
        started_at: datetime,
    ) -> None:
        self._settings = settings
        self._database = database
        self._scraper = scraper
        self._notifier = notifier
        self._started_at = started_at
        self._last_result: SlotCheckResult | None = None
        self._cached_state = SlotStatus.UNKNOWN
        self._last_verified_at: datetime | None = None
        self._cooldown_until: datetime | None = None
        self._consecutive_rate_limits = 0
        self._consecutive_upstream_failures = 0
        self._circuit_breaker_alerted = False
        self._manual_check_started_at: dict[int, float] = {}
        self._singleflight_guard = asyncio.Lock()
        self._inflight_check: asyncio.Task[SlotCheckResult] | None = None
        self._stopping = False

    @property
    def city_key(self) -> str:
        return self._settings.city_name.strip().lower()

    async def restore_state(self) -> None:
        state = await get_monitor_state(self._database.connection, self.city_key)
        if state is None:
            return
        self._cached_state = state.verified_state
        self._last_verified_at = state.last_verified_at
        now = datetime.now(timezone.utc)
        self._cooldown_until = (
            state.cooldown_until
            if state.cooldown_until is not None and state.cooldown_until > now
            else None
        )

    async def run_once(self) -> SlotCheckResult:
        return await self._run_singleflight(source="run_once")

    async def check_now(self, admin_id: int) -> SlotCheckResult:
        return await self._run_singleflight(source="admin", admin_id=admin_id)

    async def run(self, stop: asyncio.Event) -> None:
        try:
            while not stop.is_set():
                await self._run_singleflight(source="scheduler")
                delay = self._next_check_delay()
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                except TimeoutError:
                    continue
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        self._stopping = True
        async with self._singleflight_guard:
            task = self._inflight_check
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        async with self._singleflight_guard:
            if self._inflight_check is task:
                self._inflight_check = None

    async def snapshot(self) -> MonitorSnapshot:
        active = await count_active_subscribers(self._database.connection)
        persisted = await get_monitor_state(
            self._database.connection,
            self.city_key,
        )
        last = self._last_result
        health = await self._scraper.get_health_snapshot()
        verified_state = (
            persisted.verified_state if persisted is not None else self._cached_state
        )
        last_verified_at = (
            persisted.last_verified_at
            if persisted is not None
            else self._last_verified_at
        )
        last_attempt_at = (
            last.checked_at
            if last is not None
            else (persisted.last_attempt_at if persisted is not None else None)
        )
        uptime = (datetime.now(timezone.utc) - self._started_at).total_seconds()
        cooldown_until = self._active_cooldown_until(persisted)
        return MonitorSnapshot(
            last_check_at=last_verified_at,
            slot_state=verified_state,
            last_details=(
                last.details
                if last is not None
                else (persisted.last_details if persisted is not None else "")
            ),
            last_error=(
                last.error
                if last is not None
                else (persisted.last_error if persisted is not None else None)
            ),
            active_subscribers=active,
            uptime_seconds=uptime,
            city_name=self._settings.city_name,
            target_url=str(self._settings.target_url),
            slots=last.slots if last is not None else (),
            last_attempt_at=last_attempt_at,
            last_verified_at=last_verified_at,
            scraper_health=health,
            cooldown_until=cooldown_until,
        )

    async def _run_singleflight(
        self,
        *,
        source: str,
        admin_id: int | None = None,
    ) -> SlotCheckResult:
        immediate_result: SlotCheckResult | None = None
        async with self._singleflight_guard:
            if self._stopping:
                raise RuntimeError("slot monitor is shutting down")
            task = self._inflight_check
            if task is None or task.done():
                immediate_result = self._cooldown_result(source=source)
                if (
                    immediate_result is None
                    and source == "admin"
                    and admin_id is not None
                ):
                    immediate_result = self._manual_throttle_result(
                        admin_id=admin_id
                    )
                if immediate_result is None:
                    if source == "admin":
                        if admin_id is None:
                            raise ValueError("admin_id is required for manual checks")
                        self._manual_check_started_at[admin_id] = (
                            asyncio.get_running_loop().time()
                        )
                    task = asyncio.create_task(
                        self._execute_cycle_with_timeout(),
                        name=f"slot-check:{self.city_key}",
                    )
                    task.add_done_callback(self._observe_cycle_completion)
                    self._inflight_check = task
        if immediate_result is not None:
            await self._notifier.drain_outbox()
            return immediate_result
        if task is None:
            raise RuntimeError("slot check task was not created")
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                async with self._singleflight_guard:
                    if self._inflight_check is task:
                        self._inflight_check = None

    def _observe_cycle_completion(
        self,
        task: asyncio.Task[SlotCheckResult],
    ) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "slot_check_task_failed",
                exc_info=(type(error), error, error.__traceback__),
                extra={"city": self._settings.city_name, "error": str(error)},
            )

    async def _execute_cycle_with_timeout(self) -> SlotCheckResult:
        try:
            return await asyncio.wait_for(
                self._execute_cycle(),
                timeout=_CYCLE_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            checked_at = datetime.now(timezone.utc)
            result = SlotCheckResult(
                status=SlotStatus.UNKNOWN,
                checked_at=checked_at,
                details=(
                    "monitor cycle exceeded "
                    f"{_CYCLE_TIMEOUT_SECONDS:g} seconds"
                ),
                error=ScraperFailureCode.SCRAPER_ERROR.value,
                failure_code=ScraperFailureCode.SCRAPER_ERROR,
            )
            logger.error(
                "slot_check_timeout",
                extra={
                    "city": self._settings.city_name,
                    "timeout_seconds": _CYCLE_TIMEOUT_SECONDS,
                },
            )
            await self._persist_attempt(result)
            return await self._finalize_cycle(result)

    async def _execute_cycle(self) -> SlotCheckResult:
        try:
            result = await self._scraper.check_availability()
        except RateLimitException as exc:
            self._consecutive_rate_limits += 1
            cooldown_seconds = _rate_limit_cooldown_seconds(
                self._consecutive_rate_limits
            )
            checked_at = datetime.now(timezone.utc)
            cooldown_until = checked_at + timedelta(
                seconds=cooldown_seconds
            )
            self._cooldown_until = cooldown_until
            result = SlotCheckResult(
                status=SlotStatus.UNKNOWN,
                checked_at=checked_at,
                details=(
                    "Server rate limit detected; "
                    f"cooldown active for {cooldown_seconds} seconds"
                ),
                error=exc.failure_code.value,
                failure_code=exc.failure_code,
            )
            try:
                await self._notifier.handle_rate_limit(
                    exc,
                    attempted_at=checked_at,
                    cooldown_until=cooldown_until,
                )
            except aiosqlite.Error as db_error:
                logger.exception(
                    "rate_limit_incident_persist_failed",
                    extra={
                        "city": self._settings.city_name,
                        "error": str(db_error),
                    },
                )
        except HumanActionRequiredError as exc:
            result = SlotCheckResult(
                status=SlotStatus.UNKNOWN,
                checked_at=datetime.now(timezone.utc),
                details=str(exc),
                error=exc.failure_code.value,
                failure_code=exc.failure_code,
            )
            try:
                await self._notifier.handle_human_action_required(
                    exc,
                    attempted_at=result.checked_at,
                )
            except aiosqlite.Error as db_error:
                logger.exception(
                    "human_action_incident_persist_failed",
                    extra={
                        "city": self._settings.city_name,
                        "failure_code": exc.failure_code.value,
                        "error": str(db_error),
                    },
                )
        except ScraperError as exc:
            result = SlotCheckResult(
                status=SlotStatus.UNKNOWN,
                checked_at=datetime.now(timezone.utc),
                details=str(exc),
                error=ScraperFailureCode.SCRAPER_ERROR.value,
                failure_code=ScraperFailureCode.SCRAPER_ERROR,
            )
            await self._persist_attempt(result)
        else:
            if result.status is SlotStatus.UNKNOWN:
                await self._persist_attempt(result)
                if result.failure_code is ScraperFailureCode.SERVER_ERROR:
                    try:
                        await self._notifier.handle_server_error(result)
                    except aiosqlite.Error as db_error:
                        logger.exception(
                            "server_error_incident_persist_failed",
                            extra={
                                "city": self._settings.city_name,
                                "error": str(db_error),
                            },
                        )
            else:
                self._consecutive_rate_limits = 0
                self._cooldown_until = None
                self._cached_state = result.status
                self._last_verified_at = result.checked_at
                try:
                    await self._notifier.handle_verified_result(result)
                except aiosqlite.Error as db_error:
                    logger.exception(
                        "verified_result_persist_failed",
                        extra={
                            "city": self._settings.city_name,
                            "status": result.status.value,
                            "error": str(db_error),
                        },
                    )

        return await self._finalize_cycle(result)

    async def _finalize_cycle(self, result: SlotCheckResult) -> SlotCheckResult:
        self._last_result = result
        if result.status is SlotStatus.UNKNOWN:
            self._consecutive_upstream_failures += 1
            if (
                self._consecutive_upstream_failures >= _UPSTREAM_FAILURE_THRESHOLD
                and not self._circuit_breaker_alerted
            ):
                try:
                    await self._notifier.handle_circuit_breaker(
                        attempted_at=result.checked_at,
                        consecutive_failures=self._consecutive_upstream_failures,
                        next_interval_seconds=(
                            self._settings.check_interval_seconds * 2
                        ),
                    )
                    self._circuit_breaker_alerted = True
                except aiosqlite.Error as db_error:
                    logger.exception(
                        "circuit_breaker_incident_persist_failed",
                        extra={
                            "city": self._settings.city_name,
                            "error": str(db_error),
                        },
                    )
        else:
            self._consecutive_upstream_failures = 0
            self._circuit_breaker_alerted = False
        log_extra = {
            "city": self._settings.city_name,
            "status": result.status.value,
            "error": result.error,
            "failure_code": (
                result.failure_code.value
                if result.failure_code is not None
                else None
            ),
        }
        if result.failure_code is ScraperFailureCode.SERVER_ERROR:
            logger.warning("site_backend_error", extra=log_extra)
        else:
            logger.info("slot_check", extra=log_extra)
        await self._notifier.drain_outbox()
        return result

    def _manual_throttle_result(self, *, admin_id: int) -> SlotCheckResult | None:
        last_started_at = self._manual_check_started_at.get(admin_id)
        if last_started_at is None:
            return None
        elapsed = asyncio.get_running_loop().time() - last_started_at
        remaining = math.ceil(_MANUAL_CHECK_INTERVAL_SECONDS - elapsed)
        if remaining <= 0:
            return None
        now = datetime.now(timezone.utc)
        logger.info(
            "manual_check_throttled",
            extra={
                "admin_id": admin_id,
                "city": self._settings.city_name,
                "remaining_seconds": remaining,
            },
        )
        return SlotCheckResult(
            status=SlotStatus.UNKNOWN,
            checked_at=now,
            details=f"Manual check throttled; retry in {remaining} seconds.",
        )

    def _next_check_delay(self) -> int:
        interval = self._settings.check_interval_seconds
        if self._consecutive_upstream_failures >= _UPSTREAM_FAILURE_THRESHOLD:
            interval *= 2
        return _jittered_check_delay(interval)

    def _cooldown_result(self, *, source: str) -> SlotCheckResult | None:
        cooldown_until = self._cooldown_until
        if cooldown_until is None:
            return None
        now = datetime.now(timezone.utc)
        remaining = math.ceil((cooldown_until - now).total_seconds())
        if remaining <= 0:
            self._cooldown_until = None
            return None
        logger.info(
            "rate_limit_cooldown_active",
            extra={
                "city": self._settings.city_name,
                "remaining_seconds": remaining,
                "source": source,
            },
        )
        return SlotCheckResult(
            status=SlotStatus.UNKNOWN,
            checked_at=now,
            details=f"Rate-limit cooldown active; {remaining} seconds remaining.",
            error=ScraperFailureCode.RATE_LIMITED.value,
            failure_code=ScraperFailureCode.RATE_LIMITED,
        )

    def _active_cooldown_until(
        self,
        persisted: MonitorStateRecord | None,
    ) -> datetime | None:
        persisted_until = persisted.cooldown_until if persisted is not None else None
        candidates = [
            value
            for value in (self._cooldown_until, persisted_until)
            if isinstance(value, datetime)
        ]
        if not candidates:
            return None
        latest = max(candidates)
        return latest if latest > datetime.now(timezone.utc) else None

    async def _persist_attempt(self, result: SlotCheckResult) -> None:
        try:
            async with self._database.write_lock:
                await record_check_attempt(
                    self._database.connection,
                    city_key=self.city_key,
                    attempted_at=result.checked_at,
                    details=result.details,
                    error=result.error,
                )
        except aiosqlite.Error as exc:
            logger.exception(
                "check_attempt_persist_failed",
                extra={
                    "city": self._settings.city_name,
                    "error": str(exc),
                },
            )
