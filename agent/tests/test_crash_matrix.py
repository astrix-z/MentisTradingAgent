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
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from execution_world import CONTEXT, Stack, World, run
from redis.exceptions import RedisError
from sqlalchemy import select, text
from test_execution_service import service, upgrade_from_0002
from test_market_streams import isolate_sdk_state  # noqa: F401
from test_monitor_feed import Sdks

from agent.engine import Engine
from agent.exchange.budget import Priority
from agent.exchange.hub import StreamHub
from agent.execution.admission import record_signal
from agent.execution.attempts import Role
from agent.execution.lock import LeaderLock
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
from agent.execution.quotes import QuoteProvider, QuoteSnapshot
from agent.execution.reconcile import Reconciler, ReconcileReport
from agent.execution.runtime import ContextRefused, ExecutionRuntime
from agent.execution.service import ExecutionService
from agent.execution.settings import ExecutionSettings
from agent.execution.worker import Operation

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
                # Adequate: each live leg sells the whole position, to a step.
                step = fake.rules[position.symbol].step
                for leg in live[0].orders:
                    gap = position.quantity - (leg.quantity - leg.executed)
                    assert Decimal(0) <= gap < step, (
                        f"{position.symbol} list does not cover the position: "
                        f"leg {leg.client_order_id} gap {gap}"
                    )
            elif position.status == PositionStatus.UNKNOWN:
                # Only a justified UNKNOWN: an outcome still unresolved on
                # the exchange, and an escalation saying so.
                unresolved = [
                    a
                    for a in attempts
                    if a.signal_id == position.signal_id
                    and (
                        a.state in ("prepared", "sent", "unknown")
                        or (
                            a.role == Role.LIST.value
                            and a.state in ("acked", "resolved")
                            and a.order_list_id is not None
                            and a.applied_at is None
                        )
                    )
                ]
                assert unresolved, f"{position.symbol} UNKNOWN with nothing unresolved"
                assert any(
                    "unknown" in key or position.symbol in text
                    for key, text in runtime.alerts.items()
                ), f"{position.symbol} UNKNOWN without an escalation"
            else:
                assert position.status == PositionStatus.CLOSED, (
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


# --- the oracle itself ---------------------------------------------------------------------


def test_invariants_reject_an_unjustified_unknown() -> None:
    # Review 2026-10-03: a protected position whose OCO is gone, marked
    # UNKNOWN with nothing unresolved and no alert, used to pass.
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.protected(stack)
        position = await state.position(stack, position_id)
        [order_list] = state.fake.lists.values()
        state.fake.delete_order_list(
            symbol="BTCUSDT", list_client_order_id=order_list.list_client_order_id
        )
        async with stack.runtime.session() as session, session.begin():
            row = await session.get(Position, position.id)
            assert row is not None
            row.status = PositionStatus.UNKNOWN
        stack.runtime.alerts.clear()
        with pytest.raises(AssertionError, match="nothing unresolved"):
            await invariants(state, stack.runtime)

    matrix(scenario)


def test_invariants_reject_a_list_smaller_than_the_position() -> None:
    # The ledger stays consistent; only the live list shrinks on the exchange.
    async def scenario(state: World) -> None:
        stack = await state.stack()
        await state.protected(stack)
        await invariants(state, stack.runtime)
        [order_list] = state.fake.lists.values()
        step = state.fake.rules["BTCUSDT"].step
        for leg in order_list.orders:
            leg.quantity -= step
        with pytest.raises(AssertionError, match="does not cover the position"):
            await invariants(state, stack.runtime)

    matrix(scenario)


def test_invariants_accept_unknown_held_by_an_unapplied_list() -> None:
    # Re-review 2026-10-03: the oracle compared the role with "l", not "pl",
    # so an UNKNOWN justified by an accepted, never associated list failed.
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.filled(stack)

        async def crash(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("process killed")

        stack.protector._mark_protected = crash  # type: ignore[method-assign]
        with pytest.raises(RuntimeError):
            await stack.protector.protect(position_id)
        async with stack.runtime.session() as session, session.begin():
            row = await session.get(Position, position_id)
            assert row is not None
            row.status = PositionStatus.UNKNOWN
        stack.runtime.alerts["unknown"] = "list BTCUSDT answered, not applied"
        await invariants(state, stack.runtime)

    matrix(scenario)


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


@pytest.mark.parametrize(
    "boundary", ["after_prepare", "after_send", "after_ack", "after_reject"]
)
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
        elif boundary == "after_ack":
            # The ACK is committed, the process dies before applying it.
            stack.orders._settle = AsyncMock(  # type: ignore[method-assign]
                side_effect=RuntimeError("process killed")
            )
        else:
            # The refusal is committed, the process dies before the release.
            assert boundary == "after_reject"
            state.fake.fault("new_order", "reject", code=-2010)
            stack.orders._release = AsyncMock(  # type: ignore[method-assign]
                side_effect=RuntimeError("process killed")
            )
        try:
            await stack.orders.execute_entry(signal_id)
        except RuntimeError:
            assert boundary in ("after_ack", "after_reject")
        recovered = await recover(state, rounds=6)
        intent = await state.intent(stack, signal_id)
        expected = (
            IntentState.FILLED
            if boundary in ("after_send", "after_ack")
            else IntentState.NO_FILL
        )
        assert intent.state == expected
        await finish(state, recovered)

    matrix(scenario)


@pytest.mark.parametrize("upgrade", [False, True], ids=["fresh", "upgraded"])
@pytest.mark.parametrize("which", ["first", "replacement"])
def test_crash_between_the_oco_ack_and_its_association(
    which: str, upgrade: bool
) -> None:
    # Review 2026-10-03 (EXEC-09): the OCO's answer is committed, the process
    # dies before the list is associated with the position. "upgraded": the
    # state was written before 0003 and the agent restarts on the new schema.
    async def scenario(state: World) -> None:
        stack = await state.stack()
        if which == "first":
            position_id = await state.filled(stack)
        else:
            position_id = await state.protected(stack)
        stack.protector._mark_protected = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("process killed")
        )
        with pytest.raises(RuntimeError):
            if which == "first":
                await stack.protector.protect(position_id)
            else:
                await stack.worker.reprice(position_id, Decimal("99.5"))
        if upgrade:
            for dead in list(state.stacks):
                await state.kill(dead)
            await upgrade_from_0002(state)
        recovered = await recover(state)
        position = await state.position(stack, position_id)
        assert position.status == PositionStatus.PROTECTED
        sells = [o for o in state.fake.orders.values() if o.side == "SELL"]
        assert all(o.type != "MARKET" for o in sells)
        await finish(state, recovered)

    matrix(scenario)


@pytest.mark.parametrize("upgrade", [False, True], ids=["fresh", "upgraded"])
def test_lost_replacement_ack_then_a_recorded_close(upgrade: bool) -> None:
    # Review 2026-10-04 (EXEC-09a/11b/13): pl2 accepted, its association lost,
    # then a manual Close made durable. "upgraded": written before 0003; 0004
    # used to skip every EXIT, leaving pl2 holding the asset against the SELL.
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.protected(stack)
        stack.protector._mark_protected = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("process killed")
        )
        with pytest.raises(RuntimeError):
            await stack.worker.reprice(position_id, Decimal("99.5"))
        await stack.worker._record(
            position_id,
            Operation.EXIT,
            {
                "reason": ExitReason.MANUAL.value,
                "mechanism": ExitMechanism.MANUAL_CLOSE.value,
            },
        )
        if upgrade:
            for dead in list(state.stacks):
                await state.kill(dead)
            await upgrade_from_0002(state)
        recovered = await recover(state)
        position = await state.position(stack, position_id)
        assert position.status == PositionStatus.CLOSED
        sells = [o for o in state.fake.orders.values() if o.side == "SELL"]
        assert [o.type for o in sells if o.type == "MARKET"] == ["MARKET"]
        assert not [ol for ol in state.fake.lists.values() if ol.status == "EXECUTING"]
        await finish(state, recovered)

    matrix(scenario)


@pytest.mark.parametrize("cause", ["stop", "lost_lock"])
def test_admission_lost_during_the_presubmit_quote_sends_nothing(cause: str) -> None:
    # Review 2026-10-04 (EXEC-06/08b/13): Stop, or the lock session killed and
    # taken by another process, while the pre-submit quote is read. Nothing
    # may reach the exchange; the next process finds the intent consistent.
    async def scenario(state: World) -> None:
        stack = await state.stack()
        signal_id = await state.reserve(stack)
        lock = stack.runtime.lock
        assert lock is not None
        rival = LeaderLock(state.url, lock.account, lock.network)
        real = stack.orders.quotes

        async def interfere() -> None:
            if cause == "stop":
                stack.runtime.close_admission()
                return
            pid = await lock.backend_pid()
            async with stack.runtime.session() as session:
                await session.execute(
                    text("select pg_terminate_backend(:p)"), {"p": pid}
                )
                await session.commit()
            for _ in range(100):
                if await rival.acquire():
                    return
                await asyncio.sleep(0.02)
            raise AssertionError("the rival never got the lock")

        class During(QuoteProvider):
            def __init__(self, inner: QuoteProvider) -> None:
                self.inner = inner

            async def snapshot(self, symbol: str) -> QuoteSnapshot:
                quote = await self.inner.snapshot(symbol)
                await interfere()
                return quote

        stack.orders.quotes = lambda network: During(real(network))
        try:
            result = await stack.orders.execute_entry(signal_id)
        finally:
            await rival.dispose()
        assert result.outcome in (EntryOutcome.RELEASED, EntryOutcome.DEFERRED)
        assert state.fake.calls_to("new_order") == 0  # no exchange mutation
        await finish(state, await recover(state))

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
        stack.runtime.settings = ExecutionSettings(enabled=True)
        with pytest.raises(ContextRefused):
            await stack.runtime.prepare_start(network="testnet", params={})
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


# --- single writer after awaited work (review 2026-10-05, F1) -----------------------------

_MUTATION = {
    "initial_oco": "order_list_oco",
    "cancel_oco": "delete_order_list",
    "market_sell": "new_order",
}


async def _lose_lead(state: World, stack: Stack, *, noticed: bool) -> LeaderLock:
    """Kill the leader's lock session and let another process take the lock.

    ``noticed`` — the old process has already asked the database (watchdog);
    otherwise its flag still says it leads.
    """
    lock = stack.runtime.lock
    assert lock is not None
    pid = await lock.backend_pid()
    async with state.db.connect() as admin:
        await admin.execute(text("select pg_terminate_backend(:p)"), {"p": pid})
    if noticed:
        assert not await lock.check()
    rival = LeaderLock(state.url, CONTEXT.account, CONTEXT.network)
    assert await rival.acquire()
    return rival


@pytest.mark.parametrize("when", ["during_preparation", "at_the_send"])
@pytest.mark.parametrize("action", sorted(_MUTATION))
def test_no_new_mutation_after_the_lock_is_lost(action: str, when: str) -> None:
    # OCO and SELL after the quote read, cancel after the operation is
    # recorded: a loss nobody noticed yet is caught by asking the database;
    # one noticed while the request waits for the budget or a worker — by
    # the last look in the sending thread. Nothing reaches the exchange, and
    # the durable operation is left for the new owner.
    async def scenario(state: World) -> None:
        stack = await state.stack()
        if action == "cancel_oco":
            position_id = await state.protected(stack)
        else:
            position_id = await state.filled(stack)
        method = _MUTATION[action]
        before = state.fake.calls_to(method)
        rivals: list[LeaderLock] = []

        async def lose(noticed: bool) -> None:
            if not rivals:
                rivals.append(await _lose_lead(state, stack, noticed=noticed))

        if when == "at_the_send":
            trade = stack.runtime.trade
            assert trade is not None
            call = trade._call

            async def send(name: str, *args: Any, **kwargs: Any) -> Any:
                if name == method:
                    await lose(noticed=True)
                return await call(name, *args, **kwargs)

            trade._call = send  # type: ignore[method-assign]
        elif action == "cancel_oco":
            record = stack.worker._record

            async def recorded(*args: Any, **kwargs: Any) -> Any:
                result = await record(*args, **kwargs)
                await lose(noticed=False)
                return result

            stack.worker._record = recorded  # type: ignore[method-assign]
        else:
            quotes = stack.orders.quotes

            class LateQuote:
                async def snapshot(self, symbol: str) -> QuoteSnapshot:
                    quote = await quotes(CONTEXT.network).snapshot(symbol)
                    await lose(noticed=False)
                    return quote

            stack.orders.quotes = lambda network: LateQuote()  # type: ignore[assignment,return-value]
        try:
            if action == "initial_oco":
                result = await stack.protector.protect(position_id)
            else:
                result = await stack.worker.close(
                    position_id, ExitReason.MANUAL, ExitMechanism.MANUAL_CLOSE
                )
            assert rivals, "the lock was never lost"
            assert result.reason == "no execution lock"
            assert state.fake.calls_to(method) == before
            assert not stack.runtime.may_mutate()
        finally:
            for rival in rivals:
                await rival.dispose()
        await finish(state, await recover(state))

    matrix(scenario)


# --- a list taken down by an operation (review 2026-10-05, F2/F3) ---------------------------


async def _list_down_with_lagging_fills(
    state: World, stack: Any, position_id: Any, operation: str
) -> None:
    """Part of the stop leg filled, its trades not visible yet; then the
    reprice or the close cancels the list and cannot prove the legs."""
    position = await state.position(stack, position_id)
    assert position.protection_list_id is not None
    stop_leg = position.protection_list_id.replace("pl", "ps")
    state.fake.fill_leg(stop_leg, Decimal("0.02"), Decimal(98))
    leg = state.fake._by_client_id(stop_leg)
    assert leg is not None
    state.fake.hide_trades(leg.order_id)
    if operation == "close":
        result = await stack.worker.close(
            position_id, ExitReason.MANUAL, ExitMechanism.MANUAL_CLOSE
        )
    else:
        result = await stack.worker.reprice(position_id, Decimal("99.5"))
    assert result.reason == "leg fills not proven yet", result
    assert not [ol for ol in state.fake.lists.values() if ol.status == "EXECUTING"]


@pytest.mark.parametrize("operation", ["close", "reprice"])
def test_no_new_buy_while_an_operation_has_the_list_down(operation: str) -> None:
    # Neither a new reservation nor an intent reserved before the operation
    # may buy while the exchange holds no protection — without waiting for
    # the 30 s deadline.
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.protected(stack)
        reserved = await state.reserve(stack, "ETHUSDT")
        await _list_down_with_lagging_fills(state, stack, position_id, operation)
        async with stack.runtime.session() as session:
            await record_signal(
                session,
                state.entry("SOLUSDT"),
                context=CONTEXT,
                strategy="tripple_screen",
            )
        [decision] = await stack.admission.drain()
        assert decision.state == IntentState.REJECTED
        assert decision.reason == "unprotected_position"
        buys = len(state.fake.orders_of("ETHUSDT", "BUY"))
        result = await stack.orders.execute_entry(reserved)
        assert result.outcome is EntryOutcome.RELEASED
        assert result.reason == "unprotected_position"
        assert len(state.fake.orders_of("ETHUSDT", "BUY")) == buys
        state.fake.reveal_trades()
        await finish(state, await recover(state, rounds=6))

    matrix(scenario)


def test_close_with_lagging_fills_becomes_overdue_in_the_watchdog() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.protected(stack)
        await _list_down_with_lagging_fills(state, stack, position_id, "close")
        state.clock.advance(31)
        watchdog = service(state)
        await watchdog.runtime.boot()
        assert watchdog.runtime.context is not None
        await watchdog._check_deadlines(watchdog.runtime.context)
        position = await state.position(stack, position_id)
        assert position.status == PositionStatus.PROTECTION_OVERDUE
        # Overdue alerts; the exit and its barrier stay as they were.
        assert position.operation == "exit"
        assert "overdue:BTCUSDT" in watchdog.runtime.alerts
        assert [
            o for o in state.fake.orders_of("BTCUSDT", "SELL") if o.type == "MARKET"
        ] == []
        await watchdog.runtime.shutdown()
        state.fake.reveal_trades()
        await finish(state, await recover(state, rounds=6))

    matrix(scenario)


def test_reprice_stuck_on_its_replacement_becomes_overdue_in_the_watchdog() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.protected(stack)
        # The cancel of the old list hangs: the status still reads repricing.
        entered, release = state.fake.gate("delete_order_list")
        task = asyncio.create_task(stack.worker.reprice(position_id, Decimal("99.5")))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            assert (await state.position(stack, position_id)).status == (
                PositionStatus.REPRICING
            )
            state.clock.advance(31)
            watchdog = service(state)
            await watchdog.runtime.boot()
            assert watchdog.runtime.context is not None
            await watchdog._check_deadlines(watchdog.runtime.context)
            await watchdog.runtime.shutdown()
            position = await state.position(stack, position_id)
            assert position.status == PositionStatus.PROTECTION_OVERDUE
            assert position.operation == "reprice"
        finally:
            release.set()
            await task
        await finish(state, await recover(state, rounds=6))

    matrix(scenario)


# --- a reservation is no licence (review 2026-10-05 ALL, F1) ---------------------------


async def _losing_close(state: World, stack: Stack) -> None:
    position_id = await state.protected(stack)
    state.fake.set_price("BTCUSDT", Decimal(90))  # the stop sells at a loss
    await Reconciler(stack.worker).run()
    assert (await state.position(stack, position_id)).status == PositionStatus.CLOSED
    state.fake.set_price("BTCUSDT", Decimal(100))
    async with stack.runtime.session() as session, session.begin():
        for row in (await session.scalars(select(SymbolCooldown))).all():
            await session.delete(row)


def test_a_reserved_buy_waiting_through_the_fifth_loss_is_not_sent() -> None:
    # Reserved with four losses; the fifth comes before its BUY: not sent, and
    # the next UTC day buys again.
    async def scenario(state: World) -> None:
        stack = await state.stack()
        for _ in range(4):
            await _losing_close(state, stack)
        queued = await state.reserve(stack, "ETHUSDT")
        await _losing_close(state, stack)
        result = await stack.orders.execute_entry(queued)
        assert (result.outcome, result.reason) == (
            EntryOutcome.RELEASED,
            "daily_loss_limit",
        )
        assert state.fake.orders_of("ETHUSDT", "BUY") == []
        state.clock.advance(24 * 3600)
        state.fake.set_price("ETHUSDT", state.fake.last["ETHUSDT"])  # fresh tape
        again = await state.reserve(stack, "ETHUSDT")
        assert (await stack.orders.execute_entry(again)).outcome is EntryOutcome.FILLED
        await finish(state, await recover(state))

    matrix(scenario)


def test_recovery_does_not_send_a_reservation_the_fifth_loss_closed() -> None:
    # Recovery executes every RESERVED intent after reconcile (service.py):
    # one reserved before the fifth loss is released there, not bought.
    async def scenario(state: World) -> None:
        stack = await state.stack()
        for _ in range(4):
            await _losing_close(state, stack)
        queued = await state.reserve(stack, "ETHUSDT")
        await _losing_close(state, stack)
        await state.kill(stack)
        recovered = service(state)
        recovered.runtime.open_admission("tripple_screen")  # the strategy runs
        await recovered.runtime.boot()
        await recovered.tick()
        assert state.fake.orders_of("ETHUSDT", "BUY") == []
        intent = await state.intent(stack, queued)
        assert (intent.state, intent.reason) == (
            IntentState.NO_FILL,
            "daily_loss_limit",
        )
        await finish(state, recovered)

    matrix(scenario)


def test_a_reserved_buy_is_not_sent_while_another_entry_is_unknown() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        first = await state.reserve(stack, "BTCUSDT")
        queued = await state.reserve(stack, "ETHUSDT")
        state.fake.fault("new_order", "timeout_before")  # never reached the book
        assert (await stack.orders.execute_entry(first)).outcome is EntryOutcome.UNKNOWN
        result = await stack.orders.execute_entry(queued)
        assert (result.outcome, result.reason) == (
            EntryOutcome.RELEASED,
            "unknown_entry",
        )
        assert state.fake.orders_of("ETHUSDT", "BUY") == []
        # The UNKNOWN entry keeps what it holds until it is resolved.
        assert (await state.intent(stack, first)).state == IntentState.UNKNOWN
        for _ in range(3):  # proven absent once its time has passed
            state.clock.advance(60)
            await stack.orders.resolve_entries()
        assert (await state.intent(stack, first)).state == IntentState.NO_FILL
        state.fake.set_price("ETHUSDT", state.fake.last["ETHUSDT"])  # fresh tape
        again = await state.reserve(stack, "ETHUSDT")
        assert (await stack.orders.execute_entry(again)).outcome is EntryOutcome.FILLED
        await finish(state, await recover(state))

    matrix(scenario)


# --- the risk read by the gate is the risk at the send (review 2026-10-05 REPEAT, R1) -------


async def _fifth_loss(state: World, stack: Stack, position_id: Any) -> None:
    """The protected fifth position's stop fills; its trades close it.

    Only BTC's trades are synced: a whole reconcile would first resolve the
    SUBMITTED ETH entry and wait for its lock, held by the BUY that is
    waiting here (in production reconcile runs in its own task).
    """
    state.fake.set_price("BTCUSDT", Decimal(90))
    reconciler = Reconciler(stack.worker)
    await reconciler.sync_trades(CONTEXT, "BTCUSDT")
    for position in await reconciler._open_positions(CONTEXT):
        await reconciler._check_protection(position, ReconcileReport())
    assert (await state.position(stack, position_id)).status == PositionStatus.CLOSED


@pytest.mark.parametrize("boundary", ["journal", "budget"])
def test_a_loss_after_the_risk_gate_stops_the_buy(boundary: str) -> None:
    # Four losses, the fifth position still protected, ETH reserved: the gate
    # passes. The fifth closes while the BUY writes its journal, or while it
    # waits for the budget (only the check in the sending thread sees that).
    async def scenario(state: World) -> None:
        stack = await state.stack()
        for _ in range(4):
            await _losing_close(state, stack)
        fifth = await state.protected(stack)
        queued = await state.reserve(stack, "ETHUSDT")
        if boundary == "journal":
            prepare = stack.orders.journal.prepare

            async def closing(*args: Any, **kwargs: Any) -> Any:
                result = await prepare(*args, **kwargs)
                await _fifth_loss(state, stack, fifth)
                return result

            stack.orders.journal.prepare = closing  # type: ignore[method-assign]
        else:
            trade = stack.runtime.trade
            assert trade is not None
            acquire = trade.budget.acquire

            async def waiting(priority: Any, *args: Any, **kwargs: Any) -> Any:
                if priority is Priority.ENTRY:
                    trade.budget.acquire = acquire  # type: ignore[method-assign]
                    await _fifth_loss(state, stack, fifth)
                return await acquire(priority, *args, **kwargs)

            trade.budget.acquire = waiting  # type: ignore[method-assign]
        result = await stack.orders.execute_entry(queued)
        assert (result.outcome, result.reason) == (
            EntryOutcome.RELEASED,
            "daily_loss_limit",
        )
        assert state.fake.orders_of("ETHUSDT", "BUY") == []
        await finish(state, await recover(state))

    matrix(scenario)


def test_an_unrelated_risk_change_defers_the_buy_and_it_goes_next_round() -> None:
    # A cooldown written elsewhere during the journal writes changes the risk
    # version, not this entry's verdict: nothing sent, RESERVED again, sent
    # on the next round.
    async def scenario(state: World) -> None:
        stack = await state.stack()
        queued = await state.reserve(stack, "ETHUSDT")
        prepare = stack.orders.journal.prepare

        async def elsewhere(*args: Any, **kwargs: Any) -> Any:
            result = await prepare(*args, **kwargs)
            async with stack.runtime.session() as session, session.begin():
                session.add(
                    SymbolCooldown(
                        account=CONTEXT.account,
                        network=CONTEXT.network,
                        symbol="SOLUSDT",
                        cooldown_until=state.clock.now(),
                        position_id=uuid.uuid4(),
                    )
                )
            return result

        stack.orders.journal.prepare = elsewhere  # type: ignore[method-assign]
        result = await stack.orders.execute_entry(queued)
        assert result.outcome is EntryOutcome.DEFERRED
        assert state.fake.orders_of("ETHUSDT", "BUY") == []
        assert (await state.intent(stack, queued)).state == IntentState.RESERVED
        stack.orders.journal.prepare = prepare  # type: ignore[method-assign]
        assert (await stack.orders.execute_entry(queued)).outcome is EntryOutcome.FILLED
        await finish(state, await recover(state))

    matrix(scenario)


def test_a_risk_change_still_open_defers_the_buy() -> None:
    # A close flushed but not committed is invisible to the gate's read: the
    # BUY waits for it instead of reading around it.
    async def scenario(state: World) -> None:
        stack = await state.stack()
        queued = await state.reserve(stack, "ETHUSDT")
        session = stack.runtime.session()
        await session.begin()
        session.add(
            SymbolCooldown(
                account=CONTEXT.account,
                network=CONTEXT.network,
                symbol="SOLUSDT",
                cooldown_until=state.clock.now(),
                position_id=uuid.uuid4(),
            )
        )
        await session.flush()
        result = await stack.orders.execute_entry(queued)
        assert (result.outcome, result.reason) == (
            EntryOutcome.DEFERRED,
            "risk change in progress",
        )
        await session.commit()
        await session.close()
        assert (await stack.orders.execute_entry(queued)).outcome is EntryOutcome.FILLED
        await finish(state, await recover(state))

    matrix(scenario)


# --- the settings the gate read are the settings at the send (review THIRD, R3) ------------


def _at_boundary(stack: Stack, boundary: str, change: Callable[[], None]) -> None:
    if boundary == "journal":
        prepare = stack.orders.journal.prepare

        async def changed(*args: Any, **kwargs: Any) -> Any:
            result = await prepare(*args, **kwargs)
            change()
            return result

        stack.orders.journal.prepare = changed  # type: ignore[method-assign]
    else:
        trade = stack.runtime.trade
        assert trade is not None
        acquire = trade.budget.acquire

        async def waiting(priority: Any, *args: Any, **kwargs: Any) -> Any:
            if priority is Priority.ENTRY:
                trade.budget.acquire = acquire  # type: ignore[method-assign]
                change()
            return await acquire(priority, *args, **kwargs)

        trade.budget.acquire = waiting  # type: ignore[method-assign]


@pytest.mark.parametrize("boundary", ["journal", "budget"])
def test_lowering_the_daily_limit_stops_a_reserved_buy(boundary: str) -> None:
    # One loss, limit 5: ETH passes the gate. The owner sets the limit to 1
    # (execution_configure assigns the settings) before the BUY goes out.
    async def scenario(state: World) -> None:
        stack = await state.stack()
        await _losing_close(state, stack)
        queued = await state.reserve(stack, "ETHUSDT")
        runtime = stack.runtime
        stricter = runtime.settings.model_copy(update={"daily_losing_trades_limit": 1})
        _at_boundary(stack, boundary, lambda: setattr(runtime, "settings", stricter))
        result = await stack.orders.execute_entry(queued)
        assert (result.outcome, result.reason) == (
            EntryOutcome.RELEASED,
            "daily_loss_limit",
        )
        assert state.fake.orders_of("ETHUSDT", "BUY") == []
        await finish(state, await recover(state))

    matrix(scenario)


@pytest.mark.parametrize("change", ["same", "raised"])
def test_unchanged_or_raised_settings_never_block_an_entry(change: str) -> None:
    # The same settings again: nothing changes, the BUY goes now. A raised
    # limit is a change: the BUY is deferred, then goes on the next round.
    async def scenario(state: World) -> None:
        stack = await state.stack()
        queued = await state.reserve(stack, "ETHUSDT")
        runtime = stack.runtime
        current = runtime.settings
        new = (
            current.model_copy()
            if change == "same"
            else current.model_copy(
                update={
                    "daily_losing_trades_limit": current.daily_losing_trades_limit + 1
                }
            )
        )
        _at_boundary(stack, "journal", lambda: setattr(runtime, "settings", new))
        result = await stack.orders.execute_entry(queued)
        if change == "same":
            assert result.outcome is EntryOutcome.FILLED
        else:
            assert result.outcome is EntryOutcome.DEFERRED
            assert state.fake.orders_of("ETHUSDT", "BUY") == []
            assert (
                await stack.orders.execute_entry(queued)
            ).outcome is EntryOutcome.FILLED
        await finish(state, await recover(state))

    matrix(scenario)
