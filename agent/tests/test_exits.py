"""EXEC-09c: time exit, guard and close_position — with or without the strategy."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from execution_world import Stack, World, run
from fake_exchange import SymbolRules
from sqlalchemy import select
from test_ledger import fill

from agent.common.commands import HANDLERS, AgentCommand, CommandContext
from agent.exchange.hub import StreamHub
from agent.execution.exits import (
    Exits,
    NoSuchPosition,
    SymbolNotTrading,
    WriteOffRefused,
)
from agent.execution.ledger import lock_position, losing_trades_today
from agent.execution.models import (
    ExecutionEvent,
    Fill,
    Position,
    PositionStatus,
    SymbolCooldown,
)
from agent.execution.monitor import MonitorFeed
from agent.execution.outcome import ExitMechanism, ExitReason
from agent.execution.protection import Step
from agent.execution.quotes import LastTrade
from agent.execution.reconcile import Reconciler
from agent.execution.worker import Operation


def feed(state: World) -> MonitorFeed:
    async def no_rest(*_: Any) -> list[list[Any]]:
        return []

    return MonitorFeed(StreamHub(), no_rest, clock=state.clock.now)


def outcome(position: Any) -> tuple[str, str]:
    assert position.outcome is not None
    return position.outcome["reason"], position.outcome["mechanism"]


def test_time_exit_after_the_hold_even_after_downtime() -> None:
    async def scenario(state: World) -> None:
        first = await state.stack()
        position_id = await state.protected(first)
        exits = Exits(first.worker, feed(state))
        assert await exits.check_time(position_id) is None
        await state.kill(first)  # the agent is down past the 8 hours
        state.clock.advance(8 * 3600 + 60)
        state.fake.set_price("BTCUSDT", Decimal(101))
        second = await state.stack()
        second.runtime.close_admission()  # strategy stopped: exits still run
        result = await Exits(second.worker, feed(state)).check_time(position_id)
        assert result is not None and result.step is Step.CLOSED
        assert outcome(await state.position(second, position_id)) == (
            "time",
            "market_time",
        )

    run(scenario)


async def limit_protected(state: World) -> tuple[Stack, uuid.UUID]:
    rules = SymbolRules(
        symbol="ETHUSDT",
        base="ETH",
        order_types=("LIMIT", "LIMIT_MAKER", "MARKET", "STOP_LOSS_LIMIT"),
    )
    state.fake.list_symbol(rules, Decimal(100))
    stack = await state.stack()
    return stack, await state.protected(stack, "ETHUSDT")


def test_guard_sells_a_skipped_stop_limit_after_the_grace() -> None:
    async def scenario(state: World) -> None:
        stack, position_id = await limit_protected(state)
        watch = feed(state)
        exits = Exits(stack.worker, watch)
        state.fake.set_price("ETHUSDT", Decimal(97))  # gapped through 98 and 97.51
        watch._trades["ETHUSDT"] = LastTrade(Decimal(97), state.clock.now())
        assert await exits.check_guard(position_id) is None  # grace starts
        state.clock.advance(6)
        watch._trades["ETHUSDT"] = LastTrade(Decimal(97), state.clock.now())
        result = await exits.check_guard(position_id)
        assert result is not None and result.step is Step.CLOSED
        assert outcome(await state.position(stack, position_id)) == ("stop", "guard")

    run(scenario)


def test_guard_never_acts_on_a_stale_price() -> None:
    async def scenario(state: World) -> None:
        stack, position_id = await limit_protected(state)
        watch = feed(state)
        exits = Exits(stack.worker, watch)
        watch._trades["ETHUSDT"] = LastTrade(Decimal(90), state.clock.now())
        state.clock.advance(120)  # no trades since
        assert await exits.check_guard(position_id) is None
        assert await exits.check_guard(position_id) is None
        assert "guard:ETHUSDT" in stack.runtime.alerts
        assert (
            await state.position(stack, position_id)
        ).status == PositionStatus.PROTECTED

    run(scenario)


def test_guard_is_off_for_market_stops() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.protected(stack)
        watch = feed(state)
        watch._trades["BTCUSDT"] = LastTrade(Decimal(90), state.clock.now())
        exits = Exits(stack.worker, watch)
        state.clock.advance(60)
        watch._trades["BTCUSDT"] = LastTrade(Decimal(90), state.clock.now())
        assert await exits.check_guard(position_id) is None

    run(scenario)


def test_close_during_a_reprice_takes_over_the_operation() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.protected(stack)
        state.fake.fault("delete_order_list", "timeout_before")
        waiting = await stack.worker.reprice(position_id, Decimal("99.5"))
        assert waiting.reason == "cancel outcome unknown"
        result = await Exits(stack.worker, feed(state)).close_symbol("btcusdt")
        assert result.step is Step.CLOSED
        assert outcome(await state.position(stack, position_id)) == (
            "manual",
            "manual_close",
        )
        assert [
            o.type
            for o in state.fake.orders_of("BTCUSDT", "SELL")
            if o.type == "MARKET"
        ] == ["MARKET"]

    run(scenario)


def test_close_position_command_reports_the_outcome() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        await state.protected(stack)
        exits = Exits(stack.worker, feed(state))
        stack.runtime.close_position = exits.close_symbol
        engine = Mock(execution=stack.runtime)
        redis = Mock(pipeline=Mock())
        reports: list[dict[str, Any]] = []

        async def run_command(payload: dict[str, Any]) -> None:
            ctx = CommandContext(
                engine,
                redis,
                AgentCommand("c", "close_position", payload, state.clock.now()),
            )
            ctx.report = AsyncMock(side_effect=lambda **kw: reports.append(kw))  # type: ignore[method-assign]
            await HANDLERS["close_position"](ctx, payload)

        await run_command({"symbol": "ETHUSDT"})
        assert (
            reports[-1]["ok"] is False and "no open position" in reports[-1]["message"]
        )
        await run_command({"symbol": "btcusdt"})
        assert reports[-1]["ok"] is True
        assert reports[-1]["data"]["step"] == "closed"
        with pytest.raises(NoSuchPosition):
            await exits.close_symbol("BTCUSDT")  # nothing left to close

    run(scenario)


def test_a_late_sell_response_dates_the_close_by_the_exchange() -> None:
    # Review 2026-10-03 (EXEC-15): the losing SELL executed 23:59:58 UTC, its
    # response was read 00:00:03; the fill, close and loss day followed the
    # reader, and the trade list never corrected them.
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.protected(stack)
        executed = datetime(2026, 10, 3, 23, 59, 58, tzinfo=UTC)
        state.clock.advance((executed - state.clock.now()).total_seconds())
        state.fake.set_price("BTCUSDT", Decimal("99.5"))  # a loss, above the stop
        trade = stack.runtime.trade
        assert trade is not None
        real = trade.new_order

        async def late(**kwargs: Any) -> Any:
            result = await real(**kwargs)
            state.clock.advance(5)  # the answer arrives after midnight
            return result

        trade.new_order = late  # type: ignore[method-assign]
        result = await stack.worker.close(
            position_id, ExitReason.MANUAL, ExitMechanism.MANUAL_CLOSE
        )
        assert result.step is Step.CLOSED
        position = await state.position(stack, position_id)
        assert position.closed_at is not None
        assert position.closed_at.date() == executed.date()
        assert abs((position.closed_at - executed).total_seconds()) < 1
        async with stack.runtime.session() as session:
            assert await losing_trades_today(session, "1001", "live", executed) == 1
            assert (
                await losing_trades_today(
                    session, "1001", "live", executed + timedelta(seconds=5)
                )
                == 0
            )

    run(scenario)


# --- a symbol that does not trade keeps its list (ClickUp 869fjdy0k) ---------------------


def halt(state: World, symbol: str = "BTCUSDT", status: str = "HALT") -> None:
    """The exchange changes the symbol's status; the agent's cache still says
    what it read when the position was protected."""
    state.fake.rules[symbol].status = status


def resume(state: World, stack: Stack, symbol: str = "BTCUSDT") -> None:
    """Trading resumes and the halt the agent cached has expired."""
    state.fake.rules[symbol].status = "TRADING"
    stack.orders.filters.invalidate("live", symbol)


async def kept(state: World, stack: Stack, position_id: uuid.UUID) -> None:
    position = await state.position(stack, position_id)
    assert position.status == PositionStatus.PROTECTED
    assert position.operation is None and position.unprotected_since is None
    assert state.fake.calls_to("delete_order_list") == 0
    assert [ol.status for ol in state.fake.lists.values()] == ["EXECUTING"]


def test_time_exit_waits_for_trading_and_keeps_the_list() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.protected(stack)
        exits = Exits(stack.worker, feed(state))
        halt(state)
        state.clock.advance(8 * 3600 + 60)
        result = await exits.check_time(position_id)
        assert result is not None and result.step is Step.WAITING
        assert result.reason is not None and "HALT" in result.reason
        assert "halted:BTCUSDT" in stack.runtime.alerts
        assert stack.runtime.admission_block() is None  # other symbols still enter
        state.clock.advance(60)  # past the 30 s deadline: nothing is overdue
        assert (await exits.check_time(position_id)).step is Step.WAITING  # type: ignore[union-attr]
        await kept(state, stack, position_id)
        assert not [k for k in stack.runtime.alerts if k.startswith("overdue:")]

        resume(state, stack)
        state.fake.set_price("BTCUSDT", Decimal(101))
        result = await exits.check_time(position_id)
        assert result is not None and result.step is Step.CLOSED
        assert outcome(await state.position(stack, position_id)) == (
            "time",
            "market_time",
        )
        assert "halted:BTCUSDT" not in stack.runtime.alerts

    run(scenario)


def test_trailing_keeps_the_list_of_a_symbol_in_break() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.protected(stack)
        halt(state, status="BREAK")
        result = await stack.worker.reprice(position_id, Decimal("99.5"))
        assert result.step is Step.WAITING
        await kept(state, stack, position_id)
        resume(state, stack)
        result = await stack.worker.reprice(position_id, Decimal("99.5"))
        assert result.step is Step.PROTECTED
        position = await state.position(stack, position_id)
        assert position.exchange_stop == Decimal("99.5")

    run(scenario)


def test_an_operation_recorded_before_the_halt_is_withdrawn() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.protected(stack)
        # Recorded, then the process died before the cancel went out.
        await stack.worker._record(
            position_id,
            Operation.EXIT,
            {"reason": "time", "mechanism": "market_time"},
        )
        await state.kill(stack)
        second = await state.stack()
        halt(state)
        state.clock.advance(60)
        result = await second.worker.resume(position_id)
        assert result.step is Step.WAITING
        await kept(state, second, position_id)
        async with second.runtime.session() as session:
            kinds = (
                await session.scalars(
                    select(ExecutionEvent.type).where(
                        ExecutionEvent.position_id == position_id
                    )
                )
            ).all()
        assert "operation_withdrawn" in kinds

    run(scenario)


def test_a_cancel_already_sent_is_not_withdrawn() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.protected(stack)
        state.fake.fault("delete_order_list", "timeout_after")
        waiting = await stack.worker.close(
            position_id, ExitReason.MANUAL, ExitMechanism.MANUAL_CLOSE
        )
        assert waiting.reason == "cancel outcome unknown"
        halt(state)
        await stack.worker.resume(position_id)
        position = await state.position(stack, position_id)
        assert position.operation == "exit"  # the list may be gone: no way back
        assert position.unprotected_since is not None

    run(scenario)


def test_close_position_is_refused_while_the_symbol_is_halted() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.protected(stack)
        exits = Exits(stack.worker, feed(state))
        halt(state)
        with pytest.raises(SymbolNotTrading, match="HALT"):
            await exits.close_symbol("BTCUSDT")
        await kept(state, stack, position_id)

    run(scenario)


def test_a_cached_trading_status_does_not_cancel_the_list() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.protected(stack)
        exits = Exits(stack.worker, feed(state))
        await stack.orders.filters.get("live", "BTCUSDT")  # TRADING, cached
        halt(state)
        with pytest.raises(SymbolNotTrading, match="HALT"):
            await exits.close_symbol("BTCUSDT")
        state.clock.advance(8 * 3600 + 60)
        assert (await exits.check_time(position_id)).step is Step.WAITING  # type: ignore[union-attr]
        result = await stack.worker.reprice(position_id, Decimal("99.5"))
        assert result.step is Step.WAITING
        await kept(state, stack, position_id)

    run(scenario)


def test_an_unreadable_status_keeps_the_list() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.protected(stack)
        exits = Exits(stack.worker, feed(state))
        halt(state)
        state.fake.fault("exchange_info", "reject", code=-1000, times=100)
        with pytest.raises(SymbolNotTrading, match="unknown"):
            await exits.close_symbol("BTCUSDT")
        state.clock.advance(8 * 3600 + 60)
        result = await exits.check_time(position_id)
        assert result is not None and result.step is Step.WAITING
        assert result.reason == "BTCUSDT status unknown: list kept"
        await kept(state, stack, position_id)
        # Visible in the heartbeat (ClickUp 869fjf3xq), entries stay open.
        assert "status_unknown:BTCUSDT" in stack.runtime.alerts
        assert "halted:BTCUSDT" not in stack.runtime.alerts
        assert stack.runtime.admission_block() is None

        state.fake.faults.pop("exchange_info")
        resume(state, stack)
        state.fake.set_price("BTCUSDT", Decimal(101))
        result = await exits.check_time(position_id)
        assert result is not None and result.step is Step.CLOSED
        assert "status_unknown:BTCUSDT" not in stack.runtime.alerts

    run(scenario)


def test_status_unknown_clears_on_a_halt_read_and_on_close() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.protected(stack)
        halt(state)
        state.fake.fault("exchange_info", "reject", code=-1000, times=100)
        result = await stack.worker.reprice(position_id, Decimal("99.5"))
        assert result.reason == "BTCUSDT status unknown: list kept"
        assert "status_unknown:BTCUSDT" in stack.runtime.alerts
        state.fake.faults.pop("exchange_info")
        result = await stack.worker.reprice(position_id, Decimal("99.5"))
        assert result.reason == "BTCUSDT is HALT: list kept"
        assert "status_unknown:BTCUSDT" not in stack.runtime.alerts
        assert "halted:BTCUSDT" in stack.runtime.alerts

        # The position closes while the status is unreadable (here: as dust).
        stack.runtime.alerts["status_unknown:BTCUSDT"] = "status of BTCUSDT unknown"
        await stack.worker.protector._close_dust(position_id)
        assert not [k for k in stack.runtime.alerts if k.endswith(":BTCUSDT")]

    run(scenario)


def test_a_known_halt_is_not_read_again_every_round() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.protected(stack)
        halt(state)
        result = await stack.worker.reprice(position_id, Decimal("99.5"))
        assert result.step is Step.WAITING
        reads = state.fake.calls_to("exchange_info")
        result = await stack.worker.reprice(position_id, Decimal("99.5"))
        assert result.step is Step.WAITING
        assert state.fake.calls_to("exchange_info") == reads  # the cached halt

    run(scenario)


def test_a_cached_halt_expires_without_an_invalidation() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.protected(stack)
        # The cache ages on the test's clock: nothing invalidates the halt.
        stack.orders.filters._clock = lambda: state.clock.now().timestamp()
        halt(state)
        result = await stack.worker.reprice(position_id, Decimal("99.5"))
        assert result.step is Step.WAITING
        reads = state.fake.calls_to("exchange_info")
        state.fake.rules["BTCUSDT"].status = "TRADING"
        state.clock.advance(59)
        result = await stack.worker.reprice(position_id, Decimal("99.5"))
        assert result.step is Step.WAITING
        assert state.fake.calls_to("exchange_info") == reads
        state.clock.advance(2)
        state.fake.set_price("BTCUSDT", Decimal(100))
        result = await stack.worker.reprice(position_id, Decimal("99.5"))
        assert result.step is Step.PROTECTED
        assert "halted:BTCUSDT" not in stack.runtime.alerts

    run(scenario)


def test_the_guard_keeps_the_list_of_a_halted_symbol() -> None:
    async def scenario(state: World) -> None:
        stack, position_id = await limit_protected(state)
        watch = feed(state)
        exits = Exits(stack.worker, watch)
        halt(state, "ETHUSDT")
        watch._trades["ETHUSDT"] = LastTrade(Decimal(97), state.clock.now())
        assert await exits.check_guard(position_id) is None  # grace starts
        state.clock.advance(6)
        watch._trades["ETHUSDT"] = LastTrade(Decimal(97), state.clock.now())
        result = await exits.check_guard(position_id)
        assert result is not None and result.step is Step.WAITING
        await kept(state, stack, position_id)

    run(scenario)


def test_a_restart_with_an_unknown_status_withdraws_the_unsent_cancel() -> None:
    async def scenario(state: World) -> None:
        first = await state.stack()
        position_id = await state.protected(first)
        # Recorded, then the process died before the cancel went out.
        await first.worker._record(
            position_id, Operation.EXIT, {"reason": "time", "mechanism": "market_time"}
        )
        await state.kill(first)
        second = await state.stack()
        state.fake.fault("exchange_info", "reject", code=-1000, times=100)
        result = await second.worker.resume(position_id)
        assert result.step is Step.WAITING
        assert "status unknown" in (result.reason or "")
        await kept(state, second, position_id)
        assert not [k for k in second.runtime.alerts if k.startswith("overdue:")]
        state.fake.faults.pop("exchange_info")
        result = await Exits(second.worker, feed(state)).close_symbol("BTCUSDT")
        assert result.step is Step.CLOSED

    run(scenario)


# --- write-off of a delisted symbol's position (ClickUp 869fjdy10) -----------------------


def delist(state: World, stack: Stack, symbol: str = "BTCUSDT") -> None:
    state.fake.delist(symbol)
    stack.orders.filters.invalidate("live", symbol)


def test_write_off_closes_a_delisted_position_without_an_order() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.protected(stack)
        exits = Exits(stack.worker, feed(state))
        delist(state, stack)
        stack.runtime.alerts["overdue:BTCUSDT"] = "BTCUSDT unprotected"
        sent = state.fake.calls_to("new_order"), state.fake.calls_to("order_list_oco")
        result = await exits.write_off(
            "btcusdt", confirm="BTCUSDT", proceeds=Decimal("12.5")
        )
        assert result.step is Step.CLOSED
        assert (
            state.fake.calls_to("new_order"),
            state.fake.calls_to("order_list_oco"),
        ) == sent
        position = await state.position(stack, position_id)
        assert outcome(position) == ("delisted", "write_off")
        assert position.realized_pnl == (
            Decimal("12.5") - position.bought_quote - position.commission_quote
        )
        assert not [
            k for k in stack.runtime.alerts if k.endswith(":BTCUSDT")
        ]  # overdue and halted gone
        async with stack.runtime.session() as session:
            kinds = (
                await session.scalars(
                    select(ExecutionEvent.type).where(
                        ExecutionEvent.position_id == position_id
                    )
                )
            ).all()
        assert "written_off" in kinds
        with pytest.raises(NoSuchPosition):
            await exits.write_off("BTCUSDT", confirm="BTCUSDT")

    run(scenario)


def test_write_off_is_refused_while_the_symbol_trades() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.protected(stack)
        exits = Exits(stack.worker, feed(state))
        with pytest.raises(WriteOffRefused, match="trades"):
            await exits.write_off("BTCUSDT", confirm="BTCUSDT")
        delist(state, stack)
        with pytest.raises(WriteOffRefused, match="confirm"):
            await exits.write_off("BTCUSDT", confirm="ETHUSDT")
        with pytest.raises(WriteOffRefused, match="negative"):
            await exits.write_off("BTCUSDT", confirm="BTCUSDT", proceeds=Decimal(-1))
        assert (
            await state.position(stack, position_id)
        ).status != PositionStatus.CLOSED

    run(scenario)


def test_write_off_is_refused_while_the_list_is_live() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.protected(stack)
        exits = Exits(stack.worker, feed(state))
        halt(state, status="BREAK")  # not trading, but the OCO still stands
        with pytest.raises(WriteOffRefused, match="open order"):
            await exits.write_off("BTCUSDT", confirm="BTCUSDT")
        assert (
            await state.position(stack, position_id)
        ).status == PositionStatus.PROTECTED

    run(scenario)


def test_write_off_waits_for_an_unproven_order() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.protected(stack)
        exits = Exits(stack.worker, feed(state))
        state.fake.fault("new_order", "timeout_after")
        waiting = await stack.worker.close(
            position_id, ExitReason.MANUAL, ExitMechanism.MANUAL_CLOSE
        )
        assert waiting.reason == "exit outcome unknown"
        delist(state, stack)
        with pytest.raises(WriteOffRefused, match="unproven"):
            await exits.write_off("BTCUSDT", confirm="BTCUSDT")

    run(scenario)


def test_write_off_position_command_reports_the_outcome() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        await state.protected(stack)
        exits = Exits(stack.worker, feed(state))
        stack.runtime.write_off_position = exits.write_off
        engine = Mock(execution=stack.runtime)
        reports: list[dict[str, Any]] = []

        async def run_command(payload: dict[str, Any]) -> None:
            ctx = CommandContext(
                engine,
                Mock(pipeline=Mock()),
                AgentCommand("c", "write_off_position", payload, state.clock.now()),
            )
            ctx.report = AsyncMock(side_effect=lambda **kw: reports.append(kw))  # type: ignore[method-assign]
            await HANDLERS["write_off_position"](ctx, payload)

        await run_command({"symbol": "BTCUSDT", "confirm": "BTCUSDT"})
        assert reports[-1]["ok"] is False and "trades" in reports[-1]["message"]
        delist(state, stack)
        await run_command(
            {"symbol": "BTCUSDT", "confirm": "BTCUSDT", "proceeds_quote": "x"}
        )
        assert reports[-1]["ok"] is False and "number" in reports[-1]["message"]
        await run_command(
            {"symbol": "btcusdt", "confirm": "BTCUSDT", "proceeds_quote": "3.25"}
        )
        assert reports[-1]["ok"] is True
        assert reports[-1]["data"] == {
            "symbol": "BTCUSDT",
            "step": "closed",
            "proceeds_quote": "3.25",
        }

    run(scenario)


def test_write_off_reads_the_status_anew() -> None:
    # Review of #37: a halt cached before trading resumed let the write-off
    # through with zero proceeds.
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.protected(stack)
        exits = Exits(stack.worker, feed(state))
        halt(state)
        result = await stack.worker.reprice(position_id, Decimal("99.5"))
        assert result.step is Step.WAITING  # the halt is cached and alerted
        state.fake.rules["BTCUSDT"].status = "TRADING"  # the cache is not told
        with pytest.raises(WriteOffRefused, match="trades"):
            await exits.write_off("BTCUSDT", confirm="BTCUSDT")
        state.fake.fault("exchange_info", "reject", code=-1000, times=100)
        with pytest.raises(WriteOffRefused, match="unknown"):
            await exits.write_off("BTCUSDT", confirm="BTCUSDT")
        assert (
            await state.position(stack, position_id)
        ).status == PositionStatus.PROTECTED

    run(scenario)


def test_write_off_closes_on_its_own_day_after_an_old_partial_sell() -> None:
    # Review of #37: the close, the loss day and the cooldown followed the
    # last SELL, two days before the write-off.
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.protected(stack)
        exits = Exits(stack.worker, feed(state))
        sold_at = state.clock.now()
        async with stack.runtime.session() as session, session.begin():
            position = await session.get(Position, position_id)
            assert position is not None
            session.add(
                Fill(
                    account=position.account,
                    network=position.network,
                    symbol=position.symbol,
                    trade_id=9_000_001,
                    order_id=9_000_001,
                    position_id=position_id,
                    side="SELL",
                    price=Decimal(110),
                    quantity=Decimal("0.01"),
                    quote_quantity=Decimal("1.1"),
                    commission=Decimal(0),
                    commission_asset="USDT",
                    commission_quote=Decimal(0),
                    is_maker=True,
                    trade_time=sold_at,
                    source="my_trades",
                )
            )
            position.sold_qty += Decimal("0.01")
            position.sold_quote += Decimal("1.1")
            position.quantity -= Decimal("0.01")
        state.clock.advance(2 * 86400)
        delist(state, stack)
        result = await exits.write_off("BTCUSDT", confirm="BTCUSDT")
        assert result.step is Step.CLOSED
        written_off = state.clock.now()
        position = await state.position(stack, position_id)
        assert position.realized_pnl is not None and position.realized_pnl < 0
        assert position.closed_at is not None
        assert abs((position.closed_at - written_off).total_seconds()) < 1
        async with stack.runtime.session() as session:
            assert await losing_trades_today(session, "1001", "live", written_off) == 1
            assert await losing_trades_today(session, "1001", "live", sold_at) == 0
            cooldown = await session.scalar(
                select(SymbolCooldown.cooldown_until).where(
                    SymbolCooldown.symbol == "BTCUSDT"
                )
            )
        assert cooldown is not None and cooldown > written_off

        # A late reconcile retimes the position: it keeps the write-off's day.
        async with stack.runtime.session() as session, session.begin():
            locked = await session.get(Position, position_id, with_for_update=True)
            assert locked is not None
            await stack.worker.protector.finalizer.retime(session, locked)
        position = await state.position(stack, position_id)
        assert position.closed_at is not None
        assert abs((position.closed_at - written_off).total_seconds()) < 1
        async with stack.runtime.session() as session:
            assert await losing_trades_today(session, "1001", "live", written_off) == 1
            assert await losing_trades_today(session, "1001", "live", sold_at) == 0

    run(scenario)


def test_a_sell_after_the_write_off_moves_the_close_and_revalue_keeps_it() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.protected(stack)
        async with stack.runtime.session() as session, session.begin():
            position = await lock_position(session, position_id)
            await stack.applier.apply(
                session,
                position,
                [
                    fill(
                        9000,
                        order_id=9000,
                        side="SELL",
                        qty="0.001",
                        price="100",
                        commission="0",
                        at=state.clock.now(),
                    )
                ],
            )
        state.clock.advance(2 * 86400)
        delist(state, stack)
        await Exits(stack.worker, feed(state)).write_off("BTCUSDT", confirm="BTCUSDT")
        written_off = (await state.position(stack, position_id)).closed_at
        assert written_off is not None
        # The exchange reports the SELL a day after the write-off.
        later = written_off + timedelta(days=1)
        async with stack.runtime.session() as session, session.begin():
            position = await lock_position(session, position_id)
            sell = await session.scalar(
                select(Fill).where(Fill.position_id == position_id, Fill.side == "SELL")
            )
            assert sell is not None
            sell.trade_time = later
            await stack.finalizer.retime(session, position)
            await stack.finalizer.revalue(session, position)
        position = await state.position(stack, position_id)
        assert position.closed_at == later
        assert position.outcome is not None
        assert datetime.fromisoformat(position.outcome["closed_at"]) == later
        async with stack.runtime.session() as session:
            assert await losing_trades_today(session, "1001", "live", written_off) == 0
            assert await losing_trades_today(session, "1001", "live", later) == 1
            cooldown = await session.scalar(
                select(SymbolCooldown.cooldown_until).where(
                    SymbolCooldown.position_id == position_id
                )
            )
        assert cooldown is not None and cooldown > later

    run(scenario)


def test_a_slow_status_read_leaves_no_alert_on_a_position_closed_meanwhile() -> None:
    # Review of #41: close_position read the status outside the position's
    # lock; the TP filled and the reconciler closed the position while the
    # read hung, then the failed read set status_unknown for a closed one.
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.protected(stack)
        exits = Exits(stack.worker, feed(state))
        position = await state.position(stack, position_id)
        assert position.protection_list_id is not None
        assert position.take_profit is not None
        filters = stack.orders.filters
        real_get = filters.get
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow_get(*args: Any, **kwargs: Any) -> Any:
            entered.set()
            await release.wait()
            raise RuntimeError("exchangeInfo timed out")

        filters.get = slow_get  # type: ignore[method-assign]
        command = asyncio.create_task(exits.close_symbol("BTCUSDT"))
        await entered.wait()
        state.fake.fill_leg(
            position.protection_list_id.replace("-pl", "-pt"),
            position.quantity,
            position.take_profit,
        )
        reconcile = asyncio.create_task(Reconciler(stack.worker).run())
        await asyncio.sleep(0.2)  # the reconciler waits for the position's lock
        release.set()
        with pytest.raises(SymbolNotTrading, match="unknown"):
            await command
        filters.get = real_get  # type: ignore[method-assign]
        await reconcile
        position = await state.position(stack, position_id)
        assert position.status == PositionStatus.CLOSED
        assert "status_unknown:BTCUSDT" not in stack.runtime.alerts

    run(scenario)


def test_an_exit_of_a_position_closed_meanwhile_reads_no_status() -> None:
    # Re-review of #41: the reconciler closed the position between
    # close_position's check and the worker's lock; the worker then read the
    # status of the closed position and a failed read left status_unknown.
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.protected(stack)
        position = await state.position(stack, position_id)
        assert position.protection_list_id is not None
        assert position.take_profit is not None
        state.fake.fill_leg(
            position.protection_list_id.replace("-pl", "-pt"),
            position.quantity,
            position.take_profit,
        )
        await Reconciler(stack.worker).run()
        assert (await state.position(stack, position_id)).status == (
            PositionStatus.CLOSED
        )
        reads = state.fake.calls_to("exchange_info")
        state.fake.fault("exchange_info", "reject", code=-1000, times=100)
        result = await stack.worker.close(
            position_id, ExitReason.MANUAL, ExitMechanism.MANUAL_CLOSE
        )
        assert result.step is Step.CLOSED
        assert state.fake.calls_to("exchange_info") == reads
        assert "status_unknown:BTCUSDT" not in stack.runtime.alerts

    run(scenario)
