"""Integration tests against a real Redis server (Prompt 5: durable delivery).

Unlike the Mock-based tests in ``test_commands_lifecycle.py``, these exercise
genuine Redis Streams/consumer-group semantics — XREADGROUP's own-pending
replay, BUSYGROUP handling, and the exact pipeline calls
``agent/agent/common/commands.py`` makes — against a real server, not a fake.

Skipped automatically if no Redis is reachable at ``TEST_REDIS_URL`` (default
``redis://localhost:6379/15``, a high DB number so it never collides with a
real dev/prod database on the same server).
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError

from agent.common import commands
from agent.core.config import settings
from agent.engine import REDIS_COMMAND_GROUP, REDIS_COMMAND_STREAM, Engine
from agent.main import _CommandQueue, _ensure_group, _handle_independent

TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")


async def _connect() -> Redis:
    client = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    try:
        await client.ping()
    except RedisError as exc:
        await client.aclose()
        pytest.skip(f"no Redis reachable at {TEST_REDIS_URL}: {exc}")
    await client.flushdb()
    return client


def envelope(kind: str, command_id: str, **payload: Any) -> str:
    return json.dumps(
        {
            "id": command_id,
            "type": kind,
            "payload": payload,
            "issued_at": datetime.now(UTC).isoformat(),
        }
    )


async def enqueue(redis: Redis, raw: str) -> None:
    await redis.xadd(
        REDIS_COMMAND_STREAM, {"data": raw}, maxlen=10_000, approximate=True
    )


def test_restart_redelivers_an_unacked_command_via_own_pending() -> None:
    """A command read but not yet acked (crash before finishing) must come
    back on the next ``xreadgroup`` with id ``"0"`` for the same consumer —
    this is what lets a restarted agent resume it instead of losing it."""

    async def scenario() -> None:
        redis = await _connect()
        try:
            command_id = str(uuid.uuid4())
            await enqueue(redis, envelope("stop", command_id))
            await _ensure_group(redis)

            # First read (as if the agent just started): claims the entry.
            first = await redis.xreadgroup(
                REDIS_COMMAND_GROUP, "primary", {REDIS_COMMAND_STREAM: ">"}, count=10
            )
            assert len(first[0][1]) == 1
            entry_id, fields = first[0][1][0]
            assert json.loads(fields["data"])["id"] == command_id
            # Simulate a crash: no XACK.

            # "Restart": a fresh read with id "0" (own pending) must return
            # the exact same unacked entry, not skip it.
            second = await redis.xreadgroup(
                REDIS_COMMAND_GROUP, "primary", {REDIS_COMMAND_STREAM: "0"}, count=10
            )
            assert len(second[0][1]) == 1
            assert second[0][1][0][0] == entry_id

            await redis.xack(REDIS_COMMAND_STREAM, REDIS_COMMAND_GROUP, entry_id)
            drained = await redis.xreadgroup(
                REDIS_COMMAND_GROUP, "primary", {REDIS_COMMAND_STREAM: "0"}, count=10
            )
            assert drained[0][1] == []
        finally:
            await redis.aclose()

    asyncio.run(scenario())


def test_group_creation_is_idempotent() -> None:
    async def scenario() -> None:
        redis = await _connect()
        try:
            await _ensure_group(redis)
            await _ensure_group(redis)  # BUSYGROUP must not raise
        finally:
            await redis.aclose()

    asyncio.run(scenario())


def test_duplicate_delivery_does_not_rerun_a_completed_command() -> None:
    """End-to-end dedup against real Redis: redelivering a command_id whose
    state hash already says "succeeded" must not invoke the handler again."""

    async def scenario() -> None:
        redis = await _connect()
        try:
            engine = Mock(stop=AsyncMock())
            command_id = str(uuid.uuid4())
            raw = envelope("stop", command_id)

            await commands.dispatch(engine, redis, raw)
            engine.stop.assert_awaited_once()
            state = await redis.hgetall(f"agent:command:{command_id}")
            assert state["status"] == "succeeded"

            await commands.dispatch(engine, redis, raw)  # redelivered
            engine.stop.assert_awaited_once()  # still just the one call
        finally:
            await redis.aclose()

    asyncio.run(scenario())


def test_balance_timeout_reports_timed_out_via_real_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        redis = await _connect()
        try:
            engine = Engine(redis)

            async def hang(*_a: Any, **_kw: Any) -> list[Any]:
                await asyncio.sleep(10)
                return []

            monkeypatch.setattr(
                "agent.engine.ExchangeRest.connect",
                Mock(return_value=Mock(balances=AsyncMock(side_effect=hang))),
            )
            monkeypatch.setattr(settings, "COMMAND_IO_TIMEOUT_SECONDS", 0.05)
            command_id = str(uuid.uuid4())
            await asyncio.wait_for(
                commands.dispatch(
                    engine,
                    redis,
                    envelope("balance", command_id, network="testnet"),
                ),
                2,
            )
            result_raw = await redis.get(f"agent:result:{command_id}")
            result = json.loads(result_raw)
            assert result["ok"] is False
            assert "timed out" in result["message"]
            state = await redis.hgetall(f"agent:command:{command_id}")
            assert state["status"] == "timed_out"
        finally:
            await redis.aclose()

    asyncio.run(scenario())


def test_stop_completes_without_waiting_for_a_slow_balance_via_real_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        redis = await _connect()
        try:
            engine = Engine(redis)
            balance_started = asyncio.Event()
            balance_release = asyncio.Event()

            async def slow_balances() -> list[Any]:
                balance_started.set()
                await balance_release.wait()
                return []

            monkeypatch.setattr(
                "agent.engine.ExchangeRest.connect",
                Mock(return_value=Mock(balances=AsyncMock(side_effect=slow_balances))),
            )
            control = _CommandQueue(redis, engine)
            worker = asyncio.create_task(control.run())
            balance_task = asyncio.create_task(
                _handle_independent(
                    redis,
                    engine,
                    envelope("balance", str(uuid.uuid4()), network="testnet"),
                    "0-1",
                )
            )
            try:
                await asyncio.wait_for(balance_started.wait(), 2)
                stop_id = str(uuid.uuid4())
                control.submit(envelope("stop", stop_id), "0-2")
                await asyncio.wait_for(control.queue.join(), 2)
                result_raw = await redis.get(f"agent:result:{stop_id}")
                assert json.loads(result_raw)["ok"] is True
                assert not balance_task.done()
            finally:
                balance_release.set()
                await asyncio.wait_for(balance_task, 2)
                worker.cancel()
                try:
                    await worker
                except asyncio.CancelledError:
                    pass
                await engine.shutdown()
        finally:
            await redis.aclose()

    asyncio.run(scenario())


def test_concurrent_start_stop_resolves_via_generation_barrier() -> None:
    """Concurrent Start/Stop against real Redis: Stop always wins over an
    in-flight Start it overlaps with (existing generation-cancel barrier)."""

    async def scenario() -> None:
        redis = await _connect()
        try:
            engine = Mock(
                start_run=AsyncMock(side_effect=lambda **_: asyncio.sleep(1)),
                stop=AsyncMock(),
            )
            control = _CommandQueue(redis, engine)
            worker = asyncio.create_task(control.run())
            try:
                control.submit(
                    envelope(
                        "start",
                        str(uuid.uuid4()),
                        network="testnet",
                        strategy="test",
                        symbols=["BTCUSDT"],
                    ),
                    "0-1",
                )
                await asyncio.sleep(0)  # let it start running
                control.submit(envelope("stop", str(uuid.uuid4())), "0-2")
                await asyncio.wait_for(control.queue.join(), 2)
                engine.stop.assert_awaited_once()
            finally:
                worker.cancel()
                try:
                    await worker
                except asyncio.CancelledError:
                    pass
        finally:
            await redis.aclose()

    asyncio.run(scenario())
