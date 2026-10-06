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
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from agent.core.config import settings
from agent.core.migrate import MigrationResult, migrate

logger = logging.getLogger(__name__)

_settings_engine: AsyncEngine | None = None
_external_engine: AsyncEngine | None = None

# Make sure all async SQLAlchemy models are imported before using metadata
# (mirrors the backend's `app.models` requirement).

#: A database name goes into DDL, so it is checked rather than trusted.
_SAFE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _build_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, pool_pre_ping=True)


def _with_database(url: str, name: str) -> str:
    """Return ``url`` pointed at another database on the same server."""
    return urlunsplit(urlsplit(url)._replace(path=f"/{name}"))


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


async def ensure_settings_database() -> bool:
    """Create the agent's database if it does not exist yet.

    The compose Postgres creates only the backend's database, so the agent's own
    one has to come from somewhere. It is owned end to end by the agent, and
    this runs on every boot, so it is idempotent by construction rather than by
    a setup step somebody has to remember.

    Returns ``True`` when a database is available, ``False`` when the agent is
    simply not configured for one.
    """
    if settings.AGENT_DB_URL is None:
        logger.info("no AGENT_DB_URL configured; agent settings are not persisted")
        return False
    url = str(settings.AGENT_DB_URL)
    name = urlsplit(url).path.lstrip("/")
    if not _SAFE_NAME.fullmatch(name):
        raise RuntimeError(f"unsafe database name in AGENT_DB_URL: {name!r}")

    admin = create_async_engine(
        _with_database(url, "postgres"), isolation_level="AUTOCOMMIT"
    )
    try:
        async with admin.connect() as connection:
            existing = await connection.scalar(
                text("select 1 from pg_database where datname = :name"), {"name": name}
            )
            if existing is None:
                await connection.execute(text(f'create database "{name}"'))
                logger.info("created the agent settings database %r", name)
    finally:
        await admin.dispose()
    return True


async def init_settings_schema() -> MigrationResult | None:
    """Migrate the agent's database to the current schema.

    Returns ``None`` when no database is configured. Raises on anything that is
    not a clean upgrade (see :mod:`agent.core.migrate`); the caller keeps
    execution closed in that case.
    """
    engine = settings_engine()
    if engine is None:
        return None
    return await migrate(engine)


@asynccontextmanager
async def settings_session() -> AsyncIterator[AsyncSession]:
    """A session bound to the agent's own settings DB.

    Callers that need persistence must tolerate this raising: the settings DB is
    optional at boot so the daemon can still trade when it is missing.
    """
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
