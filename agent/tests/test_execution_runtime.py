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
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from test_engine_lifecycle import setup_engine, start

from agent.core.migrate import migrate
from agent.exchange.budget import RequestBudget
from agent.exchange.rest import ExchangeError
from agent.exchange.trade import TradeCallError, TradeClient
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
        first.settings = ENABLED
        await first.prepare_start(network="live", params=USDT)
        second.settings = ENABLED
        await second.prepare_start(network="live", params=USDT)
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
        first.settings = ENABLED
        await first.prepare_start(network="live", params=USDT)
        second.settings = ENABLED
        await second.prepare_start(network="live", params=USDT)
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


def _flaky_reconcile(failures: int) -> tuple[list[int], Any]:
    calls: list[int] = []

    async def reconcile(context: ExecutionContext) -> None:
        calls.append(1)
        if len(calls) <= failures:
            raise ExchangeError("ticker price timed out")

    return calls, reconcile


def test_failed_startup_reconcile_is_retried_while_the_lock_is_held() -> None:
    # Review 2026-09-30 (EXEC-05): a failing hook left the lock held and
    # reconciled=False, and later ticks only re-led when the lock was gone.
    async def scenario(state: World) -> None:
        runtime = state.runtime()
        calls, reconcile = _flaky_reconcile(failures=2)
        runtime.on_leadership(reconcile)
        runtime.settings = ENABLED
        await runtime.prepare_start(network="live", params=USDT)
        assert runtime.lock is not None and runtime.lock.held
        assert not runtime.may_mutate()
        assert "ticker price timed out" in runtime.alerts["reconcile"]
        await runtime.tick()
        assert not runtime.may_mutate() and len(calls) == 2
        await runtime.tick()
        assert runtime.may_mutate()
        assert "reconcile" not in runtime.alerts
        assert len(calls) == 3
        await runtime.tick()
        assert len(calls) == 3  # reconciled: not repeated every round

    run(scenario)


def test_boot_restores_and_starts_the_watchdog_despite_a_failing_reconcile() -> None:
    async def scenario(state: World) -> None:
        await add(state, position())
        runtime = state.runtime()
        # The restore leads and fails; the same round retries once and fails.
        calls, reconcile = _flaky_reconcile(failures=2)
        runtime.on_leadership(reconcile)
        await runtime.boot()
        assert runtime._watchdog is not None and not runtime._watchdog.done()
        assert runtime.context is not None and runtime.context.network == "live"
        assert not runtime.may_mutate() and len(calls) == 2
        await runtime.tick()
        assert runtime.may_mutate() and len(calls) == 3

    run(scenario)


def test_boot_starts_the_watchdog_even_if_the_first_round_raises() -> None:
    async def scenario(state: World) -> None:
        runtime = state.runtime()
        runtime.tick = AsyncMock(side_effect=ExchangeError("boom"))  # type: ignore[method-assign]
        await runtime.boot()
        assert runtime._watchdog is not None and not runtime._watchdog.done()

    run(scenario)


# --- context -------------------------------------------------------------------


def test_live_position_refuses_a_testnet_start_and_survives_a_restart() -> None:
    async def scenario(state: World) -> None:
        await add(state, position())
        runtime = state.runtime()
        await runtime.boot()
        assert runtime.context == ExecutionContext("1001", "live")
        assert runtime.may_mutate() and runtime.open_positions == 1
        runtime.settings = ENABLED
        with pytest.raises(ContextRefused, match="on live"):
            await runtime.prepare_start(network="testnet", params=USDT)
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
        runtime.settings = ENABLED
        with pytest.raises(ContextRefused):
            await runtime.prepare_start(network="live", params=USDT)

    run(scenario)


def test_other_account_behind_the_credentials_is_refused() -> None:
    async def scenario(state: World) -> None:
        await add(state, position(account="999"))
        runtime = state.runtime()
        await runtime.boot()
        assert runtime.context is None and not runtime.may_mutate()
        assert "work belongs to 999" in runtime.alerts["restore"]
        runtime.settings = ENABLED
        with pytest.raises(ValueError, match="could not be restored"):
            await runtime.prepare_start(network="live", params=USDT)

    run(scenario)


def test_switching_network_is_free_without_unfinished_work() -> None:
    async def scenario(state: World) -> None:
        runtime = state.runtime()
        runtime.settings = ENABLED
        await runtime.prepare_start(network="live", params=USDT)
        runtime.settings = ENABLED
        await runtime.prepare_start(network="testnet", params=USDT)
        assert runtime.context == ExecutionContext("1001", "testnet")
        other = state.runtime()
        other.settings = ENABLED
        await other.prepare_start(network="live", params=USDT)
        assert other.may_mutate()  # the live lock was released

    run(scenario)


# --- admission ------------------------------------------------------------------


def test_admission_follows_settings_stop_and_pause() -> None:
    async def scenario(state: World) -> None:
        runtime = state.runtime()
        await runtime.prepare_start(network="live", params=USDT)
        runtime.open_admission("tripple_screen")
        assert runtime.admission_block() == "execution_off"
        assert runtime.heartbeat()["mode"] == "off"
        runtime.configure(ENABLED)
        await runtime.tick()  # the lock and the reconciliation follow
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
        runtime.settings = ENABLED
        with pytest.raises(ValueError, match="USDT only"):
            await runtime.prepare_start(network="live", params={"quote_asset": "USDC"})
        runtime.settings = ExecutionSettings()
        await runtime.prepare_start(network="live", params={"quote_asset": "USDC"})

    run(scenario)


def test_key_without_spot_trading_keeps_entries_closed() -> None:
    async def scenario(state: World) -> None:
        state.exchange("live").can_trade = False
        runtime = state.runtime()
        runtime.settings = ENABLED
        await runtime.prepare_start(network="live", params=USDT)
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
        runtime.settings = ENABLED
        await runtime.prepare_start(network="live", params=USDT)
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
            runtime.configure(ENABLED)
        assert not runtime.settings.enabled
        runtime.settings = ENABLED  # as if set before the schema went away
        with pytest.raises(ValueError, match="database is not ready"):
            await runtime.prepare_start(network="live", params=USDT)
        runtime.settings = ExecutionSettings()
        await runtime.prepare_start(network="live", params=USDT)

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


# --- real trading is the owner's switch, not the Start's (ClickUp 869fgqxah) ------


def _counting_reconcile() -> tuple[list[ExecutionContext], Any]:
    seen: list[ExecutionContext] = []

    async def reconcile(context: ExecutionContext) -> None:
        seen.append(context)

    return seen, reconcile


def test_a_start_with_trading_off_identifies_the_account_but_does_not_lead() -> None:
    # 2026-10-13: a testnet Start timed out in the reconciliation of ~490
    # faucet balances although real trading was off.
    async def scenario(state: World) -> None:
        runtime = state.runtime()
        seen, reconcile = _counting_reconcile()
        runtime.on_leadership(reconcile)
        await runtime.prepare_start(network="testnet", params=USDT)
        assert runtime.context == ExecutionContext("1001", "testnet")
        assert runtime.lock is None and seen == []
        await runtime.tick()
        assert runtime.lock is None and seen == []
        assert runtime.heartbeat()["mode"] == "off"
        # Another process may lead the same context meanwhile.
        other = state.runtime()
        other.configure(ENABLED)
        await other.prepare_start(network="testnet", params=USDT)
        assert other.may_mutate()

    run(scenario)


def test_the_switch_takes_and_gives_back_the_lead_without_a_start() -> None:
    async def scenario(state: World) -> None:
        runtime = state.runtime()
        seen, reconcile = _counting_reconcile()
        runtime.on_leadership(reconcile)
        await runtime.prepare_start(network="live", params=USDT)
        runtime.open_admission("tripple_screen")
        runtime.configure(ENABLED)
        assert runtime.admission_block() == "no_execution_lock"
        await runtime.tick()
        assert runtime.may_mutate() and len(seen) == 1
        assert runtime.heartbeat()["mode"] == "enabled"
        runtime.alerts["divergence:balance:ETHUSDT"] = "0.1 ETH held without a position"
        runtime.configure(ExecutionSettings())
        assert runtime.admission_block() == "execution_off"
        await runtime.tick()
        assert runtime.lock is None and not runtime.may_mutate()
        assert runtime.context == ExecutionContext("1001", "live")
        assert "divergence:balance:ETHUSDT" not in runtime.alerts
        other = state.runtime()
        other.configure(ENABLED)
        await other.prepare_start(network="live", params=USDT)
        assert other.may_mutate()  # the lock was given back

    run(scenario)


def test_trading_off_keeps_leading_while_work_is_unfinished() -> None:
    async def scenario(state: World) -> None:
        await add(state, position())
        runtime = state.runtime()
        await runtime.boot()
        assert runtime.may_mutate() and not runtime.settings.enabled
        await runtime.tick()
        assert runtime.may_mutate()  # the open position is still accompanied
        async with runtime.session() as session:
            row = (await session.scalars(select(Position))).one()
            row.status = PositionStatus.CLOSED
            await session.commit()
        await runtime.tick()
        assert runtime.lock is None

    run(scenario)


def test_with_trading_off_an_unreachable_account_does_not_refuse_the_start() -> None:
    async def scenario(state: World) -> None:
        runtime = state.runtime()
        fake = state.exchange("testnet")
        fake.fault("get_account", "reject")
        await runtime.prepare_start(network="testnet", params=USDT)
        assert runtime.context is None
        assert "cannot identify the account on testnet" in runtime.alerts["context"]
        await runtime.tick()
        assert runtime.context == ExecutionContext("1001", "testnet")
        assert "context" not in runtime.alerts
        # With real trading on, the same failure refuses the Start.
        enabled = state.runtime()
        enabled.configure(ENABLED)
        fake.fault("get_account", "reject")
        with pytest.raises(TradeCallError):
            await enabled.prepare_start(network="testnet", params=USDT)

    run(scenario)


def test_configuring_changes_the_risk_version() -> None:
    async def scenario(state: World) -> None:
        runtime = state.runtime()
        before = runtime.risk_version
        runtime.configure(ExecutionSettings())
        assert runtime.risk_version == before  # the same settings: no change
        runtime.configure(ExecutionSettings(daily_losing_trades_limit=1))
        assert runtime.risk_version == before + 1

    run(scenario)
