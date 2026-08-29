class MonitorError(Exception):
    """Base error for the monitoring stack."""


class ScraperError(MonitorError):
    """Failed to load or parse the appointment page."""


class CloudflareChallengeError(ScraperError):
    """Cloudflare or WAF interstitial detected; slots were not parsed."""


class NetworkTimeoutError(ScraperError):
    """Navigation or network idle wait timed out."""


class RecipientUnreachableError(MonitorError):
    """Telegram delivery failed because the user blocked the bot or the chat is gone."""

    def __init__(self, chat_id: int) -> None:
        super().__init__(f"recipient unreachable: {chat_id}")
        self.chat_id = chat_id


class DeliveryError(MonitorError):
    """Transient Telegram or network failure while sending a notification."""

    def __init__(self, chat_id: int, reason: str) -> None:
        super().__init__(f"delivery failed for {chat_id}: {reason}")
        self.chat_id = chat_id
        self.reason = reason
