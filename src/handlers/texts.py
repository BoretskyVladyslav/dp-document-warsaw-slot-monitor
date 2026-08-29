from __future__ import annotations

from datetime import timedelta

from src.core.models import MonitorSnapshot, SlotStatus


def format_start_confirmation(city_name: str) -> str:
    return (
        f"Ви підписані на сповіщення про вільні слоти ДП «Документ» ({city_name}).\n\n"
        "/stop — відписатися\n"
        "/help — довідка"
    )


def format_stop_confirmation() -> str:
    return "Підписку вимкнено. Надішліть /start, щоб увімкнути знову."


def format_help(city_name: str) -> str:
    return (
        f"Бот стежить за електронною чергою ДП «Документ» ({city_name}) "
        "і пише лише коли стан слотів змінюється.\n\n"
        "/start — підписатися на сповіщення\n"
        "/stop — відписатися\n"
        "/help — ця довідка\n"
        "/status — стан моніторингу (лише для адміністратора)"
    )


def format_status(snapshot: MonitorSnapshot) -> str:
    last_check = (
        snapshot.last_check_at.isoformat(sep=" ", timespec="seconds")
        if snapshot.last_check_at
        else "ще не було"
    )
    state = _status_label(snapshot.slot_state)
    uptime = str(timedelta(seconds=int(snapshot.uptime_seconds)))
    error_line = f"\nОстання помилка: {snapshot.last_error}" if snapshot.last_error else ""
    details = snapshot.last_details or "—"
    return (
        f"Місто: {snapshot.city_name}\n"
        f"Остання перевірка: {last_check}\n"
        f"Стан слотів: {state}\n"
        f"Деталі: {details}\n"
        f"Активних підписників: {snapshot.active_subscribers}\n"
        f"Аптайм: {uptime}\n"
        f"URL: {snapshot.target_url}"
        f"{error_line}"
    )


def format_admin_only() -> str:
    return "Команда доступна лише адміністратору."


def _status_label(status: SlotStatus) -> str:
    labels = {
        SlotStatus.FREE_SLOTS_AVAILABLE: "є вільні слоти",
        SlotStatus.NO_SLOTS: "немає слотів",
        SlotStatus.UNKNOWN: "невідомо",
    }
    return labels[status]
