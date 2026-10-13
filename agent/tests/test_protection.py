"""EXEC-08c: initial OCO, the 30 s deadline, protection_overdue, emergency exit."""

from __future__ import annotations

import asyncio
from decimal import Decimal

from execution_world import World, run
from fake_exchange import SymbolRules

from agent.execution.models import AttemptState, PositionStatus
from agent.execution.outcome import ExitMechanism, ExitReason
from agent.execution.protection import Step


def sells(state: World, symbol: str = "BTCUSDT") -> list[str]:
    return [o.type for o in state.fake.orders_of(symbol, "SELL") if o.order_list_id < 0]


def test_fill_is_protected_by_an_oco_computed_from_the_ledger() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.filled(stack)
        result = await stack.protector.protect(position_id)
        assert result.step is Step.PROTECTED
        position = await state.position(stack, position_id)
        assert position.status == PositionStatus.PROTECTED
        assert position.exchange_stop == Decimal(98)  # swing below fill × 0.99
        assert position.take_profit == Decimal(110)
        assert position.unprotected_since is None
        [order_list] = state.fake.lists.values()
        above, below = order_list.orders
        assert (above.type, above.price) == ("LIMIT_MAKER", Decimal(110))
        assert (below.type, below.stop_price) == ("STOP_LOSS", Decimal(98))
        assert above.quantity == position.quantity
        assert state.fake.balance("BTC")[1] == position.quantity  # locked by the list

    run(scenario)


def test_without_market_stops_the_stop_limit_fallback_is_used() -> None:
    async def scenario(state: World) -> None:
        rules = SymbolRules(
            symbol="ETHUSDT",
            base="ETH",
            order_types=("LIMIT", "LIMIT_MAKER", "MARKET", "STOP_LOSS_LIMIT"),
        )
        state.fake.list_symbol(rules, Decimal(100))
        stack = await state.stack()
        position_id = await state.filled(stack, "ETHUSDT")
        assert (await stack.protector.protect(position_id)).step is Step.PROTECTED
        [order_list] = state.fake.lists.values()
        below = order_list.orders[1]
        assert below.type == "STOP_LOSS_LIMIT" and below.price == Decimal("97.51")

    run(scenario)


def test_deterministic_refusal_goes_to_an_emergency_exit() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.filled(stack)
        state.fake.fault("order_list_oco", "reject", code=-1013)
        result = await stack.protector.protect(position_id)
        assert result.step is Step.CLOSED
        position = await state.position(stack, position_id)
        assert position.status == PositionStatus.CLOSED and position.outcome is not None
        assert position.outcome["reason"] == "emergency"
        assert position.outcome["mechanism"] == "market_unprotected"
        assert sells(state) == ["MARKET"]

    run(scenario)


def test_price_through_the_stop_after_the_fill_sells_at_market() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.filled(stack)
        state.fake.set_price("BTCUSDT", Decimal(97))
        result = await stack.protector.protect(position_id)
        assert result.step is Step.CLOSED
        position = await state.position(stack, position_id)
        assert position.outcome is not None
        assert (position.outcome["reason"], position.outcome["mechanism"]) == (
            "stop",
            "market_gap",
        )
        assert state.fake.lists == {}

    run(scenario)


def test_deadline_does_not_wait_for_a_slow_request_and_late_ack_protects() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.filled(stack)
        entered, release = state.fake.gate("order_list_oco")
        task = asyncio.create_task(stack.protector.protect(position_id))
        await asyncio.to_thread(entered.wait, 5)
        state.clock.advance(31)  # the request takes longer than the deadline
        assert await stack.protector.check_deadline(position_id)
        position = await state.position(stack, position_id)
        assert position.status == PositionStatus.PROTECTION_OVERDUE
        assert "overdue:BTCUSDT" in stack.runtime.alerts
        release.set()
        result = await task
        assert result.step is Step.PROTECTED  # the late ACK is a real protection
        assert "overdue:BTCUSDT" not in stack.runtime.alerts
        assert sells(state) == []

    run(scenario)


def test_unknown_oco_blocks_the_emergency_sale_until_proven_absent() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.filled(stack)
        state.fake.fault("order_list_oco", "timeout_before")  # lost, never placed
        first = await stack.protector.protect(position_id)
        assert first.step is Step.WAITING and first.reason == "OCO outcome unknown"
        state.clock.advance(31)
        second = await stack.protector.protect(position_id)
        assert second.step is Step.WAITING
        position = await state.position(stack, position_id)
        assert position.status == PositionStatus.PROTECTION_OVERDUE
        assert sells(state) == []  # it might still be protecting; no blind SELL
        state.clock.advance(40)  # recvWindow + margin passed: provably absent
        third = await stack.protector.protect(position_id)
        assert third.step is Step.CLOSED
        assert sells(state) == ["MARKET"]

    run(scenario)


def test_unknown_oco_that_was_placed_protects_even_past_the_deadline() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.filled(stack)
        state.fake.fault("order_list_oco", "timeout_after")  # placed, answer lost
        await stack.protector.protect(position_id)
        state.clock.advance(31)
        await stack.protector.check_deadline(position_id)
        result = await stack.protector.protect(position_id)
        assert result.step is Step.PROTECTED
        assert sells(state) == [] and len(state.fake.lists) == 1

    run(scenario)


def test_restart_keeps_the_original_deadline() -> None:
    async def scenario(state: World) -> None:
        first = await state.stack()
        position_id = await state.filled(first)
        since = (await state.position(first, position_id)).unprotected_since
        await state.kill(first)
        state.clock.advance(20)
        second = await state.stack()
        assert await second.orders.prove_entry(position_id)  # repeated reconcile
        assert (await state.position(second, position_id)).unprotected_since == since
        state.clock.advance(11)
        assert await second.protector.check_deadline(position_id)

    run(scenario)


def test_unknown_emergency_sale_is_resolved_not_repeated() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.filled(stack)
        state.fake.fault("order_list_oco", "reject", code=-1013)
        state.fake.fault("new_order", "timeout_after")  # the SELL goes through
        result = await stack.protector.protect(position_id)
        assert result.step is Step.WAITING and result.reason == "exit outcome unknown"
        assert sells(state) == ["MARKET"]
        closed = await stack.protector.exit_market(
            position_id, ExitReason.EMERGENCY, ExitMechanism.MARKET_UNPROTECTED
        )
        assert closed.step is Step.CLOSED
        assert sells(state) == ["MARKET"]  # found, not sold again
        attempts = await state.attempts(
            stack, (await state.position(stack, position_id)).signal_id
        )
        assert [a.state for a in attempts if a.role == "x"] == [AttemptState.RESOLVED]

    run(scenario)
