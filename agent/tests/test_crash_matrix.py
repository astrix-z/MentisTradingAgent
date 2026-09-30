"""EXEC-13: crash matrix of stage 2 (plan §12) with the invariants after each.

Every scenario injects one fault into the whole stack (PostgreSQL, the fake
exchange, production code above it), lets a **new process** recover — as the
agent would after a crash — and then checks the same invariants:

* no repeated logical mutation: at most one BUY per signal, no oversell;
* ledger = Σ buy − Σ sell − base commissions, equal to the booked fills, and
  every exchange trade booked at most once;
* progress: every position is either protected by a list that is live on the
  exchange, or closed — or explicitly UNKNOWN with an escalation;
* reserves: only unresolved intents hold quote; a filled intent has its
  position; a position's events are numbered without gaps;
* exchange → ledger completeness: every BUY and SELL of ours that executed on
  the exchange is in the ledger of its position, whatever the local intent
  says — a filled BUY with no position is orphaned exposure, not progress;
* every client id fits 36 characters.

Scenarios covered in depth by the per-component suites (entry, protection,
worker, trailing, reconcile, runtime, admission, feedback) appear here once more
end to end. Publish/XACK of the backend projection belongs to stage 6 and is
not part of this matrix (plan §12).
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from execution_world import CONTEXT, Stack, World, run
from redis.exceptions import RedisError
from sqlalchemy import select, text
from test_execution_service import service
from test_market_streams import isolate_sdk_state  # noqa: F401
from test_monitor_feed import Sdks

from agent.engine import Engine
from agent.exchange.hub import StreamHub
from agent.execution.admission import record_signal
from agent.execution.models import (
    INTENT_HOLDING_STATES,
    ExecutionEvent,
    ExecutionIntent,
    Fill,
    IntentState,
    OrderAttempt,
    Position,
    PositionStatus,
    SymbolCooldown,
)
from agent.execution.orders import EntryOutcome
from agent.execution.outcome import ExitMechanism, ExitReason
from agent.execution.reconcile import Reconciler
from agent.execution.runtime import ContextRefused, ExecutionRuntime
from agent.execution.service import ExecutionService
from agent.execution.settings import ExecutionSettings

# --- invariants ---------------------------------------------------------------------


async def invariants(
    state: World, runtime: ExecutionRuntime, *, progress: bool = True
) -> None:
    fake = state.fake
    async with runtime.session() as session:
        positions = (await session.scalars(select(Position))).all()
        fills = (await session.scalars(select(Fill))).all()
        intents = (await session.scalars(select(ExecutionIntent))).all()
        attempts = (await session.scalars(select(OrderAttempt))).all()
        events = (
            await session.scalars(
                select(ExecutionEvent).where(ExecutionEvent.position_id.is_not(None))
            )
        ).all()

    # One BUY per signal on the exchange.
    buys = Counter(
        o.client_order_id.rsplit("-", 1)[0]
        for o in fake.orders.values()
        if o.side == "BUY"
    )
    assert all(count == 1 for count in buys.values()), buys

    # Every trade booked at most once.
    booked = Counter((f.symbol, f.trade_id) for f in fills)
    assert all(count == 1 for count in booked.values())

    for position in positions:
        mine = [f for f in fills if f.position_id == position.id]
        bought = sum((f.quantity for f in mine if f.side == "BUY"), Decimal(0))
        sold = sum((f.quantity for f in mine if f.side == "SELL"), Decimal(0))
        base = sum(
            (f.commission for f in mine if f.commission_asset == position.symbol[:-4]),
            Decimal(0),
        )
        assert position.bought_qty == bought and position.sold_qty == sold
        assert position.quantity == max(bought - sold - base, Decimal(0))
        assert sold <= bought - base, f"oversold {position.symbol}"
        seqs = sorted(
            e.aggregate_seq
            for e in events
            if e.position_id == position.id and e.aggregate_seq is not None
        )
        assert seqs == list(range(1, len(seqs) + 1))
        if progress:
            if position.status == PositionStatus.PROTECTED:
                live = [
                    ol
                    for ol in fake.lists.values()
                    if ol.list_client_order_id == position.protection_list_id
                    and ol.status == "EXECUTING"
                ]
                assert live, f"{position.symbol} protected by a dead list"
            else:
                assert position.status in (
                    PositionStatus.CLOSED,
                    PositionStatus.UNKNOWN,
                ), (
                    position.symbol,
                    position.status,
                )

    if progress:
        exchange_to_ledger(fake, positions)

    for intent in intents:
        if intent.state not in INTENT_HOLDING_STATES:
            assert intent.quote_reserved == 0
        if intent.state == IntentState.FILLED:
            assert any(p.signal_id == intent.signal_id for p in positions)

    assert all(len(a.client_order_id) <= 36 for a in attempts)
    for asset in ("BTC", "ETH", "SOL"):
        free, locked = fake.balance(asset)
        assert free >= 0 and locked >= 0


def exchange_to_ledger(fake: Any, positions: Any) -> None:
    """What executed on the exchange for our client ids is what the ledger holds.

    Our ids are ``mta-<24 hex of the signal>-<role><n>``, so every order of
    ours names its signal and so its position.
    """
    by_signal = {p.signal_id.hex[:24]: p for p in positions}
    executed: dict[tuple[str, str], Decimal] = {}
    for order in fake.orders.values():
        cid = order.client_order_id
        if not cid.startswith("mta-") or order.executed <= 0:
            continue
        key = (cid.split("-")[1], order.side)
        executed[key] = executed.get(key, Decimal(0)) + order.executed
    for (signal, side), quantity in sorted(executed.items()):
        position = by_signal.get(signal)
        assert position is not None, f"orphaned exchange {side} of {signal}"
        booked = position.bought_qty if side == "BUY" else position.sold_qty
        assert booked == quantity, (position.symbol, side, booked, quantity)


async def recover(state: World, rounds: int = 4) -> ExecutionService:
    """A new process takes over and works until things settle."""
    for stack in list(state.stacks):
        await state.kill(stack)
    state.clock.advance(70)  # lost requests become provably absent
    for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        state.fake.set_price(symbol, state.fake.last[symbol])  # a fresh tape
    recovered = service(state)
    await recovered.runtime.boot()
    for _ in range(rounds):
        await recovered.tick()
    return recovered


async def finish(state: World, recovered: ExecutionService) -> None:
    await invariants(state, recovered.runtime)
    await recovered.stop()
    await recovered.runtime.shutdown()


Scenario = Callable[[World], Awaitable[None]]


def matrix(scenario: Scenario, **options: Any) -> None:
    run(scenario, **options)


# --- entry ---------------------------------------------------------------------------------


def test_accept_then_timeout() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        signal_id = await state.reserve(stack)
        state.fake.fault("new_order", "timeout_after")
        await stack.orders.execute_entry(signal_id)
        await finish(state, await recover(state))

    matrix(scenario)


def test_delayed_not_found() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        signal_id = await state.reserve(stack)
        state.fake.fault("new_order", "timeout_after")
        await stack.orders.execute_entry(signal_id)
        state.fake.fault("get_order", "not_found", times=2)
        await finish(state, await recover(state, rounds=6))

    matrix(scenario)


def test_fok_expired() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        state.fake.fok_outcome = "expire"
        signal_id = await state.reserve(stack)
        assert (
            await stack.orders.execute_entry(signal_id)
        ).outcome is EntryOutcome.RELEASED
        await finish(state, await recover(state))

    matrix(scenario)


def test_lost_buy_response_with_lagging_history_and_base_commission() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        signal_id = await state.reserve(stack)
        state.fake.fault("new_order", "timeout_after")
        await stack.orders.execute_entry(signal_id)
        [buy] = state.fake.orders_of("BTCUSDT", "BUY")
        state.fake.hide_trades(buy.order_id)
        recovered = await recover(state)
        position = await state.position_of(stack, signal_id)
        assert position is not None and position.quantity == 0  # nothing unproven sold
        assert state.fake.lists == {}
        state.fake.reveal_trades()
        for _ in range(4):
            await recovered.tick()
        await finish(state, recovered)

    matrix(scenario, commission_asset="base")


@pytest.mark.parametrize("boundary", ["after_prepare", "after_send", "after_ack"])
def test_crash_at_each_boundary_of_the_entry(boundary: str) -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        signal_id = await state.reserve(stack)
        if boundary == "after_prepare":
            state.fake.fault(
                "new_order", "timeout_before"
            )  # never reached the exchange
        elif boundary == "after_send":
            state.fake.fault("new_order", "timeout_after")
        else:
            # The ACK is committed, the process dies before applying it.
            stack.orders._settle = AsyncMock(  # type: ignore[method-assign]
                side_effect=RuntimeError("process killed")
            )
        try:
            await stack.orders.execute_entry(signal_id)
        except RuntimeError:
            assert boundary == "after_ack"
        recovered = await recover(state, rounds=6)
        intent = await state.intent(stack, signal_id)
        expected = (
            IntentState.NO_FILL if boundary == "after_prepare" else IntentState.FILLED
        )
        assert intent.state == expected
        await finish(state, recovered)

    matrix(scenario)


def test_stop_during_the_buy_keeps_its_position() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        signal_id = await state.reserve(stack)
        entered, release = state.fake.gate("new_order")
        task = asyncio.create_task(stack.orders.execute_entry(signal_id))
        await asyncio.to_thread(entered.wait, 5)
        stack.runtime.close_admission()  # Stop
        release.set()
        result = await task
        assert result.outcome is EntryOutcome.FILLED
        await stack.protector.protect(result.position_id)  # type: ignore[arg-type]
        await finish(state, await recover(state))

    matrix(scenario)


# --- protection and exits --------------------------------------------------------------------


def test_oco_refused_by_a_filter_and_then_throttled() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.filled(stack)
        state.fake.fault("order_list_oco", "rate_limit", retry_after=0)
        assert (await stack.protector.protect(position_id)).step.value in (
            "protected",
            "waiting",
        )
        state.fake.set_price("ETHUSDT", Decimal(100))  # the tape moved on meanwhile
        other = await state.filled(stack, "ETHUSDT")
        state.fake.fault("order_list_oco", "reject", code=-1013)
        assert (await stack.protector.protect(other)).step.value == "closed"
        await finish(state, await recover(state))

    matrix(scenario)


def test_partial_exit_leg_and_terminal_partial_market_sell() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.protected(stack)
        position = await state.position(stack, position_id)
        assert position.protection_list_id is not None
        state.fake.fill_leg(
            position.protection_list_id.replace("pl", "pt"),
            Decimal("0.01"),
            Decimal(110),
        )
        state.fake.market_fill_ratio = Decimal("0.5")
        result = await stack.worker.close(
            position_id, ExitReason.MANUAL, ExitMechanism.MANUAL_CLOSE
        )
        # Each half-filled SELL leaves a confirmed remainder that may be sold
        # again; the operation stays recorded and the next process finishes it.
        assert result.step.value in ("closed", "waiting"), result
        recovered = await recover(state, rounds=6)
        sells = [
            o for o in state.fake.orders_of("BTCUSDT", "SELL") if o.type == "MARKET"
        ]
        assert len(sells) >= 2
        assert (
            await state.position(stack, position_id)
        ).status == PositionStatus.CLOSED
        await finish(state, recovered)

    matrix(scenario)


def test_fill_during_cancel_and_restart_during_reprice() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.protected(stack)
        position = await state.position(stack, position_id)
        assert position.protection_list_id is not None
        state.fake.fill_leg(
            position.protection_list_id.replace("pl", "pt"),
            Decimal("0.02"),
            Decimal(110),
        )
        state.fake.fault("order_list_oco", "timeout_before")
        await stack.worker.reprice(position_id, Decimal("99.5"))
        await finish(state, await recover(state, rounds=6))

    matrix(scenario)


def test_unknown_cancel_beyond_the_deadline() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.protected(stack)
        state.fake.fault("delete_order_list", "timeout_before")
        await stack.worker.reprice(position_id, Decimal("99.5"))
        state.clock.advance(31)
        await stack.protector.check_deadline(position_id)
        assert [
            o for o in state.fake.orders_of("BTCUSDT", "SELL") if o.type == "MARKET"
        ] == []
        await finish(state, await recover(state, rounds=6))

    matrix(scenario)


def test_cancelled_legs_with_lagging_fills_beyond_the_deadline() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.protected(stack)
        position = await state.position(stack, position_id)
        assert position.protection_list_id is not None
        stop_leg = position.protection_list_id.replace("pl", "ps")
        state.fake.fill_leg(stop_leg, Decimal("0.02"), Decimal(98))
        leg = state.fake._by_client_id(stop_leg)
        assert leg is not None
        state.fake.hide_trades(leg.order_id)
        await stack.worker.reprice(position_id, Decimal("99.5"))
        recovered = await recover(state)
        state.fake.reveal_trades()
        for _ in range(6):
            await recovered.tick()
        await finish(state, recovered)

    matrix(scenario)


def test_time_exit_during_downtime() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        await state.protected(stack)
        state.clock.advance(9 * 3600)
        recovered = await recover(state)
        position = await state.position_of(stack, (await _only(state, stack)).signal_id)
        assert position is not None and position.status == PositionStatus.CLOSED
        await finish(state, recovered)

    matrix(scenario)


@pytest.mark.parametrize("asset", ["BNB", "base", "quote"])
def test_commissions_in_every_asset(asset: str) -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.protected(stack)
        await stack.worker.close(
            position_id, ExitReason.MANUAL, ExitMechanism.MANUAL_CLOSE
        )
        position = await state.position(stack, position_id)
        assert position.status == PositionStatus.CLOSED
        assert position.commission_quote > 0
        await finish(state, await recover(state))

    matrix(scenario, commission_asset=asset)


def test_repeated_application_of_one_fill() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        await state.protected(stack)
        recovered = await recover(state)
        for _ in range(3):
            await recovered.reconciler.run()
        await finish(state, recovered)

    matrix(scenario)


# --- lifecycle, lock, context, Redis ------------------------------------------------------------


def test_boot_off_with_an_open_position_protects_it() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        await state.filled(stack)  # filled, never protected; strategy never runs
        await finish(state, await recover(state))

    matrix(scenario)


def test_failed_start_and_network_change_with_unknown_work() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        signal_id = await state.reserve(stack)
        state.fake.fault("new_order", "timeout_after")
        await stack.orders.execute_entry(signal_id)  # UNKNOWN entry on live
        with pytest.raises(ContextRefused):
            await stack.runtime.prepare_start(
                network="testnet", settings=ExecutionSettings(enabled=True), params={}
            )
        await finish(state, await recover(state))

    matrix(scenario)


def test_two_processes_and_a_lost_lock_session() -> None:
    async def scenario(state: World) -> None:
        first = await state.stack()
        second = await state.stack()
        assert first.runtime.may_mutate() != second.runtime.may_mutate()
        leader, follower = (
            (first, second) if first.runtime.may_mutate() else (second, first)
        )
        position_id = await state.filled(leader)
        assert leader.runtime.lock is not None
        pid = await leader.runtime.lock.backend_pid()
        async with leader.runtime.session() as session:
            await session.execute(text("select pg_terminate_backend(:p)"), {"p": pid})
            await session.commit()
        await follower.runtime.tick()  # the other process takes the free lock
        await leader.runtime.tick()
        assert not leader.runtime.may_mutate() and follower.runtime.may_mutate()
        assert (
            await leader.protector.protect(position_id)
        ).reason == "no execution lock"
        await finish(state, await recover(state))

    matrix(scenario)


def test_redis_down_does_not_stop_emit_or_protection() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        redis = Mock(
            publish=AsyncMock(side_effect=RedisError("down")),
            set=AsyncMock(side_effect=RedisError("down")),
        )
        engine = Engine(redis)
        engine.execution = stack.runtime
        engine._network = "live"
        engine._strategy_name = "tripple_screen"
        signal = state.entry()
        await engine._on_signal(signal)  # recorded in the inbox; publish failed
        await engine.publish_heartbeat()  # swallowed
        decisions = await stack.admission.drain()
        assert [d.state for d in decisions] == [IntentState.RESERVED]
        result = await stack.orders.execute_entry(decisions[0].signal_id)
        assert result.position_id is not None
        await stack.protector.protect(result.position_id)
        json.dumps(stack.runtime.heartbeat())
        await finish(state, await recover(state))

    matrix(scenario)


def test_daily_limit_and_the_utc_reset() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        for _ in range(5):
            position_id = await state.protected(stack)
            state.fake.set_price("BTCUSDT", Decimal(90))  # the stop sells at a loss
            await Reconciler(stack.worker).run()
            assert (
                await state.position(stack, position_id)
            ).status == PositionStatus.CLOSED
            state.fake.set_price("BTCUSDT", Decimal(100))
            async with stack.runtime.session() as session, session.begin():
                for row in (await session.scalars(select(SymbolCooldown))).all():
                    await session.delete(row)
        signal = state.entry()
        async with stack.runtime.session() as session:
            await record_signal(
                session, signal, context=CONTEXT, strategy="tripple_screen"
            )
        [decision] = await stack.admission.drain()
        assert decision.reason == "daily_loss_limit"
        state.clock.advance(24 * 3600)
        state.fake.set_price("BTCUSDT", Decimal(100))
        assert await state.reserve(stack)
        await invariants(state, stack.runtime, progress=False)

    matrix(scenario)


async def _only(state: World, stack: Stack) -> Position:
    async with stack.runtime.session() as session:
        return (await session.scalars(select(Position))).one()


# --- monitor feed and trailing (a process with a real StreamHub) --------------------


M15 = 900_000


async def settle_feed(recovered: ExecutionService) -> None:
    """Let the feed's backfills and the trailing work they start finish."""
    for _ in range(5):
        feed = recovered.feed
        assert feed is not None
        pending = [*feed._backfills.values(), *recovered._busy]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


def test_trailing_reprice_lost_then_crash_recovers_through_the_feed() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.protected(stack)
        before = await state.position(stack, position_id)
        assert before.exchange_stop is not None
        await state.kill(stack)
        state.clock.advance(90 * 60)
        # Closed 15m bars after the fill whose lows raise the stop.
        base = state.clock.ms() // M15 * M15
        lows = ["99", "101.0", "101.2", "101.5"]
        state.fake.klines_data[("BTCUSDT", "15m")] = [
            [base - M15 * k, "101", "102", low, "101.6", "1", base - M15 * (k - 1) - 1]
            for k, low in zip(range(len(lows), 0, -1), lows, strict=True)
        ]
        state.fake.set_price("BTCUSDT", Decimal("101.6"))
        state.fake.fault("order_list_oco", "timeout_after")  # the replacement's answer

        first = service(state, StreamHub(factory=Sdks().factory))
        await first.runtime.boot()
        await first.tick()  # follow → backfill → trailer → reprice
        await settle_feed(first)
        raised = await state.position(stack, position_id)
        assert raised.desired_stop is not None
        assert raised.desired_stop > before.exchange_stop
        # The replacement went out and its answer was lost: still repricing.
        assert raised.operation == "reprice"
        assert state.fake.calls_to("order_list_oco") == 2
        await first.stop()
        await first.runtime.shutdown()  # dies mid-reprice or right after it

        state.clock.advance(70)
        second = service(state, StreamHub(factory=Sdks().factory))
        await second.runtime.boot()
        for _ in range(4):
            await second.tick()
            await settle_feed(second)
        position = await state.position(stack, position_id)
        assert position.status == PositionStatus.PROTECTED
        assert position.exchange_stop == raised.desired_stop
        live = [ol for ol in state.fake.lists.values() if ol.status == "EXECUTING"]
        assert len(live) == 1
        await finish(state, second)

    matrix(scenario)
