"""Throwaway PostgreSQL databases for tests that need the real thing.

Partial unique indexes, advisory locks and concurrent transactions are the
point of several execution tests, so they run on PostgreSQL rather than a
stand-in. Each test gets a fresh database on the server named by
``AGENT_TEST_DB_URL`` (default: the server of the agent's ``AGENT_DB_URL``) and drops it
afterwards; the agent's real database is never touched. No server → skip.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlsplit, urlunsplit

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agent.core.config import settings


def server_url() -> str:
    url = os.environ.get("AGENT_TEST_DB_URL")
    if url:
        return url
    if settings.AGENT_DB_URL is None:
        pytest.skip("no AGENT_TEST_DB_URL or AGENT_DB_URL for PostgreSQL tests")
    return str(settings.AGENT_DB_URL)


def _with_database(url: str, name: str) -> str:
    return urlunsplit(urlsplit(url)._replace(path=f"/{name}"))


@asynccontextmanager
async def scratch_database() -> AsyncIterator[str]:
    """Yield the URL of a new empty database, dropped on exit."""
    name = f"agent_test_{uuid.uuid4().hex[:12]}"
    admin = create_async_engine(
        _with_database(server_url(), "postgres"), isolation_level="AUTOCOMMIT"
    )
    try:
        async with admin.connect() as connection:
            await connection.execute(text(f'create database "{name}"'))
    except Exception as exc:  # noqa: BLE001 - any connection failure means skip
        await admin.dispose()
        pytest.skip(f"no PostgreSQL reachable for tests: {exc}")
    try:
        yield _with_database(server_url(), name)
    finally:
        async with admin.connect() as connection:
            await connection.execute(
                text(f'drop database if exists "{name}" with (force)')
            )
        await admin.dispose()


@asynccontextmanager
async def scratch_engine() -> AsyncIterator[AsyncEngine]:
    async with scratch_database() as url:
        engine = create_async_engine(url)
        try:
            yield engine
        finally:
            await engine.dispose()
