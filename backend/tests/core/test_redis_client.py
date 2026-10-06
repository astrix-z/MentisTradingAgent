"""The shared Redis clients and the event loops that own them (ClickUp 869fc707e).

A TestClient inside another ran a second app lifespan in a second event loop;
both got the one async client, and the inner shutdown closed the outer loop's
connections from its own loop: ``RuntimeError: ... attached to a different
loop`` (only with a reachable Redis — without one no connection is opened).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import redis
from fastapi.testclient import TestClient

from app.core import redis_client
from app.core.config import settings
from app.main import app


def test_each_loop_has_its_own_client_and_closes_only_its_own() -> None:
    loops = [asyncio.new_event_loop() for _ in range(2)]
    try:
        first, second = (loop.run_until_complete(_get()) for loop in loops)
        assert first is not second
        assert loops[0].run_until_complete(_get()) is first  # one per loop
        sync = redis_client.get_sync_redis()

        loops[1].run_until_complete(redis_client.close_redis())
        assert loops[0].run_until_complete(_get()) is first  # untouched
        assert redis_client.get_sync_redis() is sync  # the first app still runs

        loops[0].run_until_complete(redis_client.close_redis())
        assert redis_client.get_sync_redis() is not sync  # closed with the last
        assert loops[0].run_until_complete(_get()) is not first
        loops[0].run_until_complete(redis_client.close_redis())
    finally:
        for loop in loops:
            loop.close()


async def _get() -> Any:
    return redis_client.get_redis()


def _redis_reachable() -> bool:
    try:
        return bool(
            redis.Redis.from_url(str(settings.REDIS_URL), socket_timeout=1).ping()
        )
    except redis.RedisError:
        return False


def test_an_app_inside_another_shuts_down_cleanly_with_a_real_redis() -> None:
    if not _redis_reachable():
        pytest.skip(f"no Redis reachable at {settings.REDIS_URL}")
    with TestClient(app) as outer:
        with TestClient(app) as inner:
            assert (
                inner.get(f"{settings.API_V1_STR}/utils/health-check/").status_code
                == 200
            )
        # The outer app's client and its listener's connection are intact.
        assert outer.portal is not None
        assert outer.portal.call(_ping) is True


async def _ping() -> bool:
    return bool(await redis_client.get_redis().ping())
