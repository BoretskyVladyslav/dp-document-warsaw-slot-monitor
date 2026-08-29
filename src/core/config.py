from functools import lru_cache

from pydantic import Field, HttpUrl, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    bot_token: str = Field(min_length=10)
    admin_ids: list[int] = Field(default_factory=list)
    check_interval_seconds: int = Field(default=180, ge=15, le=3600)
    target_url: HttpUrl
    city_name: str = "Warsaw"
    proxy_url: str | None = None
    headless: bool = True
    database_path: str = "data/monitor.db"
    service_option: str | None = None
    playwright_timeout_ms: int = Field(default=60_000, ge=5_000, le=180_000)
    browser_profile_dir: str = "data/browser_profile"
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    )

    @field_validator("admin_ids", mode="before")
    @classmethod
    def parse_admin_ids(cls, value: object) -> object:
        if value is None or value == "":
            return []
        if isinstance(value, str):
            return [int(part.strip()) for part in value.split(",") if part.strip()]
        if isinstance(value, int):
            return [value]
        return value

    @field_validator("proxy_url", "service_option", mode="before")
    @classmethod
    def empty_str_to_none(cls, value: object) -> object:
        if value == "":
            return None
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
