"""EXEC-09a: the operation protocol — cancel, barrier, replace or exit."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from execution_world import World, run

from agent.execution.models import PositionStatus
from agent.execution.outcome import ExitMechanism, ExitReason
from agent.execution.protection import Step


def live_lists(state: World) -> list[Any]:
    return [ol for ol in state.fake.lists.values() if ol.status == "EXECUTING"]


def market_sells(state: World) -> list[Any]:
    return [o for o in state.fake.orders_of("BTCUSDT", "SELL") if o.type == "MARKET"]


def test_reprice_cancels_waits_for_the_legs_and_places_the_new_stop() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.protected(stack)
        before = await state.position(stack, position_id)
        result = await stack.worker.reprice(position_id, Decimal("99.50"))
        assert result.step is Step.PROTECTED
        position = await state.position(stack, position_id)
        assert position.exchange_stop == Decimal("99.50")
        assert position.status == PositionStatus.PROTECTED
        assert (
            position.operation is None and position.op_version == before.op_version + 1
        )
        assert position.unprotected_since is None
        [current] = live_lists(state)
        assert current.orders[1].stop_price == Decimal("99.50")
        assert current.list_client_order_id.endswith("-pl2")

    run(scenario)


def test_leg_filled_during_the_cancel_is_booked_before_the_new_list() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.protected(stack)
        position = await state.position(stack, position_id)
        held = position.quantity
        assert position.protection_list_id is not None
        take_profit = position.protection_list_id.replace("pl", "pt")
        state.fake.fill_leg(take_profit, Decimal("0.03"), Decimal(110))
        result = await stack.worker.reprice(position_id, Decimal("99.50"))
        assert result.step is Step.PROTECTED
        position = await state.position(stack, position_id)
        assert position.quantity == held - Decimal("0.03")
        [current] = live_lists(state)
        assert current.orders[0].quantity == position.quantity

    run(scenario)


def test_lagging_history_of_a_cancelled_leg_blocks_every_new_order() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.protected(stack)
        list_id = (await state.position(stack, position_id)).protection_list_id
        assert list_id is not None
        take_profit = list_id.replace("pl", "pt")
        state.fake.fill_leg(take_profit, Decimal("0.03"), Decimal(110))
        leg = state.fake._by_client_id(take_profit)
        assert leg is not None
        state.fake.hide_trades(leg.order_id)
        waiting = await stack.worker.reprice(position_id, Decimal("99.50"))
        assert (
            waiting.step is Step.WAITING
            and waiting.reason == "leg fills not proven yet"
        )
        assert live_lists(state) == [] and market_sells(state) == []
        state.fake.reveal_trades()
        resumed = await stack.worker.resume(position_id)
        assert resumed.step is Step.PROTECTED
        [current] = live_lists(state)
        assert current.orders[1].stop_price == Decimal("99.50")

    run(scenario)


def test_crash_after_the_cancel_resumes_from_the_database() -> None:
    async def scenario(state: World) -> None:
        first = await state.stack()
        position_id = await state.protected(first)

        async def crash(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("process died after the cancel")

        first.protector._place = crash  # type: ignore[method-assign]
        try:
            await first.worker.reprice(position_id, Decimal("99.50"))
        except RuntimeError:
            pass
        await state.kill(first)
        assert live_lists(state) == []  # protection is gone for now
        position = await state.position(first, position_id)
        assert (
            position.operation == "reprice" and position.unprotected_since is not None
        )
        second = await state.stack()
        result = await second.worker.resume(position_id)
        assert result.step is Step.PROTECTED
        assert (await state.position(second, position_id)).exchange_stop == Decimal(
            "99.50"
        )

    run(scenario)


def test_refused_replacement_exits_instead_of_staying_unprotected() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.protected(stack)
        state.fake.fault("order_list_oco", "reject", code=-1013)
        result = await stack.worker.reprice(position_id, Decimal("99.50"))
        assert result.step is Step.CLOSED
        position = await state.position(stack, position_id)
        assert position.outcome is not None
        assert position.outcome["mechanism"] == "market_unprotected"

    run(scenario)


def test_late_ack_of_the_replacement_is_found_by_the_protection_loop() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.protected(stack)
        state.fake.fault("order_list_oco", "timeout_after")
        result = await stack.worker.reprice(position_id, Decimal("99.50"))
        assert result.step is Step.WAITING
        found = await stack.protector.protect(position_id)
        assert found.step is Step.PROTECTED
        assert (await state.position(stack, position_id)).exchange_stop == Decimal(
            "99.50"
        )
        assert len(live_lists(state)) == 1 and market_sells(state) == []

    run(scenario)


def test_unknown_cancel_past_the_deadline_is_overdue_but_never_sold_blind() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.protected(stack)
        state.fake.fault("delete_order_list", "timeout_before")
        waiting = await stack.worker.reprice(position_id, Decimal("99.50"))
        assert waiting.reason == "cancel outcome unknown"
        state.clock.advance(31)
        assert await stack.protector.check_deadline(position_id)
        assert market_sells(state) == [] and len(live_lists(state)) == 1
        # The old list protected the position all along. Once the retried
        # cancel went through and both legs are proven, the overdue position
        # is sold — once, for the proven remainder.
        result = await stack.worker.resume(position_id)
        assert result.step is Step.CLOSED
        position = await state.position(stack, position_id)
        assert position.outcome is not None
        assert position.outcome["mechanism"] == "market_unprotected"
        assert len(market_sells(state)) == 1 and live_lists(state) == []

    run(scenario)


def test_lagging_fills_past_the_deadline_wait_then_sell_the_proven_remainder() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.protected(stack)
        list_id = (await state.position(stack, position_id)).protection_list_id
        assert list_id is not None
        stop_leg = list_id.replace("pl", "ps")
        state.fake.fill_leg(stop_leg, Decimal("0.02"), Decimal(98))
        leg = state.fake._by_client_id(stop_leg)
        assert leg is not None
        state.fake.hide_trades(leg.order_id)
        await stack.worker.reprice(position_id, Decimal("99.50"))
        state.clock.advance(31)
        still = await stack.worker.resume(position_id)
        assert still.step is Step.WAITING and market_sells(state) == []
        state.fake.reveal_trades()
        state.fake.set_price("BTCUSDT", Decimal(100))  # the tape moves on
        done = await stack.worker.resume(position_id)
        assert done.step is Step.CLOSED
        [sale] = market_sells(state)
        position = await state.position(stack, position_id)
        assert position.sold_qty == Decimal("0.02") + sale.executed  # no oversell

    run(scenario)


def test_manual_close_goes_through_the_same_protocol() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.protected(stack)
        result = await stack.worker.close(
            position_id, ExitReason.MANUAL, ExitMechanism.MANUAL_CLOSE
        )
        assert result.step is Step.CLOSED
        position = await state.position(stack, position_id)
        assert position.outcome is not None
        assert (position.outcome["reason"], position.outcome["mechanism"]) == (
            "manual",
            "manual_close",
        )
        assert live_lists(state) == [] and len(market_sells(state)) == 1
        assert state.fake.balance("BTC") == (Decimal(0), Decimal(0))

    run(scenario)
