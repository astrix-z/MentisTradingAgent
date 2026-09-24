"""EXEC-14: QuoteProvider — quotes before the first position, and what the
validator makes of them (plan §9.8)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fake_exchange import FakeExchange, SymbolRules

from agent.exchange.budget import RequestBudget
from agent.exchange.rest import ExchangeRest
from agent.execution.filters import FilterCache
from agent.execution.quotes import QuoteProvider, QuoteSnapshot, ReferenceUnverified
from agent.execution.sizing import EntryRejected, SizingParams, Skip, plan_entry
from agent.strategies.base import EXIT_POLICY_VERSION, ExitPolicy

PRICE = Decimal("84448.01")


class Clock:
    """Exchange time in ms, shared by the fake exchange and the provider."""

    def __init__(self) -> None:
        self.ms = 1_800_000_000_000

    def __call__(self) -> int:
        return self.ms

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.ms / 1000, UTC)


def setup(**rules: Any) -> tuple[FakeExchange, QuoteProvider, Clock, ExchangeRest]:
    clock = Clock()
    fake = FakeExchange(clock=clock)
    fake.list_symbol(
        SymbolRules(symbol="BTCUSDT", base="BTC", step=Decimal("0.00001"), **rules),
        PRICE,
    )
    rest = ExchangeRest("live", fake.spot(), RequestBudget())
    return fake, QuoteProvider(rest, clock=clock.now), clock, rest


def policy() -> ExitPolicy:
    swing = Decimal("83000.00")
    return ExitPolicy(
        version=EXIT_POLICY_VERSION,
        stop_ref=swing,
        swing_low_ref=swing,
        min_stop_distance_pct=Decimal(1),
        stop_buffer_pct=Decimal("0.1"),
        tp_pct=Decimal(10),
        trail_interval="15m",
        trail_n=2,
        trail_cap_pct=Decimal(4),
        max_hold_hours=Decimal(8),
        cooldown_minutes=60,
    )


async def plan(rest: ExchangeRest, snapshot: QuoteSnapshot, now: datetime) -> Any:
    async def fetch(network: str, symbol: str) -> Any:
        return await rest.exchange_info(symbol)

    filters = await FilterCache(fetch).get("live", "BTCUSDT")
    return plan_entry(
        filters=filters,
        quote=snapshot,
        now=now,
        params=SizingParams(),
        entry_ref=PRICE,
        policy=policy(),
        total_equity=Decimal("13.86"),
        free_quote=Decimal("13.86"),
        reserved_quote=Decimal(0),
        occupied_slots=0,
    )


def test_first_entry_with_an_empty_portfolio_gets_a_usable_quote() -> None:
    async def scenario() -> None:
        fake, provider, clock, rest = setup()
        assert not fake.orders  # nothing held, nothing subscribed
        snapshot = await provider.snapshot("BTCUSDT")
        assert snapshot.last_trade is not None and snapshot.last_trade.price == PRICE
        assert snapshot.avg is not None and snapshot.avg.mins == 5
        assert snapshot.reference is not None and snapshot.reference.price is None
        snapshot.check_fresh(clock.now())
        result = await plan(rest, snapshot, clock.now())
        assert result.quantity == Decimal("0.00015")
        called = {name for name, _ in fake.calls}
        assert {
            "get_trades",
            "avg_price",
            "reference_price",
            "ticker_book_ticker",
        } <= called

    asyncio.run(scenario())


def test_average_over_another_window_fails_the_entry_closed() -> None:
    async def scenario() -> None:
        fake, provider, clock, rest = setup()
        fake.avg["BTCUSDT"] = (PRICE, 15)
        with pytest.raises(EntryRejected) as caught:
            await plan(rest, await provider.snapshot("BTCUSDT"), clock.now())
        assert caught.value.reason is Skip.REFERENCE_UNVERIFIED

    asyncio.run(scenario())


def test_filters_with_zero_window_use_the_last_trade() -> None:
    async def scenario() -> None:
        fake, provider, clock, rest = setup(avg_price_mins=0)
        fake.avg["BTCUSDT"] = (Decimal(1), 5)  # would break the entry if used
        result = await plan(rest, await provider.snapshot("BTCUSDT"), clock.now())
        assert result.cap_price == Decimal("84701.35")

    asyncio.run(scenario())


def test_a_non_null_reference_price_is_the_one_used() -> None:
    async def scenario() -> None:
        fake, provider, clock, rest = setup()
        fake.reference["BTCUSDT"] = Decimal(10000)  # entry > 5 × reference
        snapshot = await provider.snapshot("BTCUSDT")
        assert snapshot.reference is not None
        assert snapshot.reference.price == Decimal(10000)
        with pytest.raises(EntryRejected) as caught:
            await plan(rest, snapshot, clock.now())
        assert caught.value.reason is Skip.FILTER_VIOLATION

    asyncio.run(scenario())


@pytest.mark.parametrize("method", ["get_trades", "avg_price", "reference_price"])
def test_a_missing_part_leaves_the_snapshot_unverifiable(method: str) -> None:
    async def scenario() -> None:
        fake, provider, clock, rest = setup()
        fake.fault(method, "timeout_before")
        snapshot = await provider.snapshot("BTCUSDT")
        with pytest.raises((EntryRejected, ReferenceUnverified)):
            snapshot.check_fresh(clock.now())
            await plan(rest, snapshot, clock.now())

    asyncio.run(scenario())


def test_stale_parts_are_refused() -> None:
    async def scenario() -> None:
        fake, provider, clock, rest = setup()
        snapshot = await provider.snapshot("BTCUSDT")
        later = clock.now() + timedelta(seconds=6)
        with pytest.raises(ReferenceUnverified, match="fetched"):
            snapshot.check_fresh(later)
        clock.ms += 61_000  # no trade for a minute; a new fetch is still stale
        fresh_fetch = await provider.snapshot("BTCUSDT")
        with pytest.raises(ReferenceUnverified, match="stale"):
            fresh_fetch.check_fresh(clock.now())
        stale_reference = replace(
            fresh_fetch,
            last_trade=replace(fresh_fetch.last_trade, time=clock.now()),  # type: ignore[type-var]
        )
        with pytest.raises(ReferenceUnverified, match="stale"):
            stale_reference.reference_for(5, clock.now() + timedelta(seconds=120))

    asyncio.run(scenario())


def test_spread_is_recorded_for_the_audit() -> None:
    async def scenario() -> None:
        fake, provider, _, _ = setup()
        fake.book["BTCUSDT"] = (Decimal("84448.00"), Decimal("84448.02"))
        snapshot = await provider.snapshot("BTCUSDT")
        assert snapshot.bid == Decimal("84448.00") and snapshot.ask == Decimal(
            "84448.02"
        )
        assert snapshot.spread_pct is not None and snapshot.spread_pct < Decimal(
            "0.0001"
        )

    asyncio.run(scenario())


def test_no_budget_means_no_quote_not_a_wrong_one() -> None:
    async def scenario() -> None:
        fake, _, clock, _ = setup()
        budget = RequestBudget()
        budget.on_banned(60)
        rest = ExchangeRest("live", fake.spot(), budget)
        snapshot = await QuoteProvider(rest, clock=clock.now).snapshot("BTCUSDT")
        assert snapshot.last_trade is None and snapshot.avg is None
        assert fake.calls == []

    asyncio.run(scenario())
