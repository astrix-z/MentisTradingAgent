from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from agent.common import commands
from agent.core.config import settings
from agent.engine import Engine
from agent.main import _CommandQueue, _handle_independent, _heartbeat_loop, _listen
from agent.strategies import STRATEGIES


def envelope(kind: str, id_: str, **payload: Any) -> str:
    return json.dumps(
        {
            "id": id_,
            "type": kind,
            "payload": payload,
            "issued_at": datetime.now(UTC).isoformat(),
        }
    )


def redis_mock() -> Any:
    pipe = Mock(set=Mock(), hset=Mock(), expire=Mock(), execute=AsyncMock())
    context = Mock(
        __aenter__=AsyncMock(return_value=pipe), __aexit__=AsyncMock(return_value=False)
    )
    return Mock(
        pipeline=Mock(return_value=context),
        set=AsyncMock(),
        get=AsyncMock(return_value=None),
        hget=AsyncMock(return_value=None),  # dedup check: nothing terminal yet
        xack=AsyncMock(return_value=1),
    )


def results(redis: Any) -> dict[str, Any]:
    pipe = redis.pipeline.return_value.__aenter__.return_value
    return {call.args[0]: json.loads(call.args[1]) for call in pipe.set.call_args_list}


def test_correlated_results() -> None:
    async def scenario() -> None:
        redis = redis_mock()
        engine = Engine(redis)
        for id_ in ("first", "second"):
            command = commands.AgentCommand.from_json(envelope("balance", id_))
            await commands.CommandContext(engine, redis, command).report(
                ok=True, data={"BTC": 1}
            )
        stored = results(redis)
        assert stored["agent:result"]["command_id"] == "second"
        assert stored["agent:result:first"]["command_id"] == "first"
        assert stored["agent:result:second"] == stored["agent:result"]
        pipe = redis.pipeline.return_value.__aenter__.return_value
        assert pipe.set.call_args_list[1].kwargs == {"ex": 3600}
        assert pipe.set.call_args_list[3].kwargs == {"ex": 3600}
        # Each report() does two pipelined blocks: the result write (2 SETs)
        # and the state-hash transition to "succeeded" (HSET + EXPIRE).
        assert pipe.execute.await_count == 4
        assert [
            call.kwargs["mapping"]["status"] for call in pipe.hset.call_args_list
        ] == [
            "succeeded",
            "succeeded",
        ]
        redis.pipeline.assert_called_with(transaction=True)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "raw", ["null", "[]", '{"running": false}', '{"running": "true"}', "broken"]
)
def test_boot_off(raw: str) -> None:
    async def scenario() -> None:
        redis = redis_mock()
        redis.get.return_value = raw
        engine = Mock(start_run=AsyncMock())
        await commands.resume_from_runtime(engine, redis)
        engine.start_run.assert_not_awaited()

    asyncio.run(scenario())


def test_saved_running_resumes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        redis = redis_mock()
        redis.get.return_value = json.dumps(
            {
                "running": True,
                "network": "live",
                "strategy": "test",
                "symbols": ["btcusdt"],
                "params": {"window": 10},
            }
        )
        monkeypatch.setitem(STRATEGIES, "test", Mock())
        engine = Mock(start_run=AsyncMock())
        await commands.resume_from_runtime(engine, redis)
        engine.start_run.assert_awaited_once_with(
            network="live",
            strategy="test",
            symbols=["BTCUSDT"],
            params={"window": 10},
            execution=None,
        )

    asyncio.run(scenario())


def test_stop_barrier_and_new_start(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        redis = redis_mock()
        engine = Engine(redis)
        entered = asyncio.Event()
        count = 0
        strategy = Mock(on_stop=AsyncMock(), active_symbols=None, health_warning=None)

        async def bootstrap(*_: Any) -> None:
            nonlocal count
            count += 1
            if count == 1:
                entered.set()
                await asyncio.Event().wait()

        strategy.on_start = AsyncMock(side_effect=bootstrap)
        streams = Mock(
            start=AsyncMock(), stop=AsyncMock(), health_warning=Mock(return_value=None)
        )
        monkeypatch.setattr("agent.engine.build_strategy", Mock(return_value=strategy))
        monkeypatch.setattr(
            "agent.engine.ExchangeRest.connect", Mock(return_value=Mock())
        )
        monkeypatch.setattr(
            "agent.engine.MarketDataStreams", Mock(return_value=streams)
        )
        control = _CommandQueue(redis, engine)
        worker = asyncio.create_task(control.run())
        options = {"network": "testnet", "strategy": "test", "symbols": ["BTCUSDT"]}
        try:
            control.submit(envelope("start", "active", **options), "ack-active")
            await entered.wait()
            control.submit(envelope("start", "queued", **options), "ack-queued")
            control.submit(envelope("stop", "stop"), "ack-stop")
            control.submit(envelope("start", "after-stop", **options), "ack-after-stop")
            await asyncio.wait_for(control.queue.join(), 1)
            stored = results(redis)
            assert not stored["agent:result:active"]["ok"]
            assert not stored["agent:result:queued"]["ok"]
            assert stored["agent:result:stop"]["data"] == {"running": False}
            assert stored["agent:result:after-stop"]["ok"]
            assert engine.running
            assert count == 2
            assert sorted(call.args[-1] for call in redis.xack.call_args_list) == [
                "ack-active",
                "ack-after-stop",
                "ack-queued",
                "ack-stop",
            ]
        finally:
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker
            await engine.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("fail_resume", [False, True])
def test_listener_and_heartbeat_during_resume(
    monkeypatch: pytest.MonkeyPatch, fail_resume: bool
) -> None:
    async def scenario() -> None:
        redis = redis_mock()
        engine = Engine(redis)
        stop = asyncio.Event()
        subscribed, entered, received = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )
        incoming: asyncio.Queue[tuple[str, dict[str, str]]] = asyncio.Queue()

        async def ensure_group(*_a: Any, **_kw: Any) -> None:
            subscribed.set()

        async def xreadgroup(
            _group: str,
            _consumer: str,
            streams: dict[str, str],
            *,
            block: int,
            **_kw: Any,
        ) -> list[tuple[str, list[tuple[str, dict[str, str]]]]]:
            (stream_name,) = streams
            try:
                entry_id, fields = incoming.get_nowait()
            except asyncio.QueueEmpty:
                # Mirror real Redis's server-side BLOCK: without this, an
                # empty queue would spin the read loop as fast as physically
                # possible instead of waiting.
                await asyncio.sleep(block / 1000)
                return []
            return [(stream_name, [(entry_id, fields)])]

        redis.xgroup_create = AsyncMock(side_effect=ensure_group)
        redis.xreadgroup = AsyncMock(side_effect=xreadgroup)
        redis.get.return_value = json.dumps(
            {"running": True, "strategy": "test", "symbols": ["BTCUSDT"]}
        )
        monkeypatch.setitem(STRATEGIES, "test", Mock())
        strategy = Mock(on_stop=AsyncMock(), active_symbols=None, health_warning=None)

        async def bootstrap(*_: Any) -> None:
            assert subscribed.is_set()
            entered.set()
            if fail_resume:
                raise RuntimeError("bootstrap failure")
            await asyncio.Event().wait()

        strategy.on_start = AsyncMock(side_effect=bootstrap)
        monkeypatch.setattr("agent.engine.build_strategy", Mock(return_value=strategy))
        monkeypatch.setattr(
            "agent.engine.ExchangeRest.connect", Mock(return_value=Mock())
        )
        monkeypatch.setattr(
            "agent.engine.MarketDataStreams",
            Mock(
                return_value=Mock(
                    start=AsyncMock(),
                    stop=AsyncMock(),
                    health_warning=Mock(return_value=None),
                )
            ),
        )

        async def observed(
            ctx: commands.CommandContext, payload: dict[str, Any]
        ) -> None:
            received.set()
            await ctx.report(ok=True)

        monkeypatch.setitem(commands.HANDLERS, "observed", observed)
        monkeypatch.setattr("agent.main._POLL_QUANTUM", 0.001)
        monkeypatch.setattr("agent.main.HEARTBEAT_INTERVAL", 0.001)
        listener = asyncio.create_task(_listen(redis, engine, stop))
        heartbeat = asyncio.create_task(_heartbeat_loop(engine, stop))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.sleep(0.01)
            assert redis.set.await_count > 1
            assert not listener.done() and not heartbeat.done()
            if fail_resume:
                assert engine.state == "error"
            incoming.put_nowait(("1-1", {"data": envelope("stop", "stop")}))
            incoming.put_nowait(("1-2", {"data": envelope("observed", "new")}))
            await asyncio.wait_for(received.wait(), 1)
            assert engine.state == "idle"
            assert results(redis)["agent:result:stop"]["ok"]
        finally:
            stop.set()
            await asyncio.wait_for(asyncio.gather(listener, heartbeat), 1)
            await engine.shutdown()

    asyncio.run(scenario())


def test_balance_missing_network_reports_failure() -> None:
    async def scenario() -> None:
        redis = redis_mock()
        engine = Mock(fetch_account_balance_snapshot=AsyncMock())
        await commands.dispatch(engine, redis, envelope("balance", "no-network"))
        result = results(redis)["agent:result:no-network"]
        assert result["ok"] is False
        assert "network" in result["message"]
        engine.fetch_account_balance_snapshot.assert_not_awaited()

    asyncio.run(scenario())


def test_balance_command_returns_typed_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        redis = redis_mock()
        engine = Engine(redis)
        rows = [
            Mock(asset="USDT", free="12.34", locked="0.00"),
            Mock(asset="BTC", free="0", locked="0.50"),  # locked-only, still non-zero
            Mock(asset="ETH", free="0", locked="0"),  # zero balance, omitted
        ]
        rest = Mock(balances=AsyncMock(return_value=rows))
        monkeypatch.setattr(
            "agent.engine.ExchangeRest.connect", Mock(return_value=rest)
        )
        await commands.dispatch(
            engine, redis, envelope("balance", "bal-1", network="testnet")
        )
        result = results(redis)["agent:result:bal-1"]
        assert result["ok"] is True
        assert result["data"]["network"] == "testnet"
        assert "fetched_at" in result["data"]
        assert result["data"]["assets"] == [
            {"asset": "USDT", "free": "12.34", "locked": "0.00"},
            {"asset": "BTC", "free": "0", "locked": "0.50"},
        ]

    asyncio.run(scenario())


def test_indicators_without_a_running_strategy_reports_failure() -> None:
    async def scenario() -> None:
        redis = redis_mock()
        await commands.dispatch(Engine(redis), redis, envelope("indicators", "ind-0"))
        result = results(redis)["agent:result:ind-0"]
        assert result["ok"] is False
        assert "no running strategy" in result["message"]

    asyncio.run(scenario())


def test_indicators_returns_the_running_strategy_snapshot() -> None:
    async def scenario() -> None:
        redis = redis_mock()
        engine = Engine(redis)
        engine._state = "running"
        engine._network = "live"
        engine._strategy_name = "tripple_screen"
        engine._strategy = Mock(
            indicator_snapshot=Mock(return_value={"groups": [], "rows": []})
        )
        await commands.dispatch(engine, redis, envelope("indicators", "ind-1"))
        result = results(redis)["agent:result:ind-1"]
        assert result["ok"] is True
        assert result["data"]["strategy"] == "tripple_screen"
        assert result["data"]["network"] == "live"
        assert "computed_at" in result["data"]
        assert result["data"]["groups"] == [] and result["data"]["rows"] == []

    asyncio.run(scenario())


def test_start_failure_result(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        redis = redis_mock()
        engine = Engine(redis)
        strategy = Mock(
            on_start=AsyncMock(side_effect=RuntimeError("subscription failed")),
            on_stop=AsyncMock(),
            active_symbols=None,
            health_warning=None,
        )
        monkeypatch.setattr("agent.engine.build_strategy", Mock(return_value=strategy))
        monkeypatch.setattr(
            "agent.engine.ExchangeRest.connect", Mock(return_value=Mock())
        )
        monkeypatch.setattr(
            "agent.engine.MarketDataStreams",
            Mock(
                return_value=Mock(
                    start=AsyncMock(),
                    stop=AsyncMock(),
                    health_warning=Mock(return_value=None),
                )
            ),
        )
        await commands.dispatch(
            engine,
            redis,
            envelope("start", "failed", strategy="test", symbols=["BTCUSDT"]),
        )
        result = results(redis)["agent:result:failed"]
        assert result["ok"] is False
        assert result["message"] == "subscription failed"
        assert engine.state == "error" and not engine.running

    asyncio.run(scenario())


def test_start_timeout_scales_with_symbol_count() -> None:
    base, per_symbol, ceiling = (
        commands.START_TIMEOUT_BASE_SECONDS,
        commands.START_TIMEOUT_PER_SYMBOL_SECONDS,
        commands.START_TIMEOUT_MAX_SECONDS,
    )
    assert commands._start_timeout_seconds(0) == base
    assert commands._start_timeout_seconds(10) == base + 10 * per_symbol
    assert commands._start_timeout_seconds(10_000) == ceiling


def test_start_timeout_reports_timed_out(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        redis = redis_mock()
        engine = Engine(redis)

        async def hang(*_a: Any, **_kw: Any) -> None:
            await asyncio.sleep(10)

        monkeypatch.setattr(engine, "start_run", AsyncMock(side_effect=hang))
        monkeypatch.setattr(commands, "START_TIMEOUT_BASE_SECONDS", 0.01)
        monkeypatch.setattr(commands, "START_TIMEOUT_PER_SYMBOL_SECONDS", 0.0)
        monkeypatch.setattr(commands, "START_TIMEOUT_MAX_SECONDS", 1.0)
        await asyncio.wait_for(
            commands.dispatch(
                engine,
                redis,
                envelope("start", "slow-start", strategy="test", symbols=["BTCUSDT"]),
            ),
            2,
        )
        result = results(redis)["agent:result:slow-start"]
        assert result["ok"] is False
        assert "timed out" in result["message"]

    asyncio.run(scenario())


def test_dispatch_skips_redelivery_of_an_already_terminal_command() -> None:
    """Prompt 5 dedup: redelivery of a completed command_id never re-runs it."""

    async def scenario() -> None:
        redis = redis_mock()
        redis.hget = AsyncMock(return_value="succeeded")
        engine = Mock(stop=AsyncMock())
        await commands.dispatch(engine, redis, envelope("stop", "dup-1"))
        engine.stop.assert_not_awaited()
        pipe = redis.pipeline.return_value.__aenter__.return_value
        pipe.set.assert_not_called()  # no new result written for a skipped redelivery

    asyncio.run(scenario())


def test_dispatch_reruns_a_command_stuck_dispatched_or_running() -> None:
    """A crash mid-handling (status stuck at dispatched/running, never
    terminal) must still be re-run on redelivery — only a terminal status
    is treated as already-handled."""

    async def scenario() -> None:
        redis = redis_mock()
        redis.hget = AsyncMock(return_value="running")
        engine = Mock(stop=AsyncMock())
        await commands.dispatch(engine, redis, envelope("stop", "resumed-1"))
        engine.stop.assert_awaited_once()

    asyncio.run(scenario())


def test_balance_timeout_reports_timed_out_not_a_hang(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        redis = redis_mock()
        engine = Engine(redis)

        async def hang(*_a: Any, **_kw: Any) -> list[Any]:
            await asyncio.sleep(10)
            return []

        monkeypatch.setattr(
            "agent.engine.ExchangeRest.connect",
            Mock(return_value=Mock(balances=AsyncMock(side_effect=hang))),
        )
        monkeypatch.setattr(settings, "COMMAND_IO_TIMEOUT_SECONDS", 0.01)
        await asyncio.wait_for(
            commands.dispatch(
                engine, redis, envelope("balance", "slow-1", network="testnet")
            ),
            1,
        )
        result = results(redis)["agent:result:slow-1"]
        assert result["ok"] is False
        assert "timed out" in result["message"]
        pipe = redis.pipeline.return_value.__aenter__.return_value
        statuses = [
            call.kwargs["mapping"]["status"] for call in pipe.hset.call_args_list
        ]
        assert statuses[-1] == "timed_out"

    asyncio.run(scenario())


def test_stop_completes_without_waiting_for_a_slow_balance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prompt 5: Balance never shares the serialized start/stop queue, so a
    slow balance fetch cannot make Stop wait behind it."""

    async def scenario() -> None:
        redis = redis_mock()
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
                envelope("balance", "slow-bal", network="testnet"),
                "ack-bal",
            )
        )
        try:
            await asyncio.wait_for(balance_started.wait(), 1)
            control.submit(envelope("stop", "stop-1"), "ack-stop")
            await asyncio.wait_for(control.queue.join(), 1)
            stored = results(redis)
            assert stored["agent:result:stop-1"]["ok"] is True
            assert not balance_task.done()  # still stuck; Stop did not wait on it
        finally:
            balance_release.set()
            await asyncio.wait_for(balance_task, 1)
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker
            await engine.shutdown()

    asyncio.run(scenario())
