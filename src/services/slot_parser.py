from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from typing import Any

from src.core.models import SlotCheckResult, SlotStatus

NO_SLOT_PHRASES: tuple[str, ...] = (
    "немає вільних",
    "немає доступних",
    "немає вільних дат",
    "немає вільних талонів",
    "відсутні вільні",
    "брак вільних",
    "нет свободных",
    "no available slots",
    "no slots available",
    "currently no available",
    "brak wolnych",
    "brak dostępnych",
)

CLOUDFLARE_MARKERS: tuple[str, ...] = (
    "cf-browser-verification",
    "challenge-platform",
    "just a moment",
    "checking your browser",
    "attention required",
    "cf-challenge",
    "cdn-cgi/challenge",
)

_DATE_RE = re.compile(
    r"\b(\d{4}-\d{2}-\d{2}|\d{2}\.\d{2}\.\d{4}|\d{2}/\d{2}/\d{4})\b"
)
_TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):[0-5]\d\b")


def is_cloudflare_challenge(*, title: str, html: str) -> bool:
    blob = f"{title} {html}".lower()
    return any(marker in blob for marker in CLOUDFLARE_MARKERS)


def parse_slot_page(
    *,
    html: str,
    title: str,
    json_payloads: list[Any],
    checked_at: datetime | None = None,
) -> SlotCheckResult:
    moment = checked_at or datetime.now(timezone.utc)
    if is_cloudflare_challenge(title=title, html=html):
        return SlotCheckResult(
            status=SlotStatus.UNKNOWN,
            checked_at=moment,
            error="cloudflare_challenge",
            details="Cloudflare challenge page detected",
        )

    json_slots, json_explicit_empty = _extract_slots_from_payloads(json_payloads)
    dom_slots = _extract_slots_from_html(html)
    slots = tuple(dict.fromkeys([*json_slots, *dom_slots]))

    if slots:
        preview = ", ".join(slots[:12])
        return SlotCheckResult(
            status=SlotStatus.FREE_SLOTS_AVAILABLE,
            checked_at=moment,
            details=preview,
            slots=slots,
        )

    lowered = html.lower()
    if json_explicit_empty or any(phrase in lowered for phrase in NO_SLOT_PHRASES):
        return SlotCheckResult(
            status=SlotStatus.NO_SLOTS,
            checked_at=moment,
            details="no available dates or times",
        )

    return SlotCheckResult(
        status=SlotStatus.UNKNOWN,
        checked_at=moment,
        details="page did not contain a conclusive slot signal",
    )


def _extract_slots_from_payloads(payloads: list[Any]) -> tuple[list[str], bool]:
    found: list[str] = []
    explicit_empty = False
    for payload in payloads:
        items, empty = _walk_json(payload)
        found.extend(items)
        explicit_empty = explicit_empty or empty
    return found, explicit_empty and not found


def _walk_json(node: Any) -> tuple[list[str], bool]:
    found: list[str] = []
    explicit_empty = False

    def visit(value: Any) -> None:
        nonlocal explicit_empty
        if isinstance(value, dict):
            slot = _slot_from_dict(value)
            if slot:
                found.append(slot)
            for key, child in value.items():
                key_l = str(key).lower()
                if key_l in {"slots", "dates", "times", "tickets", "availabledates"}:
                    if isinstance(child, list) and len(child) == 0:
                        explicit_empty = True
                visit(child)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(node)
    return found, explicit_empty


def _slot_from_dict(item: dict[str, Any]) -> str | None:
    available = item.get("available", item.get("isAvailable", item.get("free")))
    if available is False or str(available).strip().lower() in {"0", "false", "no"}:
        return None
    if isinstance(available, (int, float)) and int(available) <= 0:
        return None

    date_raw = item.get("date") or item.get("day") or item.get("datetime")
    time_raw = item.get("time") or item.get("hour")
    if date_raw is None and time_raw is None:
        return None
    if not _is_future_or_today(str(date_raw) if date_raw is not None else None):
        return None
    parts = [str(date_raw)] if date_raw is not None else []
    if time_raw is not None:
        parts.append(str(time_raw))
    return " ".join(parts)


def _is_future_or_today(raw: str | None) -> bool:
    if raw is None:
        return True
    parsed = _parse_date(raw)
    if parsed is None:
        return True
    return parsed >= date.today()


def _parse_date(raw: str) -> date | None:
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw[:10], fmt).date()
        except ValueError:
            continue
    return None


def _extract_slots_from_html(html: str) -> list[str]:
    slots: list[str] = []
    attr_pattern = re.compile(
        r"""(?:data-date|data-day|data-time|data-slot)\s*=\s*["']([^"']+)["']""",
        re.IGNORECASE,
    )
    for match in attr_pattern.finditer(html):
        value = match.group(1).strip()
        if _looks_like_slot(value):
            slots.append(value)

    class_available = re.compile(
        r"""<(?:button|td|div|span)[^>]*class=["'][^"']*\b(?:available|free|enabled)\b[^"']*["'][^>]*>(.*?)</(?:button|td|div|span)>""",
        re.IGNORECASE | re.DOTALL,
    )
    for match in class_available.finditer(html):
        text = re.sub(r"<[^>]+>", " ", match.group(1))
        extracted = _dates_and_times_from_text(text)
        slots.extend(extracted)

    return [item for item in slots if _is_future_or_today(item.split()[0] if item else None)]


def _looks_like_slot(value: str) -> bool:
    return bool(_DATE_RE.search(value) or _TIME_RE.search(value))


def _dates_and_times_from_text(text: str) -> list[str]:
    dates = _DATE_RE.findall(text)
    times = _TIME_RE.findall(text)
    if dates and times:
        return [f"{day} {time}" for day in dates for time in times]
    return [*dates, *times]


def dumps_payload(raw: str) -> Any | None:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None
