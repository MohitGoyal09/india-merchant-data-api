"""Runtime settings, read from environment variables prefixed with ``IMDA_``."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_USER_AGENT = (
    "india-merchant-data-api/0.1 "
    "(+https://github.com/MohitGoyal09/india-merchant-data-api; research demo)"
)

MIN_ADMIN_TOKEN_LENGTH = 32


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
    max_response_bytes: int = Field(default=25 * 1024 * 1024, ge=1024)

    # Storage
    db_path: Path = Path("data/imda.sqlite3")

    # API
    admin_token: SecretStr | None = None
    mcp_token: SecretStr | None = None
    max_page_size: int = Field(default=1000, ge=1, le=10_000)
    max_request_body_bytes: int = Field(default=64 * 1024, ge=1024)
    enable_docs: bool = True
    refresh_cooldown_seconds: float = Field(default=300.0, ge=0.0)

    # Webhooks
    allow_private_webhooks: bool = False
    webhook_timeout_seconds: float = Field(default=10.0, gt=0.0)
    webhook_max_attempts: int = Field(default=3, ge=1, le=10)

    # Domain rules
    settlement_cycle_days: int = Field(default=2, ge=0, le=30)
    closing_of_accounts_is_holiday: bool = True
    fx_publish_cutoff_ist: str = "13:30"
    fx_asof_max_lookback_days: int = Field(default=10, ge=1, le=60)

    @field_validator("admin_token")
    @classmethod
    def _admin_token_strength(cls, value: SecretStr | None) -> SecretStr | None:
        """Unset or blank disables admin routes; a set token needs 32+ characters once trimmed."""
        return _checked_token("admin_token", value)

    @field_validator("mcp_token")
    @classmethod
    def _mcp_token_strength(cls, value: SecretStr | None) -> SecretStr | None:
        """Unset or blank disables the MCP HTTP transport; a set token needs 32+ characters."""
        return _checked_token("mcp_token", value)


def _checked_token(name: str, value: SecretStr | None) -> SecretStr | None:
    """Surrounding whitespace (a stray newline from a secret file) is not part of the token."""
    if value is None:
        return None
    token = value.get_secret_value().strip()
    if not token:
        return None
    if len(token) < MIN_ADMIN_TOKEN_LENGTH:
        raise ValueError(f"{name} must be at least {MIN_ADMIN_TOKEN_LENGTH} characters")
    return SecretStr(token)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
