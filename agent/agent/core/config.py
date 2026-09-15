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

    AGENT_NAME: str = "MentisAgent"
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    ENVIRONMENT: Literal["development", "production"] = "development"

    # Control/status plane between backend and agent (commands + liveness).
    REDIS_URL: str = "redis://localhost:6379/0"

    # Server-side deadline for a command's network I/O (e.g. exchange calls
    # during start/balance), so a stuck call reports a clear timeout instead
    # of hanging indefinitely.
    COMMAND_IO_TIMEOUT_SECONDS: float = 20.0

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

    @field_validator("AGENT_DB_URL", "BINANCE_DB_URL", mode="after")
    @classmethod
    def _use_psycopg_driver(
        cls, value: PostgresDsn | None
    ) -> PostgresDsn | None:
        """Point Postgres URLs at psycopg's async driver.

        ``create_async_engine`` refuses a plain ``postgresql://`` URL, and the
        compose file (like the backend's) spells it that way.
        """
        if value is None:
            return None
        url = str(value)
        for scheme in ("postgres://", "postgresql://"):
            if url.startswith(scheme):
                return PostgresDsn(url.replace(scheme, "postgresql+psycopg://", 1))
        return value


settings = Settings()
