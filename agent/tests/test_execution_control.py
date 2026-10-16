"""Real trading is the owner's switch, apart from Start (ClickUp 869fgqxah).

The switch and the limits travel by ``execution_configure`` and live in
``agent:execution``; a Start, a resume or a network choice never change them.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from redis.exceptions import RedisError
from test_commands_lifecycle import redis_mock, results
from test_engine_lifecycle import setup_engine, start
from test_strategy_feedback import _recording, _watch

from agent.common import commands
from agent.engine import REDIS_EXECUTION_KEY, Engine
from agent.execution.settings import ExecutionSettings
from agent.strategies.base import PositionsSnapshot

ENABLED = ExecutionSettings(enabled=True)


def fake_runtime(*, schema_ready: bool = True) -> Any:
    """The parts of ``ExecutionRuntime`` the engine talks to."""
    runtime = Mock(
        settings=ExecutionSettings(),
        schema_ready=schema_ready,
        alerts={},
        prepare_start=AsyncMock(),
        positions_snapshot=AsyncMock(
            return_value=PositionsSnapshot(open_symbols=frozenset({"BTCUSDT"}))
        ),
        open_admission=Mock(),
        close_admission=Mock(),
        message=Mock(return_value=None),
    )

    def configure(settings: ExecutionSettings) -> None:
        if settings.enabled and not runtime.schema_ready:
            raise ValueError("agent database is not ready; real trading refused")
        runtime.settings = settings

    runtime.configure = Mock(side_effect=configure)
    runtime.heartbeat = Mock(
        side_effect=lambda: {"mode": "enabled" if runtime.settings.enabled else "off"}
    )
    return runtime


def test_switching_trading_reseeds_the_running_strategy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # On: the strategy holds the real positions, not the followed signals.
    # Off again: the followed signals, their recent exits replayed.
    async def scenario() -> None:
        engine, _redis, strategy, _streams = setup_engine(monkeypatch)
        seen = _recording(strategy)
        crossed = datetime.now(UTC).replace(second=0, microsecond=0)
        tracker = Mock(
            snapshot=AsyncMock(
                return_value=(
                    frozenset({"ETHUSDT"}),
                    [_watch("SOLUSDT", "sol", crossed - timedelta(minutes=30))],
                )
            )
        )
        engine.tracker = tracker
        runtime = fake_runtime()
        engine.execution = runtime
        await start(engine)
        assert seen[0] == "restore ['BTCUSDT', 'ETHUSDT']"
        seen.clear()

        await engine.configure_execution(ENABLED)
        assert seen == ["restore ['BTCUSDT']"]
        assert not engine._virtual_feedback
        seen.clear()

        await engine.configure_execution(ENABLED.model_copy(update={"max_symbols": 2}))
        assert seen == []  # the same switch: limits only, nothing re-seeded

        await engine.configure_execution(ExecutionSettings())
        assert seen == [
            "restore ['BTCUSDT', 'ETHUSDT']",
            f"closed SOLUSDT at {crossed - timedelta(minutes=30):%H:%M}",
        ]
        assert engine._virtual_feedback and engine._startup_feedback is None
        assert engine.running  # the run itself was never touched
        await engine.shutdown()

    asyncio.run(scenario())


def test_trading_on_is_refused_for_a_strategy_quoted_in_usdc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        engine, *_ = setup_engine(monkeypatch)
        runtime = fake_runtime()
        engine.execution = runtime
        await start(engine, params={"quote_asset": "USDC"})
        with pytest.raises(ValueError, match="USDT only"):
            await engine.configure_execution(ENABLED)
        assert not runtime.settings.enabled
        await engine.stop()
        await engine.configure_execution(ENABLED)  # nothing runs: allowed
        assert runtime.settings.enabled
        await engine.shutdown()

    asyncio.run(scenario())


def test_a_start_never_switches_trading(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        redis = redis_mock()
        engine = Mock(start_run=AsyncMock())
        raw = json.dumps(
            {
                "id": "c1",
                "type": "start",
                "issued_at": datetime.now(UTC).isoformat(),
                "payload": {
                    "network": "live",
                    "strategy": "test",
                    "symbols": ["BTCUSDT"],
                    "params": {},
                    "execution": {"enabled": True},
                },
            }
        )
        await commands.dispatch(engine, redis, raw)
        engine.start_run.assert_awaited_once_with(
            network="live", strategy="test", symbols=["BTCUSDT"], params={}
        )

    asyncio.run(scenario())


def _configure_command(settings: Any) -> str:
    return json.dumps(
        {
            "id": "x1",
            "type": "execution_configure",
            "issued_at": datetime.now(UTC).isoformat(),
            "payload": {"settings": settings},
        }
    )


def test_execution_configure_applies_or_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        engine, *_ = setup_engine(monkeypatch)
        runtime = fake_runtime()
        engine.execution = runtime

        redis = redis_mock()
        await commands.dispatch(
            engine, redis, _configure_command({"enabled": True, "max_symbols": 2})
        )
        assert results(redis)["agent:result:x1"]["ok"] is True
        assert results(redis)["agent:result:x1"]["data"] == {"mode": "enabled"}
        assert runtime.settings == ExecutionSettings(enabled=True, max_symbols=2)

        for bad in ({"enabled": True, "max_slippage_pct": 50}, {"unknown": 1}, 7):
            redis = redis_mock()
            await commands.dispatch(engine, redis, _configure_command(bad))
            assert results(redis)["agent:result:x1"]["ok"] is False
        assert runtime.settings == ExecutionSettings(enabled=True, max_symbols=2)

        runtime.schema_ready = False
        redis = redis_mock()
        await commands.dispatch(engine, redis, _configure_command({"enabled": True}))
        refused = results(redis)["agent:result:x1"]
        assert refused["ok"] is False and "not ready" in refused["message"]

        engine.execution = None
        redis = redis_mock()
        await commands.dispatch(engine, redis, _configure_command({}))
        assert results(redis)["agent:result:x1"]["ok"] is False

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("stored", "enabled", "alert"),
    [
        (None, False, None),
        (json.dumps({"settings": {"enabled": True, "max_symbols": 2}}), True, None),
        (
            json.dumps({"settings": {"enabled": True, "max_slippage_pct": 50}}),
            False,
            "refused",
        ),
        ("not json", False, "refused"),
        (json.dumps(["settings"]), False, "refused"),
    ],
)
def test_boot_restores_the_stored_switch_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    stored: str | None,
    enabled: bool,
    alert: str | None,
) -> None:
    async def scenario() -> None:
        engine, *_ = setup_engine(monkeypatch)
        runtime = fake_runtime()
        engine.execution = runtime
        redis = Mock(
            get=AsyncMock(side_effect={REDIS_EXECUTION_KEY: stored}.get),
            set=AsyncMock(),
        )
        await commands.load_execution_settings(engine, redis)
        assert redis.get.await_args_list[0].args == (REDIS_EXECUTION_KEY,)
        assert runtime.settings.enabled is enabled
        if alert is None:
            assert "settings" not in runtime.alerts
        else:
            assert alert in runtime.alerts["settings"]

    asyncio.run(scenario())


def test_boot_without_redis_keeps_trading_off(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        engine: Engine
        engine, *_ = setup_engine(monkeypatch)
        runtime = fake_runtime()
        engine.execution = runtime
        redis = Mock(get=AsyncMock(side_effect=RedisError("down")))
        await commands.load_execution_settings(engine, redis)
        assert not runtime.settings.enabled
        assert "Redis unavailable" in runtime.alerts["settings"]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("quote", "enabled"), [("USDC", False), ("USDT", True), (None, True)]
)
def test_a_stored_switch_the_resumed_strategy_cannot_trade_stays_off(
    monkeypatch: pytest.MonkeyPatch, quote: str | None, enabled: bool
) -> None:
    # Review 869fgqxah, P2-3: applied at boot, a refused ON made the resume
    # itself fail on "USDT only" — the strategy did not come back.
    async def scenario() -> None:
        engine, *_ = setup_engine(monkeypatch)
        runtime = fake_runtime()
        engine.execution = runtime
        params = {} if quote is None else {"quote_asset": quote}
        stored = {
            REDIS_EXECUTION_KEY: json.dumps({"settings": {"enabled": True}}),
            "agent:runtime": json.dumps({"running": True, "params": params}),
        }
        redis = Mock(get=AsyncMock(side_effect=stored.get), set=AsyncMock())
        await commands.load_execution_settings(engine, redis)
        assert runtime.settings.enabled is enabled
        if not enabled:
            assert "USDT only" in runtime.alerts["settings"]

    asyncio.run(scenario())
