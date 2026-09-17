from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from redis.exceptions import RedisError

from agent.engine import REDIS_EVENTS_CHANNEL, Engine
from agent.exchange.streams import MarketDataStreams
from agent.strategies import Signal


def setup_engine(monkeypatch: pytest.MonkeyPatch) -> tuple[Engine, Any, Any, Any]:
    redis = Mock(set=AsyncMock(), publish=AsyncMock())
    strategy = Mock(on_start=AsyncMock(), on_stop=AsyncMock())
    streams = Mock(start=AsyncMock(), stop=AsyncMock())
    monkeypatch.setattr("agent.engine.build_strategy", Mock(return_value=strategy))
    monkeypatch.setattr("agent.engine.ExchangeRest.connect", Mock(return_value=Mock()))
    monkeypatch.setattr("agent.engine.MarketDataStreams", Mock(return_value=streams))
    return Engine(redis), redis, strategy, streams


async def start(engine: Engine, **overrides: Any) -> None:
    options: dict[str, Any] = dict(
        network="testnet", strategy="test", symbols=["btcusdt"], params={}
    )
    options.update(overrides)
    await engine.start_run(**options)


@pytest.mark.parametrize(
    "stage", ["rest", "constructor", "connect", "on_start", "heartbeat"]
)
def test_start_rollback(monkeypatch: pytest.MonkeyPatch, stage: str) -> None:
    async def scenario() -> None:
        engine, redis, strategy, streams = setup_engine(monkeypatch)
        error = RuntimeError(f"failpoint {stage}")
        if stage == "rest":
            monkeypatch.setattr(
                "agent.engine.ExchangeRest.connect", Mock(side_effect=error)
            )
        elif stage == "constructor":
            monkeypatch.setattr(
                "agent.engine.MarketDataStreams", Mock(side_effect=error)
            )
        elif stage == "connect":
            streams.start.side_effect = error
        elif stage == "on_start":
            strategy.on_start.side_effect = error
        else:
            redis.set.side_effect = [error, None, None, None]
        with pytest.raises(RuntimeError, match="failpoint"):
            await start(engine)
        assert engine.state == "error"
        assert not engine.running
        assert engine._task is None
        assert engine._startup is None
        assert engine._strategy is None
        assert engine._streams is None
        heartbeat = json.loads(redis.set.call_args.args[1])
        assert heartbeat["state"] == "error"
        assert str(error) in heartbeat["message"]
        assert streams.stop.await_count == (stage not in ("rest", "constructor"))
        assert strategy.on_stop.await_count == (stage in ("on_start", "heartbeat"))
        await engine.shutdown()


def test_start_run_cancellation_stops_the_orphaned_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller giving up on ``start_run`` (e.g. the ``start`` command's own
    timeout) must actually cancel the in-flight bootstrap, not merely stop
    awaiting it — otherwise it keeps running unobserved and can clobber a
    later start/stop."""

    async def scenario() -> None:
        engine, _redis, strategy, _streams = setup_engine(monkeypatch)
        entered = asyncio.Event()

        async def hang(*_a: Any, **_kw: Any) -> None:
            entered.set()
            await asyncio.sleep(10)

        strategy.on_start.side_effect = hang
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(start(engine), timeout=0.05)
        await asyncio.wait_for(entered.wait(), timeout=1)
        for _ in range(200):
            if strategy.on_stop.await_count:
                break
            await asyncio.sleep(0.01)
        assert strategy.on_stop.await_count == 1
        assert engine.state == "error"
        assert engine._startup is None
        await engine.shutdown()

    asyncio.run(scenario())

    asyncio.run(scenario())


@pytest.mark.parametrize("via_stop", [False, True])
def test_cancel_bootstrap(monkeypatch: pytest.MonkeyPatch, via_stop: bool) -> None:
    async def scenario() -> None:
        engine, _, strategy, streams = setup_engine(monkeypatch)
        entered = asyncio.Event()

        async def bootstrap(*_: Any) -> None:
            entered.set()
            await asyncio.Event().wait()

        strategy.on_start.side_effect = bootstrap
        task = asyncio.create_task(start(engine))
        await entered.wait()
        if via_stop:
            await asyncio.wait_for(engine.stop(), 1)
        else:
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert engine.state == ("idle" if via_stop else "error")
        assert not engine.running
        strategy.on_stop.assert_awaited_once()
        streams.stop.assert_awaited_once()
        assert engine._strategy is None
        assert engine._streams is None

    asyncio.run(scenario())


def test_partial_subscriptions_cleanup_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        engine, _, strategy, streams = setup_engine(monkeypatch)
        subscriptions: list[str] = []

        async def bootstrap(*_: Any) -> None:
            subscriptions.extend(["ticker", "kline"])
            raise RuntimeError("third subscription failed")

        async def cleanup() -> None:
            subscriptions.clear()
            raise RuntimeError("on_stop failed after cleanup")

        strategy.on_start.side_effect = bootstrap
        strategy.on_stop.side_effect = cleanup
        with pytest.raises(RuntimeError, match="third subscription"):
            await start(engine)
        assert not subscriptions
        streams.stop.assert_awaited_once()
        assert engine.state == "error"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "overrides",
    [
        {"network": "invalid"},
        {"symbols": []},
        {"symbols": [""]},
        {"symbols": [None]},
        {"symbols": ["BTC/USDT"]},
    ],
)
def test_validation_preserves_run(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any]
) -> None:
    async def scenario() -> None:
        engine, _, strategy, streams = setup_engine(monkeypatch)
        await start(engine)
        task = engine._task
        with pytest.raises(ValueError):
            await start(engine, **overrides)
        assert engine.running and engine.state == "running"
        assert engine._task is task
        strategy.on_stop.assert_not_awaited()
        streams.stop.assert_not_awaited()
        await engine.stop()

    asyncio.run(scenario())


def test_serialized_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        engine, _, strategy, _ = setup_engine(monkeypatch)
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def bootstrap(*_: Any) -> None:
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()

        strategy.on_start.side_effect = bootstrap
        first = asyncio.create_task(start(engine))
        await entered.wait()
        second = asyncio.create_task(start(engine))
        await asyncio.sleep(0)
        assert calls == 1
        release.set()
        await asyncio.gather(first, second)
        assert calls == 2
        await engine.stop()

    asyncio.run(scenario())


def test_full_signal_event_and_redis_failure() -> None:
    async def scenario() -> None:
        redis = Mock(publish=AsyncMock(side_effect=[RedisError("offline"), None]))
        engine = Engine(redis)
        now = datetime.now(UTC)
        signal = Signal(
            type="ENTRY",
            symbol="BTCUSDT",
            emitted_at=now,
            reason="entry",
            epoch_id="epoch",
            entry_ref=Decimal("123.000000000000000001"),
            swing_low_ref=Decimal("120.00"),
            tp_rule="rule",
            valid_until=now,
            provenance={"nested": [Decimal("0.100"), {"at": now}]},
            config_version="config",
            market_window_versions={"1h": "window"},
        )
        with pytest.raises(RedisError):
            await engine._on_signal(signal)
        await engine._on_signal(signal)
        first, second = redis.publish.call_args_list
        assert first == second
        assert second.args[0] == REDIS_EVENTS_CHANNEL
        payload = json.loads(second.args[1])
        assert payload == {"kind": "signal", **signal.transport()}
        assert payload["entry_ref"] == "123.000000000000000001"
        assert payload["provenance"]["nested"][0] == "0.100"
        assert payload["signal_id"] == signal.signal_id

    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["connect", "bootstrap", "stop"])
def test_repeated_cancel_finishes_cleanup(
    monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    async def scenario() -> None:
        engine, _, strategy, streams = setup_engine(monkeypatch)
        entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def block(*_: Any) -> None:
            entered.set()
            await asyncio.Event().wait()

        async def close() -> None:
            cleaning.set()
            await release.wait()

        streams.stop.side_effect = close
        if stage == "stop":
            await start(engine)
            task = asyncio.create_task(engine.stop())
        else:
            if stage == "connect":
                streams.start.side_effect = block
            else:
                strategy.on_start.side_effect = block
            task = asyncio.create_task(start(engine))
            await entered.wait()
            task.cancel()
        await cleaning.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert engine._streams is None
        assert engine._strategy is None
        assert engine._task is None
        assert engine._startup is None
        streams.stop.assert_awaited_once()

    asyncio.run(scenario())


def test_streams_close_partial_sdk_connection() -> None:
    async def scenario() -> None:
        streams = MarketDataStreams.__new__(MarketDataStreams)
        streams._network = "testnet"
        streams._connection = None
        streams._handles = [
            Mock(unsubscribe=AsyncMock(side_effect=RuntimeError("unsubscribe")))
        ]
        streams._kline_handles = {}
        streams._client = Mock(close_connection=AsyncMock())
        await streams.stop()
        streams._client.close_connection.assert_awaited_once_with(close_session=True)
        assert not streams._handles

    asyncio.run(scenario())
