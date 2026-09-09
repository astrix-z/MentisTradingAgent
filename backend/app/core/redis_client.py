"""Async Redis client shared by the trading control/status endpoints.

The backend and the agent coordinate exclusively over Redis (the "live
plane"): the backend writes the desired trading state and publishes change
notifications, and the agent reports liveness back via a heartbeat key. No
service reads or writes another service's database.
"""

from __future__ import annotations

import logging

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from app.core.config import settings

logger = logging.getLogger(__name__)

_client: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    """Return the shared async Redis client, creating it lazily on first use."""
    global _client
    if _client is None:
        _client = aioredis.from_url(  # type: ignore[no-untyped-call]
            str(settings.REDIS_URL), encoding="utf-8", decode_responses=True
        )
    return _client


async def close_redis() -> None:
    """Close the shared Redis client. Safe to call even if never opened."""
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        except RedisError:  # pragma: no cover - best effort on shutdown
            logger.warning("failed to close Redis client cleanly")
        finally:
            _client = None
