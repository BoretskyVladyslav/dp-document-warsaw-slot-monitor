from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone

from src.core.config import Settings
from src.core.models import MonitorSnapshot, SlotCheckResult, SlotStatus
from src.database.connection import Database
from src.database.monitor_state import get_slot_state, upsert_slot_state
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
        self._cached_state: SlotStatus = SlotStatus.NO_SLOTS

    @property
    def city_key(self) -> str:
        return self._settings.city_name.strip().lower()

    async def restore_state(self) -> None:
        state, _, _, _ = await get_slot_state(self._database.connection, self.city_key)
        self._notifier.prime_previous(state)
        if state is not None:
            self._cached_state = state

    async def run_once(self) -> SlotCheckResult:
        await self._tick()
        assert self._last_result is not None
        return self._last_result

    async def run(self, stop: asyncio.Event) -> None:
        try:
            while not stop.is_set():
                await self._tick()
                if self._settings.check_once:
                    logger.info("check_once_complete")
                    stop.set()
                    return
                delay = self._settings.check_interval_seconds + random.uniform(0, 12)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                except TimeoutError:
                    continue
        finally:
            await self._scraper.stop()

    async def snapshot(self) -> MonitorSnapshot:
        active = await count_active_subscribers(self._database.connection)
        last = self._last_result
        persisted, last_check_at, last_details, last_error = await get_slot_state(
            self._database.connection, self.city_key
        )
        uptime = (datetime.now(timezone.utc) - self._started_at).total_seconds()
        return MonitorSnapshot(
            last_check_at=last.checked_at if last else last_check_at,
            slot_state=(
                last.status
                if last is not None and last.status is not SlotStatus.UNKNOWN
                else (persisted or self._cached_state)
            ),
            last_details=(last.details if last else last_details) or "",
            last_error=last.error if last else last_error,
            active_subscribers=active,
            uptime_seconds=uptime,
            city_name=self._settings.city_name,
            target_url=str(self._settings.target_url),
            slots=last.slots if last else (),
        )

    async def _tick(self) -> None:
        result = await self._scraper.check_availability()
        self._last_result = result
        logger.info(
            "slot_check",
            extra={
                "city": self._settings.city_name,
                "status": result.status.value,
                "error": result.error,
                "slot_count": len(result.slots),
            },
        )
        if result.error == "session_expired":
            await self._notifier.notify_session_expired()
        state_to_store = result.status
        if result.status is SlotStatus.UNKNOWN:
            state_to_store = self._cached_state
        else:
            await self._notifier.handle_check(result)
            self._cached_state = result.status
        await upsert_slot_state(
            self._database.connection,
            city_key=self.city_key,
            slot_state=state_to_store,
            last_check_at=result.checked_at,
            last_details=result.details,
            last_error=result.error,
        )
