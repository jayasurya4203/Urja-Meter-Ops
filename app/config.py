"""Application configuration, loaded from environment or ``.env``."""

from __future__ import annotations

from functools import lru_cache
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "Flock Energy — Urja Meter Ops API"
    app_version: str = "1.0.0"

    # --- Legacy portal -----------------------------------------------------
    portal_base_url: str = "https://urja-ops.flockenergy.tech"
    portal_email: str = ""
    portal_password: str = ""
    portal_timezone: str = "Asia/Kolkata"
    portal_request_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    portal_connect_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    portal_max_retries: int = Field(default=2, ge=0, le=5)
    session_refresh_skew_seconds: int = Field(default=90, ge=0, le=1800)

    # --- Caches ------------------------------------------------------------
    dataset_cache_ttl_seconds: int = Field(default=300, ge=1)
    detail_cache_ttl_seconds: int = Field(default=120, ge=1)
    energy_cache_ttl_seconds: int = Field(default=120, ge=1)
    cache_max_entries: int = Field(default=2048, ge=16)

    # --- API ---------------------------------------------------------------
    api_key: str | None = None
    max_consumption_range_days: int = Field(default=92, ge=1, le=366)
    default_page_size: int = Field(default=25, ge=1, le=200)
    max_page_size: int = Field(default=200, ge=1, le=1000)
    cors_origins: str = ""
    log_level: str = "INFO"

    @field_validator("portal_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("api_key", mode="before")
    @classmethod
    def _empty_key_is_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @property
    def portal_origin(self) -> str:
        parts = urlsplit(self.portal_base_url)
        return f"{parts.scheme}://{parts.netloc}"

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.portal_timezone)

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def credentials_configured(self) -> bool:
        return bool(self.portal_email and self.portal_password)


@lru_cache
def get_settings() -> Settings:
    return Settings()
