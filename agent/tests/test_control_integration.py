"""Offline backend -> agent integration; no HTTP/auth, DB, Redis or Binance.

Only infrastructure is fake: envelopes/handlers, Engine, strategy and Signal
transport are real. Publications are explicitly delivered to dispatch; this does
not exercise the listener or execute Redis Lua on a server.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Annotated, Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import Depends
from pydantic import BaseModel
from redis.asyncio import Redis
from test_strategy_runtime import (
    PARAMS,
    SYMBOL,
    TRIGGER,
    Rest,
    Streams,
    fixture,
    message,
)

from agent.common import commands
from agent.engine import Engine
from agent.strategies.tripple_screen.strategy import TripleScreenStrategy


@pytest.fixture
def trading(monkeypatch: pytest.MonkeyPatch) -> Generator[ModuleType]:
    # Load only the router, without importing backend settings/auth/DB clients.
    def unused_dependency() -> None:
        raise AssertionError("HTTP dependencies must not run")

    modules: dict[str, dict[str, Any]] = {
        "app.api.deps": {
            "CurrentUser": Annotated[BaseModel, Depends(unused_dependency)],
            "get_current_active_superuser": unused_dependency,
        },
        "app.core.redis_client": {"get_redis": unused_dependency},
        "app.models": {"User": BaseModel},
    }
    with monkeypatch.context() as imports:
        for name, attributes in modules.items():
            stub = ModuleType(name)
            stub.__dict__.update(attributes)
            imports.setitem(sys.modules, name, stub)
        path = Path(__file__).resolve().parents[2] / "backend/app/api/routes/trading.py"
        spec = importlib.util.spec_from_file_location(
            "_control_integration_trading", path
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        imports.setitem(sys.modules, spec.name, module)
        spec.loader.exec_module(module)
        yield module


class MemoryRedis:
    """Atomic CAS/publication and transactional result writes, without TTL expiry."""

    def __init__(self, trading: ModuleType) -> None:
        self.trading = trading
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int | None] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.publications: list[tuple[str, str, str | None]] = []
        self.signal_ready = asyncio.Event()

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, raw: str, *, ex: int | None = None) -> None:
        self.values[key] = raw
        self.ttls[key] = ex

    async def hget(self, key: str, field: str) -> str | None:
        return self.hashes.get(key, {}).get(field)

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    async def xack(self, *_args: Any, **_kwargs: Any) -> int:
        return 1

    async def publish(self, channel: str, raw: str) -> int:
        # Unrelated to command delivery: agent:events (signals) stays Pub/Sub.
        self.record(channel, raw)
        return 1

    def record(self, channel: str, raw: str) -> None:
        self.publications.append(
            (channel, raw, self.values.get(self.trading.REDIS_RUNTIME_KEY))
        )
        if channel == "agent:events":
            self.signal_ready.set()

    async def xadd(
        self,
        key: str,
        fields: dict[str, str],
        maxlen: int | None = None,
        approximate: bool | None = None,
    ) -> str:
        self.record(key, fields["data"])
        return "0-1"

    async def execute_command(
        self,
        command: str,
        script: str,
        numkeys: int,
        key: str,
        expected: str,
        snapshot: str,
        stream: str,
        envelope: str,
        maxlen: str,
    ) -> int:
        assert (command, script, numkeys, key, stream) == (
            "EVAL",
            self.trading.COMMIT_RUNTIME_LUA,
            1,
            self.trading.REDIS_RUNTIME_KEY,
            self.trading.REDIS_COMMAND_STREAM,
        )
        if self.values.get(key, "") != expected:
            return 0
        # No await between compare, write and enqueue.
        self.values[key] = snapshot
        self.record(stream, envelope)
        return 1

    def pipeline(self, *, transaction: bool) -> Any:
        assert transaction is True
        pending: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

        def set_value(key: str, raw: str, *, ex: int | None = None) -> None:
            pending.append(("set", (key, raw), {"ex": ex}))

        def hset_value(key: str, mapping: dict[str, str]) -> None:
            pending.append(("hset", (key,), {"mapping": mapping}))

        def expire_value(key: str, ttl: int) -> None:
            pending.append(("expire", (key, ttl), {}))

        async def execute() -> None:
            for name, args, kwargs in pending:
                if name == "set":
                    key, raw = args
                    self.values[key] = raw
                    self.ttls[key] = kwargs["ex"]
                elif name == "hset":
                    (key,) = args
                    self.hashes.setdefault(key, {}).update(kwargs["mapping"])
                elif name == "expire":
                    key, ttl = args
                    self.ttls[key] = ttl

        pipe = Mock(
            set=Mock(side_effect=set_value),
            hset=Mock(side_effect=hset_value),
            expire=Mock(side_effect=expire_value),
            execute=AsyncMock(side_effect=execute),
        )
        return Mock(
            __aenter__=AsyncMock(return_value=pipe),
            __aexit__=AsyncMock(return_value=False),
        )


def test_start_signal_stop_and_failed_start(
    monkeypatch: pytest.MonkeyPatch,
    trading: ModuleType,
) -> None:
    async def scenario() -> None:
        class Clock(datetime):
            @classmethod
            def now(cls, tz: Any = None) -> Clock:
                return cls.fromtimestamp((TRIGGER + 899_999) / 1000, UTC)

        monkeypatch.setattr("agent.strategies.tripple_screen.strategy.datetime", Clock)
        windows = fixture()
        rest, streams = Rest(windows), Streams()
        stream_start, stream_stop = AsyncMock(), AsyncMock()
        monkeypatch.setattr(streams, "start", stream_start, raising=False)
        monkeypatch.setattr(streams, "stop", stream_stop, raising=False)
        connect = Mock(return_value=rest)
        stream_factory = Mock(return_value=streams)
        monkeypatch.setattr("agent.engine.ExchangeRest.connect", connect)
        monkeypatch.setattr("agent.engine.MarketDataStreams", stream_factory)
        store = MemoryRedis(trading)
        redis = cast(Redis, store)
        monkeypatch.setattr(trading, "get_redis", lambda: redis)
        engine = Engine(redis)
        emit = AsyncMock(wraps=engine._on_signal)
        monkeypatch.setattr(engine, "_on_signal", emit)
        payload = trading._start_command_payload(
            trading.StartPayload(
                network="testnet",
                strategy="tripple_screen",
                symbols=["btcusdt"],
                params=PARAMS,
            )
        )

        async def accept(kind: str, revision: int) -> Any:
            envelope = await trading._apply_command(
                kind, payload if kind == "start" else {}
            )
            channel, raw, snapshot_raw = store.publications[-1]
            assert channel == trading.REDIS_COMMAND_STREAM
            assert json.loads(raw) == envelope.model_dump(mode="json")
            assert snapshot_raw == store.values["agent:runtime"]
            snapshot = json.loads(snapshot_raw)
            assert snapshot == {
                **payload,
                "running": kind == "start",
                "last_command": kind,
                "revision": revision,
                "updated_at": envelope.model_dump(mode="json")["issued_at"],
            }
            assert f"agent:result:{envelope.id}" not in store.values
            # Deliver the exact published bytes, not a reconstructed envelope.
            await commands.dispatch(engine, redis, raw)
            result_key = f"agent:result:{envelope.id}"
            assert store.values[result_key] == store.values["agent:result"]
            assert store.ttls[result_key] == 3600
            result = trading.CommandResult.model_validate_json(store.values[result_key])
            assert result.command_id == envelope.id and result.type == kind
            return result

        try:
            # Freeze backend time so snapshot/envelope timestamps can be compared.
            monkeypatch.setattr(
                trading, "_now", lambda: datetime(2026, 9, 12, tzinfo=UTC)
            )
            started = await accept("start", 1)
            assert started.ok and started.data == {
                "running": True,
                "network": "testnet",
                "strategy": "tripple_screen",
                "symbols": [SYMBOL],
            }
            assert engine.running and engine.state == "running"
            strategy = engine._strategy
            assert isinstance(strategy, TripleScreenStrategy)
            assert strategy.services.symbols == [SYMBOL]
            assert strategy.settings.kline_window == PARAMS["kline_window"]
            assert set(streams.callbacks) == {"4h", "1h", "15m"}
            assert {call["interval"] for call in rest.calls} == set(streams.callbacks)
            connect.assert_called_once_with("testnet")
            stream_factory.assert_called_once_with("testnet")
            stream_start.assert_awaited_once()
            assert json.loads(store.values["trading:heartbeat"])["state"] == "running"
            assert not store.signal_ready.is_set()

            streams.callbacks["15m"](message(windows["15m"][-1]))
            await asyncio.wait_for(store.signal_ready.wait(), 1)
            emit.assert_awaited_once()
            signal = emit.call_args.args[0]
            events = [
                json.loads(raw)
                for channel, raw, _ in store.publications
                if channel == "agent:events"
            ]
            assert events == [{"kind": "signal", **signal.transport()}]
            event = events[0]
            assert set(event) == {"kind", *signal.__dataclass_fields__}
            assert event["type"] == "ENTRY" and event["symbol"] == SYMBOL
            assert event["entry_ref"] == "11.200000000000000001"
            assert event["signal_id"] and event["epoch_id"] and event["config_version"]
            assert event["valid_until"] == signal.valid_until.isoformat()
            assert event["swing_low_ref"] is not None and event["tp_rule"]
            assert event["provenance"]["wave"]["conditions"] == {
                "oversold_exit": True,
                "stochastic_cross": True,
                "force_cross": True,
            }
            assert set(event["market_window_versions"]) == {"4h", "1h", "15m"}

            tasks = list(strategy._tasks)
            run_task = engine._task
            stopped = await accept("stop", 2)
            assert stopped.ok and stopped.data == {"running": False}
            assert not engine.running and engine.state == "idle"
            assert run_task is not None and run_task.done()
            assert all(task.done() for task in tasks)
            assert not strategy._tasks and not strategy._candles
            assert engine._strategy is None and engine._streams is None
            stream_stop.assert_awaited_once()
            assert json.loads(store.values["trading:heartbeat"])["state"] == "idle"
            streams.callbacks["15m"](message(windows["15m"][-1]))
            await asyncio.sleep(0)
            emit.assert_awaited_once()
            assert not strategy._tasks

            monkeypatch.setattr(
                rest, "klines", AsyncMock(side_effect=RuntimeError("bootstrap offline"))
            )
            failed = await accept("start", 3)
            assert not failed.ok and failed.message == "bootstrap offline"
            assert failed.data is None
            assert engine.state == "error" and not engine.running
            assert engine._startup is None and engine._task is None
            assert engine._strategy is None and engine._streams is None
            assert engine._rest is None
            assert stream_stop.await_count == 2
            heartbeat = trading.AgentHeartbeat.model_validate_json(
                store.values["trading:heartbeat"]
            )
            assert (
                heartbeat.state == "error" and "bootstrap offline" in heartbeat.message
            )
            assert json.loads(store.values["agent:runtime"])["running"] is True
            assert (
                trading.CommandResult.model_validate_json(
                    store.values[f"agent:result:{started.command_id}"]
                )
                == started
            )
            streams.callbacks["15m"](message(windows["15m"][-1]))
            await asyncio.sleep(0)
            emit.assert_awaited_once()
        finally:
            await engine.shutdown()

    asyncio.run(asyncio.wait_for(scenario(), 5))
