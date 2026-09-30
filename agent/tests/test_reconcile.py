"""EXEC-11a: reconciliation by trades — pages, cursor, crashes, quarantine."""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from typing import Any

import pytest
from execution_world import CONTEXT, Stack, World, run
from sqlalchemy import func, select

from agent.exchange.budget import RequestBudget
from agent.exchange.trade import TradeClient
from agent.execution.admission import record_signal
from agent.execution.ledger import FillRecord, lock_position
from agent.execution.models import Fill, PositionStatus, SymbolCooldown, TradeCursor
from agent.execution.reconcile import Reconciler


def reconciler(stack: Stack, page: int = 1000) -> Reconciler:
    async def price(symbol: str) -> Decimal | None:
        return Decimal(100)

    return Reconciler(stack.worker, prices=price, page=page)


async def fills_of(stack: Stack, position_id: uuid.UUID) -> int:
    async with stack.runtime.session() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(Fill)
            .where(Fill.position_id == position_id)
        )
    return int(count or 0)


async def cursor(stack: Stack, symbol: str = "BTCUSDT") -> int | None:
    async with stack.runtime.session() as session:
        value = await session.scalar(
            select(TradeCursor.last_trade_id).where(TradeCursor.symbol == symbol)
        )
    return int(value) if value is not None else None


async def partial_fills(
    state: World, stack: Stack, position_id: uuid.UUID, count: int
) -> None:
    """Take-profit leg fills in small pieces while nobody books them."""
    position = await state.position(stack, position_id)
    assert position.protection_list_id is not None
    take_profit = position.protection_list_id.replace("pl", "pt")
    for _ in range(count):
        state.fake.fill_leg(take_profit, Decimal("0.001"), Decimal(110))


def test_many_pages_are_booked_once_and_the_cursor_follows() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.protected(stack)
        await partial_fills(state, stack, position_id, 5)
        report = await reconciler(stack, page=2).run()
        assert report.fills == 5
        assert await fills_of(stack, position_id) == 6  # the entry + 5 pieces
        assert await cursor(stack) == max(t["id"] for t in state.fake.trades)
        again = await reconciler(stack, page=2).run()
        assert again.fills == 0  # the inclusive boundary trade is deduplicated
        position = await state.position(stack, position_id)
        assert position.sold_qty == Decimal("0.005")

    run(scenario)


def test_same_trade_id_on_two_symbols_is_two_fills() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        btc = await state.protected(stack, "BTCUSDT")
        eth = await state.protected(stack, "ETHUSDT")
        for position_id, symbol in ((btc, "BTCUSDT"), (eth, "ETHUSDT")):
            async with stack.runtime.session() as session, session.begin():
                position = await lock_position(session, position_id)
                result = await stack.applier.apply(
                    session,
                    position,
                    [
                        FillRecord(
                            symbol=symbol,
                            trade_id=987654,
                            order_id=1,
                            side="SELL",
                            price=Decimal(110),
                            quantity=Decimal("0.001"),
                            quote_quantity=Decimal("0.11"),
                            commission=Decimal(0),
                            commission_asset="BNB",
                            is_maker=True,
                            time=state.clock.now(),
                            source="test",
                        )
                    ],
                )
                assert len(result.new_fills) == 1

    run(scenario)


def test_crash_while_booking_a_page_loses_nothing_and_doubles_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.protected(stack)
        await partial_fills(state, stack, position_id, 3)
        original = stack.applier.apply
        calls = {"n": 0}

        async def crash(*args: Any, **kwargs: Any) -> Any:
            calls["n"] += 1
            await original(*args, **kwargs)
            raise RuntimeError("process died before the commit")

        monkeypatch.setattr(stack.applier, "apply", crash)
        with pytest.raises(RuntimeError):
            await reconciler(stack).sync_trades(stack.runtime.context, "BTCUSDT")  # type: ignore[arg-type]
        assert await fills_of(stack, position_id) == 1 and await cursor(stack) is None
        monkeypatch.setattr(stack.applier, "apply", original)
        await reconciler(stack).run()
        assert await fills_of(stack, position_id) == 4
        # A cursor that lags (crash after the fills, before anything else):
        async with stack.runtime.session() as session, session.begin():
            row = (await session.scalars(select(TradeCursor))).one()
            row.last_trade_id = 0
        assert (await reconciler(stack).run()).fills == 0
        assert (await state.position(stack, position_id)).sold_qty == Decimal("0.003")

    run(scenario)


def test_reconcile_racing_the_cancel_protocol_books_each_fill_once() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.protected(stack)
        await partial_fills(state, stack, position_id, 2)
        await asyncio.gather(
            reconciler(stack).run(), stack.worker.reprice(position_id, Decimal("99.5"))
        )
        position = await state.position(stack, position_id)
        assert position.sold_qty == Decimal("0.002")
        assert await fills_of(stack, position_id) == 3

    run(scenario)


def test_list_that_filled_during_downtime_closes_the_position_once() -> None:
    async def scenario(state: World) -> None:
        first = await state.stack()
        position_id = await state.protected(first)
        await state.kill(first)
        state.fake.set_price("BTCUSDT", Decimal(111))  # the take-profit executes
        fill_time = state.clock.now()
        state.clock.advance(600)
        second = await state.stack()
        report = await reconciler(second).run()
        assert report.closed == ["BTCUSDT"]
        position = await state.position(second, position_id)
        assert position.status == PositionStatus.CLOSED and position.outcome is not None
        assert (position.outcome["reason"], position.outcome["mechanism"]) == (
            "tp",
            "exchange_tp",
        )
        async with second.runtime.session() as session:
            cooldown = (await session.scalars(select(SymbolCooldown))).one()
        # 60 minutes from the fill, not from when the agent noticed.
        assert abs((cooldown.cooldown_until - fill_time).total_seconds() - 3600) < 5
        assert (await reconciler(second).run()).closed == []

    run(scenario)


def test_a_list_gone_with_a_remainder_goes_back_to_protection() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.protected(stack)
        list_id = (await state.position(stack, position_id)).protection_list_id
        outside = TradeClient("live", state.fake.spot(), budget=RequestBudget())
        await outside.delete_order_list(symbol="BTCUSDT", list_client_order_id=list_id)  # type: ignore[arg-type]
        report = await reconciler(stack).run()
        assert report.unprotected == ["BTCUSDT"]
        position = await state.position(stack, position_id)
        # Held by the worker until the old legs are proven, then replaced.
        assert position.operation == "reprice"
        assert position.op_state is not None
        assert position.op_state["list"] == list_id
        assert position.unprotected_since is not None
        assert (await stack.worker.resume(position_id)).step.value == "protected"

    run(scenario)


def test_unknown_orders_are_quarantined_or_divergent_but_never_touched() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        await state.protected(stack)
        state.fake.free["BTC"] += Decimal("0.2")  # bought by hand: not ours
        outside = TradeClient("live", state.fake.spot(), budget=RequestBudget())
        await outside.order_list_oco(
            symbol="BTCUSDT",
            quantity=Decimal("0.1"),
            take_profit=Decimal(120),
            stop=Decimal(90),
            stop_limit_price=None,
            list_client_order_id="mta-ffffffffffffffffffffffff-pl9",
            above_client_order_id="mta-ffffffffffffffffffffffff-pt9",
            below_client_order_id="manual-stop-1",
        )
        state.fake.free["SOL"] = Decimal(1)
        report = await reconciler(stack).run()
        assert report.quarantined == ["mta-ffffffffffffffffffffffff-pt9"]
        assert "manual-stop-1" in report.divergence and "SOLUSDT" in report.divergence
        assert any(key.startswith("quarantine:") for key in stack.runtime.alerts)
        assert (
            len([ol for ol in state.fake.lists.values() if ol.status == "EXECUTING"])
            == 2
        )

    run(scenario)


def test_divergence_closes_admission_until_a_check_no_longer_finds_it() -> None:
    # Review 2026-09-30 (EXEC-06): an unexplained SOL balance was reported,
    # yet a following ETH ENTRY was admitted and bought.
    async def scenario(state: World) -> None:
        stack = await state.stack()
        await state.protected(stack)
        state.fake.free["SOL"] = Decimal(1)  # 100 USDT, no position explains it
        report = await reconciler(stack).run()
        assert report.divergence == ["SOLUSDT"]
        assert stack.runtime.admission_block() == "divergence"
        signal = state.entry("ETHUSDT")
        async with stack.runtime.session() as session:
            await record_signal(
                session, signal, context=CONTEXT, strategy="tripple_screen"
            )
        [decision] = await stack.admission.drain()
        assert decision.reason == "divergence"
        assert state.fake.orders_of("ETHUSDT", "BUY") == []

        del state.fake.free["SOL"]  # the owner sold it
        report = await reconciler(stack).run()
        assert report.divergence == []
        assert not any(k.startswith("divergence:") for k in stack.runtime.alerts)
        assert stack.runtime.admission_block() is None
        await state.reserve(stack, "ETHUSDT")

    run(scenario)


def test_an_unread_check_keeps_the_previous_finding() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        await state.protected(stack)
        state.fake.free["SOL"] = Decimal(1)
        await reconciler(stack).run()
        state.fake.fault("get_account", "timeout_before")
        await reconciler(stack).run()
        assert "divergence:balance:SOLUSDT" in stack.runtime.alerts
        assert stack.runtime.admission_block() == "divergence"

    run(scenario)


def test_finished_list_with_hidden_leg_fills_blocks_the_replacement() -> None:
    # Review 2026-09-30 (EXEC-11): TP filled 0.1, the list was cancelled and
    # the leg's trades were not visible yet. The replacement used to be sized
    # from the full quantity against a smaller free balance.
    async def scenario(state: World) -> None:
        stack = await state.stack()
        position_id = await state.protected(stack)
        position = await state.position(stack, position_id)
        list_id = position.protection_list_id
        assert list_id is not None and position.take_profit is not None
        before = position.quantity
        take_profit = list_id.replace("-pl", "-pt")
        state.fake.fill_leg(take_profit, Decimal("0.1"), position.take_profit)
        leg = state.fake._by_client_id(take_profit)
        assert leg is not None
        state.fake.hide_trades(leg.order_id)
        outside = TradeClient("live", state.fake.spot(), budget=RequestBudget())
        await outside.delete_order_list(symbol="BTCUSDT", list_client_order_id=list_id)
        sent = state.fake.calls_to("order_list_oco")

        report = await reconciler(stack).run()
        assert report.unprotected == ["BTCUSDT"]
        for _ in range(2):  # neither the protector nor the worker may size it
            assert (await stack.protector.protect(position_id)).step.value == "waiting"
            result = await stack.worker.resume(position_id)
            assert result.step.value == "waiting"
            assert result.reason == "leg fills not proven yet"
        assert state.fake.calls_to("order_list_oco") == sent
        assert [
            o for o in state.fake.orders_of("BTCUSDT", "SELL") if o.type == "MARKET"
        ] == []

        state.fake.reveal_trades()
        assert (await stack.worker.resume(position_id)).step.value == "protected"
        position = await state.position(stack, position_id)
        assert position.quantity == before - Decimal("0.1")
        [live] = [ol for ol in state.fake.lists.values() if ol.status == "EXECUTING"]
        assert live.orders[0].quantity == position.quantity

    run(scenario)
