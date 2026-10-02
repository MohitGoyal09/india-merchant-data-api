"""Runtime settings, read from environment variables prefixed with ``IMDA_``."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_USER_AGENT = (
    "india-merchant-data-api/0.1 "
    "(+https://github.com/MohitGoyal09/india-merchant-data-api; research demo)"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="IMDA_", env_file=".env", extra="ignore")

    # Upstream politeness
    user_agent: str = DEFAULT_USER_AGENT
    upstream_enabled: bool = True
    min_interval_seconds: float = Field(default=2.0, ge=0.0)
    timeout_seconds: float = Field(default=30.0, gt=0.0)
    max_attempts: int = Field(default=4, ge=1, le=10)
    backoff_base_seconds: float = Field(default=1.0, ge=0.0)
    backoff_max_seconds: float = Field(default=30.0, ge=0.0)
    breaker_failure_threshold: int = Field(default=5, ge=1)
    breaker_cooldown_seconds: float = Field(default=600.0, ge=0.0)
    request_budget: int = Field(default=500, ge=1)

    # Storage
    db_path: Path = Path("data/imda.sqlite3")

    # API
    admin_token: SecretStr | None = None
    max_page_size: int = Field(default=1000, ge=1, le=10_000)

    # Webhooks
    allow_private_webhooks: bool = False
    webhook_timeout_seconds: float = Field(default=10.0, gt=0.0)
    webhook_max_attempts: int = Field(default=3, ge=1, le=10)

    # Domain rules
    settlement_cycle_days: int = Field(default=2, ge=0, le=30)
    closing_of_accounts_is_holiday: bool = True
    fx_publish_cutoff_ist: str = "13:30"
    fx_asof_max_lookback_days: int = Field(default=10, ge=1, le=60)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
