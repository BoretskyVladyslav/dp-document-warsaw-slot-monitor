from src.core.models import ScraperFailureCode


class MonitorError(Exception):
    """Base error for the monitoring stack."""


class ScraperError(MonitorError):
    """Failed to load or parse the appointment page."""


class HumanActionRequiredError(ScraperError):
    """The attached browser requires operator intervention."""

    def __init__(self, message: str, failure_code: ScraperFailureCode) -> None:
        super().__init__(message)
        self.failure_code = failure_code


class SessionExpiredException(HumanActionRequiredError):
    """The attached browser session no longer clears the Cloudflare challenge."""

    def __init__(
        self,
        message: str,
        failure_code: ScraperFailureCode = ScraperFailureCode.CLOUDFLARE_CHALLENGE,
    ) -> None:
        super().__init__(message, failure_code)


class CloudflareChallengeError(SessionExpiredException):
    """Cloudflare or WAF interstitial detected; slots were not parsed."""


class CdpUnavailableError(HumanActionRequiredError):
    """The configured Chrome DevTools Protocol endpoint is unavailable."""

    def __init__(self, message: str) -> None:
        super().__init__(message, ScraperFailureCode.CDP_UNAVAILABLE)


class TargetTabMissingError(HumanActionRequiredError):
    """The attached browser has no exact target queue tab."""

    def __init__(self, message: str) -> None:
        super().__init__(message, ScraperFailureCode.TARGET_TAB_MISSING)


class TargetTabClosedError(HumanActionRequiredError):
    """The target queue tab closed during a check."""

    def __init__(self, message: str) -> None:
        super().__init__(message, ScraperFailureCode.TARGET_TAB_CLOSED)


class RateLimitException(ScraperError):
    """The target requested a temporary polling cooldown."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.failure_code = ScraperFailureCode.RATE_LIMITED


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
