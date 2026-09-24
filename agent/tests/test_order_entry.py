"""EXEC-08b: the entry — LIMIT FOK, fills through the applier, proven quantity."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal

from execution_world import World, run

from agent.execution.models import AttemptState, IntentState, PositionStatus
from agent.execution.orders import EntryOutcome


def test_filled_entry_opens_a_proven_position_and_pays_off_the_reserve() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        filled: list[object] = []

        async def on_filled(position_id: object) -> None:
            filled.append(position_id)

        stack.orders.on_filled = on_filled
        signal_id = await state.reserve(stack)
        result = await stack.orders.execute_entry(signal_id)
        assert result.outcome is EntryOutcome.FILLED
        intent = await state.intent(stack, signal_id)
        assert intent.state == IntentState.FILLED and intent.quote_reserved == 0
        position = await state.position_of(stack, signal_id)
        assert position is not None and position.id == result.position_id
        assert position.status == PositionStatus.PROTECTING
        assert position.quantity == intent.quantity
        assert position.unprotected_since is not None
        assert await stack.orders.prove_entry(position.id)
        assert filled == [position.id]
        [attempt] = await state.attempts(stack, signal_id)
        assert attempt.role == "e" and attempt.state == AttemptState.ACKED
        assert attempt.request["timeInForce"] == "FOK"
        assert len(state.fake.orders_of("BTCUSDT", "BUY")) == 1

    run(scenario)


def test_expired_fok_releases_the_slot_for_the_next_epoch() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        state.fake.fok_outcome = "expire"
        signal_id = await state.reserve(stack)
        result = await stack.orders.execute_entry(signal_id)
        assert (
            result.outcome is EntryOutcome.RELEASED and result.reason == "fok_expired"
        )
        intent = await state.intent(stack, signal_id)
        assert intent.state == IntentState.NO_FILL and intent.quote_reserved == 0
        assert await state.position_of(stack, signal_id) is None
        state.fake.fok_outcome = "fill"
        again = await state.reserve(stack)  # same symbol, next epoch
        assert (await stack.orders.execute_entry(again)).outcome is EntryOutcome.FILLED

    run(scenario)


def test_refused_entry_is_released_with_the_exchange_code() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        signal_id = await state.reserve(stack)
        state.fake.fault("new_order", "reject", code=-1013)
        result = await stack.orders.execute_entry(signal_id)
        assert result.outcome is EntryOutcome.RELEASED
        assert result.reason == "entry_rejected:-1013"
        assert (await state.intent(stack, signal_id)).state == IntentState.NO_FILL

    run(scenario)


def test_timeout_after_accept_is_found_later_and_never_bought_twice() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        signal_id = await state.reserve(stack)
        state.fake.fault("new_order", "timeout_after")
        result = await stack.orders.execute_entry(signal_id)
        assert result.outcome is EntryOutcome.UNKNOWN
        intent = await state.intent(stack, signal_id)
        assert intent.state == IntentState.UNKNOWN and intent.quote_reserved > 0
        # The same ENTRY is not sent again while UNKNOWN.
        assert (
            await stack.orders.execute_entry(signal_id)
        ).outcome is EntryOutcome.DEFERRED
        # A restarted process resolves it from the exchange.
        restarted = await state.stack()
        state.fake.fault("get_order", "not_found")
        [pending] = await restarted.orders.resolve_entries()
        assert pending.outcome is EntryOutcome.UNKNOWN
        [found] = await restarted.orders.resolve_entries()
        assert found.outcome is EntryOutcome.FILLED
        position = await state.position_of(restarted, signal_id)
        intent = await state.intent(restarted, signal_id)
        assert position is not None and position.quantity == intent.quantity
        assert intent.state == IntentState.FILLED and intent.quote_reserved == 0
        assert len(state.fake.orders_of("BTCUSDT", "BUY")) == 1

    run(scenario, usdt="10")


def test_lost_response_with_lagging_history_does_not_overstate_the_quantity() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        signal_id = await state.reserve(stack)
        state.fake.fault("new_order", "timeout_after")
        await stack.orders.execute_entry(signal_id)
        [buy] = state.fake.orders_of("BTCUSDT", "BUY")
        state.fake.hide_trades(buy.order_id)  # myTrades not caught up yet
        [result] = await stack.orders.resolve_entries()
        assert result.outcome is EntryOutcome.FILLED
        position = await state.position_of(stack, signal_id)
        assert position is not None
        assert position.quantity == 0  # nothing booked, nothing to protect or sell
        assert not await stack.orders.prove_entry(position.id)
        state.fake.reveal_trades()
        assert await stack.orders.prove_entry(position.id)
        position = await state.position_of(stack, signal_id)
        assert position is not None
        # Commission paid in the base asset reduces what can be sold.
        assert position.quantity == buy.executed - buy.executed * Decimal("0.001")
        assert len(state.fake.orders_of("BTCUSDT", "BUY")) == 1

    run(scenario, commission_asset="base")


def test_checks_before_the_send_release_instead_of_buying() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        stopped = await state.reserve(stack)
        stack.runtime.close_admission()  # Stop between admission and send
        assert (await stack.orders.execute_entry(stopped)).reason == "strategy_stopped"
        stack.runtime.open_admission("tripple_screen")
        late = await state.reserve(stack, "ETHUSDT", valid_for=timedelta(seconds=30))
        state.clock.advance(31)
        assert (await stack.orders.execute_entry(late)).reason == "expired"
        low = await state.reserve(stack, "SOLUSDT")
        state.fake.set_price("SOLUSDT", Decimal(97))  # fell through stop_ref 98
        assert (await stack.orders.execute_entry(low)).reason == "stop_above_price"
        assert state.fake.orders == {}

    run(scenario)


def test_no_execution_lock_defers_without_sending() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        signal_id = await state.reserve(stack)
        assert stack.runtime.lock is not None
        await stack.runtime.lock.release()
        result = await stack.orders.execute_entry(signal_id)
        assert result.outcome is EntryOutcome.DEFERRED
        assert (await state.intent(stack, signal_id)).state == IntentState.RESERVED
        assert state.fake.orders == {}

    run(scenario)


def test_cancelled_wait_keeps_the_entry_unknown() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        signal_id = await state.reserve(stack)
        entered, release = state.fake.gate("new_order")
        task = asyncio.create_task(stack.orders.execute_entry(signal_id))
        await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        release.set()
        intent = await state.intent(stack, signal_id)
        assert intent.state == IntentState.UNKNOWN and intent.quote_reserved > 0
        for _ in range(100):
            if state.fake.orders:
                break
            await asyncio.sleep(0.01)
        [found] = await stack.orders.resolve_entries()
        assert found.outcome is EntryOutcome.FILLED
        assert len(state.fake.orders_of("BTCUSDT", "BUY")) == 1

    run(scenario)
