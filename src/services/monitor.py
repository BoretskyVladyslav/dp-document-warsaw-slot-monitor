from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone

import aiosqlite

from src.core.config import Settings
from src.core.exceptions import HumanActionRequiredError, ScraperError
from src.core.models import (
    MonitorSnapshot,
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

    async def run_once(self) -> SlotCheckResult:
        return await self._run_singleflight()

    async def check_now(self) -> SlotCheckResult:
        return await self._run_singleflight()

    async def run(self, stop: asyncio.Event) -> None:
        try:
            while not stop.is_set():
                await self._run_singleflight()
                delay = self._settings.check_interval_seconds + random.uniform(0, 12)
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
        )

    async def _run_singleflight(self) -> SlotCheckResult:
        async with self._singleflight_guard:
            if self._stopping:
                raise RuntimeError("slot monitor is shutting down")
            task = self._inflight_check
            if task is None or task.done():
                task = asyncio.create_task(
                    self._execute_cycle(),
                    name=f"slot-check:{self.city_key}",
                )
                task.add_done_callback(self._observe_cycle_completion)
                self._inflight_check = task
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

    async def _execute_cycle(self) -> SlotCheckResult:
        try:
            result = await self._scraper.check_availability()
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
            else:
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

        self._last_result = result
        logger.info(
            "slot_check",
            extra={
                "city": self._settings.city_name,
                "status": result.status.value,
                "error": result.error,
                "failure_code": (
                    result.failure_code.value
                    if result.failure_code is not None
                    else None
                ),
            },
        )
        await self._notifier.drain_outbox()
        return result

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
