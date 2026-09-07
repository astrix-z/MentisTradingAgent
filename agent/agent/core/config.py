from typing import Literal

from pydantic import PostgresDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _normalize_db_url(value: str | PostgresDsn | None) -> str | None:
    """Return the value as-is, or None for empty/unset database URLs."""
    if value is None:
        return None
    url = str(value)
    if not url:
        return None
    return url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Use the top-level .env file (one level above ./agent/)
        env_file="../.env",
        env_ignore_empty=True,
        extra="ignore",
    )

    AGENT_NAME: str = "agent"
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    ENVIRONMENT: Literal["development", "production"] = "development"

    # Own settings DB: a dedicated database on the project's Postgres.
    # Optional at boot so the daemon can start and report a warning if missing.
    AGENT_DB_URL: PostgresDsn | None = None

    # Binance read-only market/history DB. Point this at a low-privilege,
    # SELECT-only role; never reuse AGENT_DB_URL for it.
    BINANCE_DB_URL: PostgresDsn | None = None

    # Binance API credentials (injected from the environment / secrets).
    BINANCE_API_KEY: str | None = None
    BINANCE_API_SECRET: str | None = None

    # Binance testnet (sandbox) API credentials.
    BINANCE_TESTNET_API_KEY: str | None = None
    BINANCE_TESTNET_API_SECRET: str | None = None

    @field_validator("AGENT_DB_URL", "BINANCE_DB_URL", mode="before")
    @classmethod
    def _empty_is_none(cls, value: str | PostgresDsn | None) -> str | None:
        return _normalize_db_url(value)


settings = Settings()  # type: ignore # ty: ignore[unused-ignore-comment]
