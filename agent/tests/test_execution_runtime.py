"""EXEC-05: ExecutionRuntime — lifecycle, single writer, context, admission.

Runs on a throwaway PostgreSQL database (advisory locks and their loss are the
point) with the fake exchange underneath the production trade client.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from fake_exchange import FakeExchange
from pg import scratch_database
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from test_engine_lifecycle import setup_engine, start

from agent.core.migrate import migrate
from agent.exchange.budget import RequestBudget
from agent.exchange.trade import TradeClient
from agent.execution.models import (
    AttemptState,
    OrderAttempt,
    Position,
    PositionStatus,
    SignalInbox,
)
from agent.execution.runtime import ContextRefused, ExecutionContext, ExecutionRuntime
from agent.execution.settings import ExecutionSettings
from agent.strategies.base import PositionsSnapshot

ENABLED = ExecutionSettings(enabled=True)
USDT: dict[str, Any] = {"quote_asset": "USDT"}


def wall_clock() -> int:
    return int(time.time() * 1000)


@dataclass
class World:
    """One database and one exchange per network, shared by every runtime."""

    url: str
    db: AsyncEngine
    exchanges: dict[str, FakeExchange] = field(default_factory=dict)
    runtimes: list[ExecutionRuntime] = field(default_factory=list)

    def exchange(self, network: str) -> FakeExchange:
        if network not in self.exchanges:
            self.exchanges[network] = FakeExchange(
                uid=1001,
                balances={"USDT": Decimal(100), "BNB": Decimal(1)},
                clock=wall_clock,
            )
        return self.exchanges[network]

    def runtime(self, *, schema_ready: bool = True) -> ExecutionRuntime:
        runtime = ExecutionRuntime(
            db=self.db,
            db_url=self.url,
            schema_ready=schema_ready,
            trade_factory=lambda network: TradeClient(
                network,  # type: ignore[arg-type]
                self.exchange(network).spot(),
                budget=RequestBudget(),
            ),
        )
        self.runtimes.append(runtime)
        return runtime


@asynccontextmanager
async def world() -> AsyncIterator[World]:
    async with scratch_database() as url:
        db = create_async_engine(url)
        await migrate(db)
        state = World(url=url, db=db)
        try:
            yield state
        finally:
            for runtime in state.runtimes:
                await runtime.shutdown()
            await db.dispose()


def run(scenario: Callable[[World], Awaitable[None]]) -> None:
    async def wrapper() -> None:
        async with world() as state:
            await scenario(state)

    asyncio.run(wrapper())


async def add(state: World, *rows: Any) -> None:
    runtime = ExecutionRuntime(db=state.db, db_url=state.url, schema_ready=True)
    async with runtime.session() as session:
        session.add_all(rows)
        await session.commit()


def position(account: str = "1001", network: str = "live") -> Position:
    return Position(
        id=uuid.uuid4(),
        account=account,
        network=network,
        symbol="BTCUSDT",
        strategy="tripple_screen",
        signal_id=uuid.uuid4(),
        status=PositionStatus.PROTECTED,
        exit_policy={},
        quantity=Decimal("0.001"),
    )


# --- single writer ------------------------------------------------------------


def test_a_second_runtime_on_the_same_database_does_not_lead() -> None:
    async def scenario(state: World) -> None:
        first, second = state.runtime(), state.runtime()
        await first.prepare_start(network="live", settings=ENABLED, params=USDT)
        await second.prepare_start(network="live", settings=ENABLED, params=USDT)
        assert first.may_mutate() and not second.may_mutate()
        first.open_admission("tripple_screen")
        second.open_admission("tripple_screen")
        assert first.admission_block() is None
        assert second.admission_block() == "no_execution_lock"
        assert "another process holds execution" in (second.message() or "")

    run(scenario)


def test_losing_the_lock_session_hands_over_after_reconcile() -> None:
    async def scenario(state: World) -> None:
        first, second = state.runtime(), state.runtime()
        seen: list[tuple[str, bool]] = []

        def hook(
            name: str, runtime: ExecutionRuntime
        ) -> Callable[[ExecutionContext], Awaitable[None]]:
            async def reconcile(context: ExecutionContext) -> None:
                # Mutations stay off while reconciliation runs.
                seen.append((name, runtime.may_mutate()))

            return reconcile

        first.on_leadership(hook("first", first))
        second.on_leadership(hook("second", second))
        await first.prepare_start(network="live", settings=ENABLED, params=USDT)
        await second.prepare_start(network="live", settings=ENABLED, params=USDT)
        assert first.lock is not None
        pid = await first.lock.backend_pid()
        async with state.db.connect() as admin:
            await admin.execute(text("select pg_terminate_backend(:pid)"), {"pid": pid})
            await admin.commit()

        await second.tick()
        assert second.may_mutate()
        await first.tick()
        assert not first.may_mutate()
        assert "lock" in first.alerts
        assert seen == [("first", False), ("second", False)]

    run(scenario)


# --- context -------------------------------------------------------------------


def test_live_position_refuses_a_testnet_start_and_survives_a_restart() -> None:
    async def scenario(state: World) -> None:
        await add(state, position())
        runtime = state.runtime()
        await runtime.boot()
        assert runtime.context == ExecutionContext("1001", "live")
        assert runtime.may_mutate() and runtime.open_positions == 1
        with pytest.raises(ContextRefused, match="on live"):
            await runtime.prepare_start(
                network="testnet", settings=ENABLED, params=USDT
            )
        assert runtime.context == ExecutionContext("1001", "live")
        await runtime.shutdown()

        restarted = state.runtime()
        await restarted.boot()  # the desired network may say testnet; the DB says live
        assert restarted.context == ExecutionContext("1001", "live")
        assert restarted.may_mutate()

    run(scenario)


def test_unknown_buy_without_a_position_restores_its_context() -> None:
    async def scenario(state: World) -> None:
        signal_id = uuid.uuid4()
        await add(
            state,
            SignalInbox(
                signal_id=signal_id,
                account="1001",
                network="testnet",
                strategy="tripple_screen",
                symbol="BTCUSDT",
                type="ENTRY",
                epoch_id="BTCUSDT:1",
                emitted_at=datetime.now(UTC),
                payload={},
            ),
            OrderAttempt(
                client_order_id="mta-0123456789abcdef01234567-e1",
                account="1001",
                network="testnet",
                symbol="BTCUSDT",
                signal_id=signal_id,
                role="e",
                sequence=1,
                request={},
                state=AttemptState.UNKNOWN,
            ),
        )
        runtime = state.runtime()
        await runtime.boot()
        assert runtime.context == ExecutionContext("1001", "testnet")
        with pytest.raises(ContextRefused):
            await runtime.prepare_start(network="live", settings=ENABLED, params=USDT)

    run(scenario)


def test_other_account_behind_the_credentials_is_refused() -> None:
    async def scenario(state: World) -> None:
        await add(state, position(account="999"))
        runtime = state.runtime()
        await runtime.boot()
        assert runtime.context is None and not runtime.may_mutate()
        assert "work belongs to 999" in runtime.alerts["restore"]
        with pytest.raises(ValueError, match="could not be restored"):
            await runtime.prepare_start(network="live", settings=ENABLED, params=USDT)

    run(scenario)


def test_switching_network_is_free_without_unfinished_work() -> None:
    async def scenario(state: World) -> None:
        runtime = state.runtime()
        await runtime.prepare_start(network="live", settings=ENABLED, params=USDT)
        await runtime.prepare_start(network="testnet", settings=ENABLED, params=USDT)
        assert runtime.context == ExecutionContext("1001", "testnet")
        other = state.runtime()
        await other.prepare_start(network="live", settings=ENABLED, params=USDT)
        assert other.may_mutate()  # the live lock was released

    run(scenario)


# --- admission ------------------------------------------------------------------


def test_admission_follows_settings_stop_and_pause() -> None:
    async def scenario(state: World) -> None:
        runtime = state.runtime()
        await runtime.prepare_start(
            network="live", settings=ExecutionSettings(), params=USDT
        )
        runtime.open_admission("tripple_screen")
        assert runtime.admission_block() == "execution_off"
        assert runtime.heartbeat()["mode"] == "off"
        await runtime.prepare_start(network="live", settings=ENABLED, params=USDT)
        assert runtime.admission_block() is None
        assert runtime.heartbeat()["mode"] == "enabled"
        runtime.pause()
        assert runtime.heartbeat()["mode"] == "paused(paused_by_owner)"
        runtime.resume()
        runtime.close_admission()
        assert runtime.admission_block() == "strategy_stopped"
        assert runtime.may_mutate()  # Stop closes entries only

    run(scenario)


def test_real_execution_is_usdt_only() -> None:
    async def scenario(state: World) -> None:
        runtime = state.runtime()
        with pytest.raises(ValueError, match="USDT only"):
            await runtime.prepare_start(
                network="live", settings=ENABLED, params={"quote_asset": "USDC"}
            )
        await runtime.prepare_start(
            network="live", settings=ExecutionSettings(), params={"quote_asset": "USDC"}
        )

    run(scenario)


def test_key_without_spot_trading_keeps_entries_closed() -> None:
    async def scenario(state: World) -> None:
        state.exchange("live").can_trade = False
        runtime = state.runtime()
        await runtime.prepare_start(network="live", settings=ENABLED, params=USDT)
        runtime.open_admission("tripple_screen")
        assert runtime.admission_block() == "key"
        assert "cannot trade spot" in runtime.alerts["key"]

    run(scenario)


def test_skewed_clock_closes_admission_and_low_bnb_warns() -> None:
    async def scenario(state: World) -> None:
        fake = state.exchange("live")
        fake._clock = lambda: wall_clock() + 10_000
        fake.free["BNB"] = Decimal(0)
        runtime = state.runtime()
        await runtime.prepare_start(network="live", settings=ENABLED, params=USDT)
        runtime.open_admission("tripple_screen")
        assert runtime.admission_block() == "clock"
        assert "bnb" in runtime.alerts
        assert runtime.heartbeat()["mode"] == "paused(clock)"

    run(scenario)


def test_without_a_migrated_database_real_execution_is_refused() -> None:
    async def scenario(state: World) -> None:
        runtime = state.runtime(schema_ready=False)
        await runtime.boot()
        assert "schema" in runtime.alerts
        with pytest.raises(ValueError, match="database is not ready"):
            await runtime.prepare_start(network="live", settings=ENABLED, params=USDT)
        await runtime.prepare_start(
            network="live", settings=ExecutionSettings(), params=USDT
        )

    run(scenario)


# --- the engine keeps execution alive ---------------------------------------------


def test_refused_start_leaves_the_current_run_and_stop_only_closes_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        engine, _redis, strategy, _streams = setup_engine(monkeypatch)
        runtime = Mock(
            prepare_start=AsyncMock(),
            positions_snapshot=AsyncMock(return_value=PositionsSnapshot()),
            open_admission=Mock(),
            close_admission=Mock(),
            heartbeat=Mock(return_value={"mode": "enabled"}),
            message=Mock(return_value=None),
            shutdown=AsyncMock(),
        )
        engine.execution = runtime
        await start(engine)
        runtime.open_admission.assert_called_once_with("test")
        runtime.prepare_start.side_effect = ContextRefused("open work on live")
        with pytest.raises(ValueError, match="open work on live"):
            await start(engine, network="live")
        assert engine.running and engine.network == "testnet"
        strategy.on_stop.assert_not_awaited()
        await engine.stop()
        runtime.close_admission.assert_called_once()
        runtime.shutdown.assert_not_awaited()
        await engine.shutdown()

    asyncio.run(scenario())


def test_invalid_execution_settings_refuse_the_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        engine, *_ = setup_engine(monkeypatch)
        with pytest.raises(ValueError):
            await start(engine, execution={"enabled": True, "max_slippage_pct": 50})
        with pytest.raises(ValueError):
            await start(engine, execution={"unknown": 1})
        assert not engine.running

    asyncio.run(scenario())
