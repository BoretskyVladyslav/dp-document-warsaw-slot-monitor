from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class SlotStatus(StrEnum):
    FREE_SLOTS_AVAILABLE = "FREE_SLOTS_AVAILABLE"
    NO_SLOTS = "NO_SLOTS"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class Subscriber:
    user_id: int
    chat_id: int
    username: str | None
    is_active: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SlotCheckResult:
    status: SlotStatus
    checked_at: datetime
    details: str = ""
    slots: tuple[str, ...] = ()
    error: str | None = None


@dataclass(frozen=True, slots=True)
class MonitorSnapshot:
    last_check_at: datetime | None
    slot_state: SlotStatus
    last_details: str
    last_error: str | None
    active_subscribers: int
    uptime_seconds: float
    city_name: str
    target_url: str
    slots: tuple[str, ...] = field(default_factory=tuple)
