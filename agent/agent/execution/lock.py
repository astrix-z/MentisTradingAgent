"""Single writer per execution context (plan §4.2).

A PostgreSQL **session-level** advisory lock, held on a connection of its own
for as long as this process owns the context. The connection never goes back
to a pool: returning it would hand the lock to whoever checks it out next, and
a pool's recycling would drop it silently.

The key is derived from ``(account, network)`` only, so every process computes
the same number; Python's ``hash()`` is randomized per process and would not.

If the connection or the database goes away, the lock is gone with it: the
server frees session locks of a dead session. :meth:`LeaderLock.check` notices,
:attr:`LeaderLock.held` turns false, and the runtime stops sending new
mutations until it has the lock again **and** has reconciled. Requests already
handed to a worker thread are not cancelled by losing the lock; that is the
accepted residual risk (no fencing on the exchange side).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

logger = logging.getLogger(__name__)


def lock_key(account: str, network: str) -> int:
    """Stable signed 64-bit advisory-lock key for an execution context."""
    digest = hashlib.sha256(f"mentis-execution|{account}|{network}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


class LeaderLock:
    """The advisory lock of one context, on a dedicated connection."""

    def __init__(self, url: str, account: str, network: str) -> None:
        self.account = account
        self.network = network
        self.key = lock_key(account, network)
        # NullPool: the connection is opened for the lock and closed with it.
        self._engine: AsyncEngine = create_async_engine(url, poolclass=NullPool)
        self._connection: AsyncConnection | None = None
        self.held = False
        # The watchdog and a sender may both check; one connection, one query.
        self._checking = asyncio.Lock()

    async def acquire(self) -> bool:
        """Try once; ``True`` if this process now owns the context."""
        if self.held:
            return True
        await self._drop_connection()
        try:
            connection = await self._engine.connect()
            got = await connection.scalar(
                text("select pg_try_advisory_lock(:key)"), {"key": self.key}
            )
            await connection.commit()
        except SQLAlchemyError as exc:
            logger.warning("cannot take the execution lock: %s", exc)
            return False
        if not got:
            await connection.close()
            return False
        self._connection = connection
        self.held = True
        logger.info(
            "execution lock acquired account=%s network=%s", self.account, self.network
        )
        return True

    async def check(self) -> bool:
        """Confirm the lock's session is still alive; drops ``held`` if not."""
        async with self._checking:
            return await self._check()

    async def _check(self) -> bool:
        if not self.held or self._connection is None:
            return False
        try:
            await self._connection.execute(text("select 1"))
            await self._connection.commit()
        except (SQLAlchemyError, DBAPIError, OSError) as exc:
            logger.error(
                "execution lock lost account=%s network=%s: %s",
                self.account,
                self.network,
                exc,
            )
            self.held = False
            await self._drop_connection()
            return False
        return True

    async def backend_pid(self) -> int | None:
        """The server pid of the lock's session (tests use it to cut it)."""
        if self._connection is None:
            return None
        pid = await self._connection.scalar(text("select pg_backend_pid()"))
        await self._connection.commit()
        return int(pid) if pid is not None else None

    async def release(self) -> None:
        if self._connection is not None and self.held:
            try:
                await self._connection.execute(
                    text("select pg_advisory_unlock(:key)"), {"key": self.key}
                )
                await self._connection.commit()
            except SQLAlchemyError:
                logger.debug("advisory unlock failed", exc_info=True)
        self.held = False
        await self._drop_connection()

    async def dispose(self) -> None:
        await self.release()
        await self._engine.dispose()

    async def _drop_connection(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            try:
                await connection.close()
            except SQLAlchemyError, OSError:
                logger.debug("closing a dead lock connection failed", exc_info=True)


__all__ = ["LeaderLock", "lock_key"]
