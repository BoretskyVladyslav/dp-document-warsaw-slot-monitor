from __future__ import annotations

from datetime import timedelta

from src.core.models import MonitorSnapshot, SlotCheckResult, SlotStatus


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
        "/subscribe — підписатися на сповіщення\n"
        "/stop — відписатися\n"
        "/unsubscribe — відписатися\n"
        "/help — ця довідка\n"
        "/status — стан моніторингу"
    )


def format_status(snapshot: MonitorSnapshot, *, is_admin: bool) -> str:
    last_verified = (
        snapshot.last_verified_at.isoformat(sep=" ", timespec="seconds")
        if snapshot.last_verified_at
        else "ще не було"
    )
    state = _status_label(snapshot.slot_state)
    public_text = (
        f"Місто: {snapshot.city_name}\n"
        f"Стан слотів: {state}\n"
        f"Остання підтверджена перевірка: {last_verified}\n"
        f"Запис: {snapshot.target_url}"
    )
    if not is_admin:
        return public_text

    health = snapshot.scraper_health
    last_attempt = (
        snapshot.last_attempt_at.isoformat(sep=" ", timespec="seconds")
        if snapshot.last_attempt_at
        else "ще не було"
    )
    health_status = health.status.value if health is not None else "UNKNOWN"
    cdp_connected = "так" if health is not None and health.cdp_connected else "ні"
    target_tab = "так" if health is not None and health.target_tab_present else "ні"
    health_reason = (
        health.failure_code.value
        if health is not None and health.failure_code is not None
        else "—"
    )
    last_error = snapshot.last_error or "—"
    details = snapshot.last_details or "—"
    cooldown_until = (
        snapshot.cooldown_until.isoformat(sep=" ", timespec="seconds")
        if snapshot.cooldown_until is not None
        else "—"
    )
    uptime = str(timedelta(seconds=int(snapshot.uptime_seconds)))
    return (
        f"{public_text}\n\n"
        f"Остання спроба: {last_attempt}\n"
        f"Стан scraper: {health_status}\n"
        f"CDP підключено: {cdp_connected}\n"
        f"Цільова вкладка: {target_tab}\n"
        f"Причина деградації: {health_reason}\n"
        f"Cooldown до: {cooldown_until}\n"
        f"Остання помилка: {last_error}\n"
        f"Деталі: {details}\n"
        f"Активних підписників: {snapshot.active_subscribers}\n"
        f"Аптайм: {uptime}"
    )


def format_check_result(result: SlotCheckResult) -> str:
    checked_at = result.checked_at.isoformat(sep=" ", timespec="seconds")
    details = result.details or "—"
    error = f"\nПомилка: {result.error}" if result.error else ""
    return (
        "Ручну перевірку завершено.\n"
        f"Стан: {_status_label(result.status)}\n"
        f"Час: {checked_at}\n"
        f"Деталі: {details}"
        f"{error}"
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
