"""Async SQLAlchemy engines for the agent's databases.

Two distinct engines on purpose:

* settings engine -> the agent's own settings DB (dedicated database on the
  project's Postgres). The agent owns these tables/migrations.
* external engine  -> a separate, external DB that the agent only *reads*
  (market/history data). Enforce read-only with a low-privilege DB role;
  never point this at the same URL as the settings engine.

Engines are created lazily so the daemon can boot (and report clearly) even
when a database is not yet configured.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from agent.core.config import settings

logger = logging.getLogger(__name__)

_settings_engine: AsyncEngine | None = None
_external_engine: AsyncEngine | None = None

# Make sure all async SQLAlchemy models are imported before using metadata
# (mirrors the backend's `app.models` requirement).


def _build_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, pool_pre_ping=True)


def settings_engine() -> AsyncEngine | None:
    """Return the engine for the agent's own settings DB, if configured."""
    global _settings_engine
    if settings.AGENT_DB_URL is None:
        return None
    if _settings_engine is None:
        _settings_engine = _build_engine(str(settings.AGENT_DB_URL))
    return _settings_engine


def external_engine() -> AsyncEngine | None:
    """Return the engine for the Binance read-only DB, if configured."""
    global _external_engine
    if settings.BINANCE_DB_URL is None:
        return None
    if _external_engine is None:
        _external_engine = _build_engine(str(settings.BINANCE_DB_URL))
    return _external_engine


async def settings_session() -> AsyncIterator[AsyncSession]:
    """Yield a session bound to the agent's own settings DB."""
    engine = settings_engine()
    if engine is None:
        raise RuntimeError("AGENT_DB_URL is not configured for the agent.")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session


async def dispose_engines() -> None:
    """Dispose all engines (call on shutdown)."""
    global _settings_engine, _external_engine
    for engine in (_settings_engine, _external_engine):
        if engine is not None:
            await engine.dispose()
    _settings_engine = None
    _external_engine = None
