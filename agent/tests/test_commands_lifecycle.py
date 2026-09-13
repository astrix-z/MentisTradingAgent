from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from agent.common import commands
from agent.engine import Engine
from agent.main import _CommandQueue, _heartbeat_loop, _listen
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
    pipe = Mock(set=Mock(), execute=AsyncMock())
    context = Mock(
        __aenter__=AsyncMock(return_value=pipe), __aexit__=AsyncMock(return_value=False)
    )
    return Mock(
        pipeline=Mock(return_value=context),
        set=AsyncMock(),
        get=AsyncMock(return_value=None),
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
        assert pipe.execute.await_count == 2
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
            network="live", strategy="test", symbols=["BTCUSDT"], params={"window": 10}
        )

    asyncio.run(scenario())


def test_stop_barrier_and_new_start(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        redis = redis_mock()
        engine = Engine(redis)
        entered = asyncio.Event()
        count = 0
        strategy = Mock(on_stop=AsyncMock())

        async def bootstrap(*_: Any) -> None:
            nonlocal count
            count += 1
            if count == 1:
                entered.set()
                await asyncio.Event().wait()

        strategy.on_start = AsyncMock(side_effect=bootstrap)
        streams = Mock(start=AsyncMock(), stop=AsyncMock())
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
            control.submit(envelope("start", "active", **options))
            await entered.wait()
            control.submit(envelope("start", "queued", **options))
            control.submit(envelope("stop", "stop"))
            control.submit(envelope("start", "after-stop", **options))
            await asyncio.wait_for(control.queue.join(), 1)
            stored = results(redis)
            assert not stored["agent:result:active"]["ok"]
            assert not stored["agent:result:queued"]["ok"]
            assert stored["agent:result:stop"]["data"] == {"running": False}
            assert stored["agent:result:after-stop"]["ok"]
            assert engine.running
            assert count == 2
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
        incoming: asyncio.Queue[dict[str, str]] = asyncio.Queue()
        pubsub = Mock()

        async def subscribe(*_: Any) -> None:
            subscribed.set()

        async def get_message(**_: Any) -> Any:
            try:
                return incoming.get_nowait()
            except asyncio.QueueEmpty:
                return None

        pubsub.subscribe = AsyncMock(side_effect=subscribe)
        pubsub.get_message = AsyncMock(side_effect=get_message)
        redis.pubsub = Mock(
            return_value=Mock(
                __aenter__=AsyncMock(return_value=pubsub),
                __aexit__=AsyncMock(return_value=False),
            )
        )
        redis.get.return_value = json.dumps(
            {"running": True, "strategy": "test", "symbols": ["BTCUSDT"]}
        )
        monkeypatch.setitem(STRATEGIES, "test", Mock())
        strategy = Mock(on_stop=AsyncMock())

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
            Mock(return_value=Mock(start=AsyncMock(), stop=AsyncMock())),
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
            incoming.put_nowait({"type": "message", "data": envelope("stop", "stop")})
            incoming.put_nowait(
                {"type": "message", "data": envelope("observed", "new")}
            )
            await asyncio.wait_for(received.wait(), 1)
            assert engine.state == "idle"
            assert results(redis)["agent:result:stop"]["ok"]
        finally:
            stop.set()
            await asyncio.wait_for(asyncio.gather(listener, heartbeat), 1)
            await engine.shutdown()

    asyncio.run(scenario())


def test_start_failure_result(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        redis = redis_mock()
        engine = Engine(redis)
        strategy = Mock(
            on_start=AsyncMock(side_effect=RuntimeError("subscription failed")),
            on_stop=AsyncMock(),
        )
        monkeypatch.setattr("agent.engine.build_strategy", Mock(return_value=strategy))
        monkeypatch.setattr(
            "agent.engine.ExchangeRest.connect", Mock(return_value=Mock())
        )
        monkeypatch.setattr(
            "agent.engine.MarketDataStreams",
            Mock(return_value=Mock(start=AsyncMock(), stop=AsyncMock())),
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
