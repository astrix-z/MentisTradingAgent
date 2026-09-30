"""EXEC-12: the fake exchange behaves like the exchange where execution relies on it.

Every call goes through the production :class:`TradeClient`, so these are also
the reference examples of each scripted scenario.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any, cast

import pytest
from fake_exchange import FakeExchange, SymbolRules

from agent.exchange.budget import Priority, RequestBudget
from agent.exchange.trade import Outcome, TradeCallError, TradeClient

BTC = SymbolRules(symbol="BTCUSDT", base="BTC")


def exchange(**kwargs: object) -> FakeExchange:
    fake = FakeExchange(balances={"USDT": Decimal(1000), "BNB": Decimal(1)}, **kwargs)  # type: ignore[arg-type]
    fake.list_symbol(BTC, Decimal(100))
    return fake


def client(fake: FakeExchange) -> TradeClient:
    """A new "runtime": fresh client and budget, same exchange underneath."""
    return TradeClient("testnet", fake.spot(), budget=RequestBudget())


async def buy(trade: TradeClient, cid: str = "mta-s-e1", qty: str = "0.1") -> object:
    return await trade.new_order(
        symbol="BTCUSDT",
        side="BUY",
        order_type="LIMIT",
        quantity=Decimal(qty),
        price=Decimal("100.3"),
        time_in_force="FOK",
        client_order_id=cid,
        priority=Priority.ENTRY,
    )


async def protect(
    trade: TradeClient, n: int = 1, qty: str = "0.1", limit: str | None = None
) -> object:
    return await trade.order_list_oco(
        symbol="BTCUSDT",
        quantity=Decimal(qty),
        take_profit=Decimal(110),
        stop=Decimal(99),
        stop_limit_price=Decimal(limit) if limit else None,
        list_client_order_id=f"mta-s-pl{n}",
        above_client_order_id=f"mta-s-pt{n}",
        below_client_order_id=f"mta-s-ps{n}",
    )


async def sell(trade: TradeClient, qty: str = "0.1", cid: str = "mta-s-x1") -> object:
    return await trade.new_order(
        symbol="BTCUSDT",
        side="SELL",
        order_type="MARKET",
        quantity=Decimal(qty),
        client_order_id=cid,
        priority=Priority.PROTECT,
    )


def test_fok_fills_at_last_and_expires_without_fill() -> None:
    async def scenario() -> None:
        fake = exchange()
        trade = client(fake)
        filled = (await buy(trade)).data  # type: ignore[attr-defined]
        assert filled.status == "FILLED" and filled.executed_qty == "0.10000000"
        assert filled.fills and filled.fills[0].commission_asset == "BNB"
        assert fake.balance("BTC") == (Decimal("0.1"), Decimal(0))
        fake.fok_outcome = "expire"
        expired = (await buy(trade, "mta-s-e2")).data  # type: ignore[attr-defined]
        assert expired.status == "EXPIRED" and expired.executed_qty == "0.00000000"

    asyncio.run(scenario())


def test_sell_of_an_asset_locked_under_oco_is_insufficient_balance() -> None:
    async def scenario() -> None:
        fake = exchange()
        trade = client(fake)
        await buy(trade)
        await protect(trade)
        assert fake.balance("BTC") == (Decimal(0), Decimal("0.1"))
        with pytest.raises(TradeCallError) as caught:
            await sell(trade)
        assert caught.value.outcome is Outcome.REJECTED
        assert caught.value.exchange_code == -2010

    asyncio.run(scenario())


def test_accepted_buy_outlives_a_cancelled_wait_and_a_new_runtime() -> None:
    """BUY accepted → wait cancelled → new runtime → not-found → the same order."""

    async def scenario() -> None:
        fake = exchange()
        entered, release = fake.gate("new_order")
        first = client(fake)
        waiting = asyncio.create_task(buy(first))
        await asyncio.to_thread(entered.wait, 5)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        # The old runtime is gone; the request is still inside the exchange.
        second = client(fake)
        fake.fault("get_order", "not_found")
        release.set()
        with pytest.raises(TradeCallError) as missing:
            await second.get_order(symbol="BTCUSDT", client_order_id="mta-s-e1")
        assert missing.value.exchange_code == -2013
        found = None
        for _ in range(50):
            try:
                found = (
                    await second.get_order(symbol="BTCUSDT", client_order_id="mta-s-e1")
                ).data
                break
            except TradeCallError:
                await asyncio.sleep(0.01)
        assert found is not None and found.status == "FILLED"
        assert len(fake.orders_of("BTCUSDT", "BUY")) == 1

    asyncio.run(scenario())


def test_timeout_after_accept_is_unknown_but_the_order_exists() -> None:
    async def scenario() -> None:
        fake = exchange()
        trade = client(fake)
        fake.fault("new_order", "timeout_after")
        with pytest.raises(TradeCallError) as caught:
            await buy(trade)
        assert caught.value.outcome is Outcome.UNKNOWN
        assert [o.status for o in fake.orders_of("BTCUSDT", "BUY")] == ["FILLED"]
        fake.fault("new_order", "timeout_before")
        with pytest.raises(TradeCallError) as lost:
            await buy(trade, "mta-s-e2")
        assert lost.value.outcome is Outcome.UNKNOWN
        assert len(fake.orders_of("BTCUSDT", "BUY")) == 1

    asyncio.run(scenario())


def test_server_error_after_accept_and_scripted_refusals() -> None:
    async def scenario() -> None:
        fake = exchange()
        trade = client(fake)
        fake.fault("order_list_oco", "reject", code=-1013)
        await buy(trade)
        with pytest.raises(TradeCallError) as rejected:
            await protect(trade)
        assert rejected.value.outcome is Outcome.REJECTED
        assert rejected.value.exchange_code == -1013
        fake.fault("order_list_oco", "server_error_after")
        with pytest.raises(TradeCallError) as unknown:
            await protect(trade)
        assert unknown.value.outcome is Outcome.UNKNOWN
        assert fake.lists and fake.balance("BTC")[1] == Decimal("0.1")

    asyncio.run(scenario())


def test_rate_limit_and_ban_are_scriptable() -> None:
    async def scenario() -> None:
        fake = exchange()
        trade = client(fake)
        fake.fault("get_order", "rate_limit", retry_after=3)
        with pytest.raises(TradeCallError) as limited:
            await trade.get_order(symbol="BTCUSDT", client_order_id="x")
        assert limited.value.outcome is Outcome.RATE_LIMITED
        assert trade.budget.paused()
        other = client(fake)
        fake.fault("get_order", "ban", retry_after=60)
        with pytest.raises(TradeCallError) as banned:
            await other.get_order(symbol="BTCUSDT", client_order_id="x")
        assert banned.value.outcome is Outcome.BANNED and other.budget.banned()

    asyncio.run(scenario())


def test_response_without_fills_while_history_has_them() -> None:
    async def scenario() -> None:
        fake = exchange()
        trade = client(fake)
        fake.fault("new_order", "no_fills")
        placed = (await buy(trade)).data  # type: ignore[attr-defined]
        assert placed.status == "FILLED" and not placed.fills
        trades = (await trade.my_trades(symbol="BTCUSDT", from_id=0)).data
        assert [t.qty for t in trades] == ["0.10000000"]

    asyncio.run(scenario())


def test_history_can_lag_the_order_status() -> None:
    async def scenario() -> None:
        fake = exchange()
        trade = client(fake)
        await buy(trade)
        order_id = fake.orders_of("BTCUSDT", "BUY")[0].order_id
        fake.hide_trades(order_id)
        status = (
            await trade.get_order(symbol="BTCUSDT", client_order_id="mta-s-e1")
        ).data
        assert status.status == "FILLED"
        assert (await trade.my_trades(symbol="BTCUSDT", from_id=0)).data == []
        fake.reveal_trades()
        assert len((await trade.my_trades(symbol="BTCUSDT", from_id=0)).data) == 1

    asyncio.run(scenario())


def test_server_side_fills_survive_the_runtime_that_placed_them() -> None:
    async def scenario() -> None:
        fake = exchange(commission_asset="quote")
        placing = client(fake)
        await buy(placing)
        await protect(placing)
        del placing  # the runtime dies; the OCO stays on the exchange
        fake.set_price("BTCUSDT", Decimal(111))  # TP leg fills while nobody watches
        later = client(fake)
        order_list = (await later.get_order_list(list_client_order_id="mta-s-pl1")).data
        assert order_list.list_order_status == "ALL_DONE"
        statuses = {
            o.client_order_id: o.status for o in fake.orders_of("BTCUSDT", "SELL")
        }
        assert statuses == {"mta-s-pt1": "FILLED", "mta-s-ps1": "EXPIRED"}
        assert fake.balance("BTC") == (Decimal(0), Decimal(0))
        trades = (await later.my_trades(symbol="BTCUSDT", from_id=0)).data
        assert [t.is_buyer for t in trades] == [True, False]
        assert trades[1].commission_asset == "USDT"

    asyncio.run(scenario())


def test_market_stop_fills_at_the_gap_price_and_stop_limit_does_not() -> None:
    async def scenario() -> None:
        fake = exchange()
        trade = client(fake)
        await buy(trade)
        await protect(trade)
        fake.set_price("BTCUSDT", Decimal(95))  # gaps through 99
        stop = fake._by_client_id("mta-s-ps1")
        assert stop is not None and stop.status == "FILLED"
        assert stop.quote == Decimal("9.5")

        fake2 = exchange()
        trade2 = client(fake2)
        await buy(trade2)
        await protect(trade2, limit="98.5")
        fake2.set_price("BTCUSDT", Decimal(95))  # triggered, limit not reachable
        leg = fake2._by_client_id("mta-s-ps1")
        assert leg is not None and leg.triggered and leg.status == "NEW"
        fake2.set_price("BTCUSDT", Decimal(98.6))
        assert leg.status == "FILLED"

    asyncio.run(scenario())


def test_partial_fill_during_cancel_leaves_a_remainder() -> None:
    async def scenario() -> None:
        fake = exchange()
        trade = client(fake)
        await buy(trade)
        await protect(trade)
        fake.fill_leg("mta-s-pt1", Decimal("0.04"), Decimal(110))
        stop = fake._by_client_id("mta-s-ps1")
        assert stop is not None and stop.status == "EXPIRED"  # OCO: sibling goes
        cancelled = (
            await trade.delete_order_list(
                symbol="BTCUSDT", list_client_order_id="mta-s-pl1"
            )
        ).data
        reports = {r.client_order_id: r.status for r in cancelled.order_reports or []}
        assert reports == {"mta-s-pt1": "CANCELED", "mta-s-ps1": "EXPIRED"}
        assert fake.balance("BTC") == (Decimal("0.06"), Decimal(0))
        leg = fake._by_client_id("mta-s-pt1")
        assert leg is not None and leg.executed == Decimal("0.04")
        with pytest.raises(TradeCallError) as again:
            await trade.delete_order_list(
                symbol="BTCUSDT", list_client_order_id="mta-s-pl1"
            )
        assert again.value.exchange_code == -2011

    asyncio.run(scenario())


def test_terminal_partial_market_sell() -> None:
    async def scenario() -> None:
        fake = exchange()
        trade = client(fake)
        await buy(trade)
        fake.market_fill_ratio = Decimal("0.5")
        result = (await sell(trade)).data  # type: ignore[attr-defined]
        assert result.status == "EXPIRED" and result.executed_qty == "0.05000000"

    asyncio.run(scenario())


def test_client_id_is_unique_only_among_open_orders() -> None:
    """Why UNKNOWN must never be retried: after FILLED the same id buys again."""

    async def scenario() -> None:
        fake = exchange()
        trade = client(fake)
        await buy(trade)
        await buy(trade)  # same client id, previous one FILLED → accepted
        assert len(fake.orders_of("BTCUSDT", "BUY")) == 2
        await protect(trade, n=1, qty="0.1")
        with pytest.raises(TradeCallError) as duplicate:
            await protect(trade, n=1, qty="0.1")
        assert duplicate.value.outcome is Outcome.REJECTED

    asyncio.run(scenario())


def test_market_data_answers_with_sdk_models() -> None:
    async def scenario() -> None:
        fake = exchange()
        spot = fake.spot()
        info = spot.rest_api.exchange_info(symbol="BTCUSDT").data()
        filters = [f.actual_instance.filter_type for f in info.symbols[0].filters]  # type: ignore[index, union-attr]
        assert "PERCENT_PRICE_BY_SIDE" in filters and "NOTIONAL" in filters
        avg = spot.rest_api.avg_price(symbol="BTCUSDT").data()
        assert avg.mins == 5 and avg.price == "100.00000000"
        ref = spot.rest_api.reference_price(symbol="BTCUSDT").data()
        assert ref.reference_price is None
        # The SDK annotates this as one model but returns a list at runtime.
        last = cast(
            list[Any], spot.rest_api.get_trades(symbol="BTCUSDT", limit=1).data()
        )
        assert last[0].price == "100.00000000"

    asyncio.run(scenario())


def test_partial_tp_then_price_through_the_stop_sells_nothing_more() -> None:
    # Review 2026-09-30 (EXEC-12): TP filled 0.04 of 0.1, then the price fell
    # through the stop: the stop sold the full 0.10 (0.14 sold, locked -0.04).
    async def scenario() -> None:
        fake = exchange()
        trade = client(fake)
        await buy(trade)
        await protect(trade)
        fake.fill_leg("mta-s-pt1", Decimal("0.04"), Decimal(110))
        fake.set_price("BTCUSDT", Decimal(80))
        sold = sum(o.executed for o in fake.orders_of("BTCUSDT", "SELL"))
        assert sold == Decimal("0.04")
        free, locked = fake.balance("BTC")
        assert locked == Decimal("0.06") and free == Decimal(0)
        [order_list] = fake.lists.values()
        assert order_list.status == "EXECUTING"  # TP still open for 0.06
        fake.fill_leg("mta-s-pt1", Decimal("0.06"), Decimal(110))
        assert order_list.status == "ALL_DONE"
        assert fake.balance("BTC") == (Decimal(0), Decimal(0))

    asyncio.run(scenario())
