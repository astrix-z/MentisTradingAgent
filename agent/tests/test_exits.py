"""EXEC-09c: time exit, guard and close_position — with or without the strategy."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from execution_world import Stack, World, run
from fake_exchange import SymbolRules
from sqlalchemy import select

from agent.common.commands import HANDLERS, AgentCommand, CommandContext
from agent.exchange.hub import StreamHub
from agent.execution.exits import (
    Exits,
    NoSuchPosition,
    SymbolNotTrading,
    WriteOffRefused,
)
from agent.execution.ledger import losing_trades_today
from agent.execution.models import ExecutionEvent, PositionStatus
from agent.execution.monitor import MonitorFeed
from agent.execution.outcome import ExitMechanism, ExitReason
from agent.execution.protection import Step
from agent.execution.quotes import LastTrade
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


def halt(state: World, stack: Stack, status: str, symbol: str = "BTCUSDT") -> None:
    """The exchange changes the symbol's status; the agent's rules are re-read."""
    state.fake.rules[symbol].status = status
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
        halt(state, stack, "HALT")
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

        halt(state, stack, "TRADING")
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
        halt(state, stack, "BREAK")
        result = await stack.worker.reprice(position_id, Decimal("99.5"))
        assert result.step is Step.WAITING
        await kept(state, stack, position_id)
        halt(state, stack, "TRADING")
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
        halt(state, second, "HALT")
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
        halt(state, stack, "HALT")
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
        halt(state, stack, "HALT")
        with pytest.raises(SymbolNotTrading, match="HALT"):
            await exits.close_symbol("BTCUSDT")
        await kept(state, stack, position_id)

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
        halt(state, stack, "BREAK")  # not trading, but the OCO still stands
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
