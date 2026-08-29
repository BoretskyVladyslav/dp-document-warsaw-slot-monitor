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
    check_interval_seconds: int = Field(default=300, ge=15, le=3600)
    target_url: HttpUrl
    city_name: str = "Warsaw"
    database_path: str = "data/monitor.db"
    cdp_url: str | None = None
    check_once: bool = False

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

    @field_validator("cdp_url", mode="before")
    @classmethod
    def empty_str_to_none(cls, value: object) -> object:
        if value == "":
            return None
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
