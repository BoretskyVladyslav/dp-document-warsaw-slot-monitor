from __future__ import annotations

from src.core.models import MonitorSnapshot, SlotCheckResult
from src.services.monitor import SlotMonitor


class StatusService:
    def __init__(self, monitor: SlotMonitor) -> None:
        self._monitor = monitor

    async def get_snapshot(self) -> MonitorSnapshot:
        return await self._monitor.snapshot()

    async def check_now(self, admin_id: int) -> SlotCheckResult:
        return await self._monitor.check_now(admin_id=admin_id)
