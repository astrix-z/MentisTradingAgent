"""Async Redis client shared by the trading control/status endpoints.

The backend and the agent coordinate exclusively over Redis (the "live
plane"): the backend writes the desired trading state and publishes change
notifications, and the agent reports liveness back via a heartbeat key. No
service reads or writes another service's database.
"""

from __future__ import annotations

import logging

import redis
import redis.asyncio as aioredis
from redis.exceptions import RedisError

from app.core.config import settings

logger = logging.getLogger(__name__)

_client: aioredis.Redis | None = None
_sync_client: redis.Redis | None = None

#: Every call of the blocking client gives up after this long. It is used
#: where a read happens under a database lock (the built-in preset sync,
#: plan §4.3.2): a hung Redis must not hold the lock.
SYNC_REDIS_TIMEOUT_S = 2.0


def get_redis() -> aioredis.Redis:
    """Return the shared async Redis client, creating it lazily on first use."""
    global _client
    if _client is None:
        _client = aioredis.from_url(  # type: ignore[no-untyped-call]
            str(settings.REDIS_URL), encoding="utf-8", decode_responses=True
        )
    return _client


def get_sync_redis() -> redis.Redis:
    """Return the shared blocking Redis client, for code run in a worker thread.

    A sync route (FastAPI runs it in its threadpool) must not await the
    event loop's client; this one blocks only its own thread. Connecting and
    every read are bounded by ``SYNC_REDIS_TIMEOUT_S``.
    """
    global _sync_client
    if _sync_client is None:
        _sync_client = redis.Redis.from_url(
            str(settings.REDIS_URL),
            encoding="utf-8",
            decode_responses=True,
            socket_timeout=SYNC_REDIS_TIMEOUT_S,
            socket_connect_timeout=SYNC_REDIS_TIMEOUT_S,
        )
    return _sync_client


async def close_redis() -> None:
    """Close the shared Redis clients. Safe to call even if never opened."""
    global _client, _sync_client
    if _client is not None:
        try:
            await _client.aclose()
        except RedisError:  # pragma: no cover - best effort on shutdown
            logger.warning("failed to close Redis client cleanly")
        finally:
            _client = None
    if _sync_client is not None:
        try:
            _sync_client.close()
        except RedisError:  # pragma: no cover - best effort on shutdown
            logger.warning("failed to close the blocking Redis client cleanly")
        finally:
            _sync_client = None
