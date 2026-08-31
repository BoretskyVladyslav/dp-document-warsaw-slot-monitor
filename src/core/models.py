from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class SlotStatus(StrEnum):
    FREE_SLOTS_AVAILABLE = "FREE_SLOTS_AVAILABLE"
    NO_SLOTS = "NO_SLOTS"
    UNKNOWN = "UNKNOWN"


class ScraperFailureCode(StrEnum):
    CDP_UNAVAILABLE = "cdp_unavailable"
    TARGET_TAB_MISSING = "target_tab_missing"
    TARGET_TAB_CLOSED = "target_tab_closed"
    CLOUDFLARE_CHALLENGE = "cloudflare_challenge"
    CLOUDFLARE_DELAYED = "cloudflare_delayed"
    NAVIGATION_TIMEOUT = "navigation_timeout"
    INCONCLUSIVE_PAGE = "inconclusive_page"
    SERVICE_VALIDATE_ERROR = "service_validate_error"
    SERVER_ERROR = "server_error"
    RATE_LIMITED = "rate_limited"
    SCRAPER_ERROR = "scraper_error"


class ScraperHealthStatus(StrEnum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    READY = "READY"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    DEGRADED = "DEGRADED"


class NotificationEventType(StrEnum):
    SLOTS_AVAILABLE = "SLOTS_AVAILABLE"
    HUMAN_ACTION_REQUIRED = "HUMAN_ACTION_REQUIRED"


class DeliveryStatus(StrEnum):
    PENDING = "PENDING"
    DELIVERED = "DELIVERED"
    UNREACHABLE = "UNREACHABLE"


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
    failure_code: ScraperFailureCode | None = None


@dataclass(frozen=True, slots=True)
class ScraperHealthSnapshot:
    status: ScraperHealthStatus
    cdp_connected: bool
    target_tab_present: bool
    updated_at: datetime
    failure_code: ScraperFailureCode | None = None
    details: str = ""


@dataclass(frozen=True, slots=True)
class MonitorStateRecord:
    city_key: str
    verified_state: SlotStatus
    last_verified_at: datetime | None
    last_attempt_at: datetime | None
    last_details: str
    last_error: str | None
    human_action_incident_key: str | None
    human_action_incident_notified_at: datetime | None
    cooldown_until: datetime | None


@dataclass(frozen=True, slots=True)
class NotificationRecipient:
    chat_id: int
    user_id: int | None = None


@dataclass(frozen=True, slots=True)
class PendingNotificationDelivery:
    event_id: int
    event_key: str
    event_type: NotificationEventType
    city_key: str
    chat_id: int
    user_id: int | None
    text: str
    attempts: int
    created_at: datetime


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
    last_attempt_at: datetime | None = None
    last_verified_at: datetime | None = None
    scraper_health: ScraperHealthSnapshot | None = None
    cooldown_until: datetime | None = None
