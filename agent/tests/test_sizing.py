"""EXEC-03: symbol rules, rounding, slots, entry sizing and the leg validator.

Rule fixtures are real ``exchangeInfo`` entries (BTCUSDT, DOGEUSDT, PEPEUSDT,
SENTUSDT, ETHUSDC) saved in ``tests/fixtures/exchange_info.json``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fake_exchange import FakeExchange, SymbolRules

from agent.execution.filters import (
    FilterCache,
    Notional,
    SymbolFilters,
    parse_symbol,
    round_down,
    round_up,
)
from agent.execution.quotes import (
    AveragePrice,
    LastTrade,
    QuoteSnapshot,
    ReferencePrice,
    ReferenceUnverified,
)
from agent.execution.sizing import (
    EntryRejected,
    SizingParams,
    Skip,
    check_symbol,
    equity,
    plan_entry,
    sellable,
    slots_for,
    stop_order_type,
    validate_market_sell,
    validate_protection,
)
from agent.strategies.base import EXIT_POLICY_VERSION, ExitPolicy

NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)
PARAMS = SizingParams()
RAW = json.loads(
    (Path(__file__).parent / "fixtures" / "exchange_info.json").read_text()
)
REAL = {entry["symbol"]: parse_symbol(entry) for entry in RAW["symbols"]}


def quote(
    price: Decimal,
    *,
    mins: int = 5,
    reference: Decimal | None = None,
    age: timedelta = timedelta(0),
) -> QuoteSnapshot:
    at = NOW - age
    return QuoteSnapshot(
        symbol="X",
        network="live",
        fetched_at=NOW,
        last_trade=LastTrade(price, at),
        avg=AveragePrice(price, mins, at),
        reference=ReferencePrice(reference, at),
    )


def policy(swing: Decimal, **changes: Any) -> ExitPolicy:
    values: dict[str, Any] = {
        "version": EXIT_POLICY_VERSION,
        "stop_ref": swing,
        "swing_low_ref": swing,
        "min_stop_distance_pct": Decimal(1),
        "stop_buffer_pct": Decimal("0.1"),
        "tp_pct": Decimal(10),
        "trail_interval": "15m",
        "trail_n": 2,
        "trail_cap_pct": Decimal(4),
        "max_hold_hours": Decimal(8),
        "cooldown_minutes": 60,
    }
    values.update(changes)
    return ExitPolicy(**values)


def plan(
    filters: SymbolFilters,
    price: Decimal,
    total: Decimal,
    *,
    free: Decimal | None = None,
    reserved: Decimal = Decimal(0),
    occupied: int = 0,
    snapshot: QuoteSnapshot | None = None,
    swing: Decimal | None = None,
) -> Any:
    return plan_entry(
        filters=filters,
        quote=snapshot or quote(price),
        now=NOW,
        params=PARAMS,
        entry_ref=price,
        policy=policy(swing if swing is not None else price * Decimal("0.98")),
        total_equity=total,
        free_quote=total if free is None else free,
        reserved_quote=reserved,
        occupied_slots=occupied,
    )


# --- slots (plan §7.1 table) -------------------------------------------------


@pytest.mark.parametrize(
    ("deposit", "slots", "size", "reserve"),
    [
        ("6.99", 0, "0", "0.699"),
        ("7", 1, "6.30", "0.70"),
        ("10", 1, "9.00", "1.00"),
        ("13.86", 1, "12.474", "1.386"),
        ("14", 2, "6.30", "1.40"),
        ("17.50", 2, "7.875", "1.75"),
        ("20.99", 2, "9.4455", "2.099"),
        ("21", 3, "6.30", "2.10"),
        ("28", 3, "8.40", "2.80"),
        ("50", 3, "15.00", "5.00"),
    ],
)
def test_slot_table(deposit: str, slots: int, size: str, reserve: str) -> None:
    result = slots_for(Decimal(deposit), PARAMS)
    assert result.slots == slots
    assert result.size == Decimal(size)
    assert result.reserve == Decimal(reserve)


def test_equity_counts_quote_and_positions_at_last_price() -> None:
    assert equity(
        Decimal(5), Decimal(1), [(Decimal("0.0001"), Decimal(80000))]
    ) == Decimal(14)


def test_rounding() -> None:
    assert round_down(Decimal("84701.359"), Decimal("0.01")) == Decimal("84701.35")
    assert round_up(Decimal("0.00014727"), Decimal("0.00001")) == Decimal("0.00015")
    assert round_up(Decimal("65.52"), Decimal(1)) == Decimal(66)
    assert round_down(Decimal("7"), Decimal(0)) == Decimal(7)


# --- real symbols -------------------------------------------------------------


def test_real_rules_parse_without_unknown_filters() -> None:
    btc = REAL["BTCUSDT"]
    assert btc.tick == Decimal("0.01") and btc.step == Decimal("0.00001")
    assert btc.notional == Notional(Decimal(5), Decimal(9000000), True, False, 5)
    assert btc.percent_by_side is not None and btc.percent_by_side.avg_price_mins == 5
    for filters in REAL.values():
        assert filters.unknown == ()
        assert filters.oco_allowed and "STOP_LOSS" in filters.order_types
        # The live account is in trading group 072, not on SPOT (2026-10-16).
        assert filters.permits(frozenset({"TRD_GRP_072"}))
        assert filters.permits(frozenset({"SPOT"}))
        assert not filters.permits(frozenset({"TRD_GRP_999"}))


@pytest.mark.parametrize(
    ("symbol", "price", "deposit"),
    [
        ("BTCUSDT", "84448.01", "13.86"),
        ("DOGEUSDT", "0.09587", "7"),
        ("PEPEUSDT", "0.00000447", "21"),
        ("SENTUSDT", "0.02141", "28"),
    ],
)
def test_entry_on_real_symbols(symbol: str, price: str, deposit: str) -> None:
    filters = REAL[symbol]
    result = plan(filters, Decimal(price), Decimal(deposit))
    slots = slots_for(Decimal(deposit), PARAMS)
    assert result.cap_price == round_down(
        Decimal(price) * Decimal("1.003"), filters.tick
    )
    # Exact multiples of the exchange steps.
    assert result.quantity % filters.step == 0
    assert result.cap_price % filters.tick == 0
    assert result.stop_estimate % filters.tick == 0
    # One step above the target at most: no growth was needed.
    assert (
        slots.size <= result.quote_cost < slots.size + filters.step * result.cap_price
    )
    assert not result.grew_to_minimum
    assert result.stop_estimate < Decimal(price) < result.take_profit_estimate


def test_btc_at_the_current_deposit() -> None:
    result = plan(REAL["BTCUSDT"], Decimal("84448.01"), Decimal("13.86"))
    assert result.cap_price == Decimal("84701.35")
    assert result.quantity == Decimal("0.00015")
    assert result.quote_cost == Decimal("12.7052025")


def test_usdc_symbol_is_refused_for_execution() -> None:
    with pytest.raises(EntryRejected) as caught:
        plan(REAL["ETHUSDC"], Decimal(3000), Decimal(50))
    assert caught.value.reason is Skip.QUOTE_NOT_SUPPORTED


# --- growth to the minimum and money (Q18, §7.5) ---------------------------


def coarse_btc() -> SymbolFilters:
    """A symbol whose smallest order is worth about 13 USDT."""
    return replace(REAL["BTCUSDT"], step=Decimal("0.0001"), min_qty=Decimal("0.0001"))


def test_growth_to_the_minimum_may_spend_the_reserve() -> None:
    result = plan(coarse_btc(), Decimal(130000), Decimal(14))
    assert result.slots.slots == 2 and result.target_size == Decimal("6.30")
    assert result.quantity == Decimal("0.0001")
    assert result.grew_to_minimum
    assert result.quote_cost == Decimal("13.0390")  # past the 90% share, ≤ free
    assert result.quote_cost > Decimal(14) - result.slots.reserve


def test_growth_stops_at_free_quote_minus_other_reservations() -> None:
    with pytest.raises(EntryRejected) as caught:
        plan(coarse_btc(), Decimal(130000), Decimal(14), reserved=Decimal("6.30"))
    assert caught.value.reason is Skip.INSUFFICIENT_BALANCE


def test_no_slots_below_seven_and_full_slots() -> None:
    with pytest.raises(EntryRejected) as none:
        plan(REAL["BTCUSDT"], Decimal(84000), Decimal("6.99"))
    assert none.value.reason is Skip.NO_SLOTS
    with pytest.raises(EntryRejected) as full:
        plan(REAL["BTCUSDT"], Decimal(84000), Decimal(14), occupied=2)
    assert full.value.reason is Skip.SLOTS_FULL


# --- the validator ------------------------------------------------------------


def simple(**changes: Any) -> SymbolFilters:
    base = SymbolFilters(
        symbol="XUSDT",
        status="TRADING",
        base_asset="X",
        quote_asset="USDT",
        order_types=frozenset({"LIMIT", "LIMIT_MAKER", "MARKET", "STOP_LOSS"}),
        oco_allowed=True,
        tick=Decimal("0.01"),
        min_price=Decimal("0.01"),
        max_price=Decimal(100000),
        step=Decimal(1),
        min_qty=Decimal(1),
        max_qty=Decimal(1000),
        notional=Notional(Decimal(5), Decimal(1000), True, False, 5),
    )
    return replace(base, **changes)


def test_stop_limit_leg_is_checked_at_its_own_price() -> None:
    """qty 1, stop 5, limit 4.95, minNotional 5: only the limit leg is short."""
    problems = validate_protection(
        simple(),
        quote(Decimal(6)),
        NOW,
        PARAMS,
        qty=Decimal(1),
        take_profit=Decimal("6.60"),
        stop=Decimal("5.00"),
        limit_price=Decimal("4.95"),
    )
    assert [p.detail for p in problems] == [
        "stop limit notional 4.95 outside [5, 1000]"
    ]


def test_market_flags_and_max_notional() -> None:
    q = quote(Decimal(4))  # 1 × 4 < 5 at the market reference
    assert validate_market_sell(simple(), q, NOW, PARAMS, qty=Decimal(1))
    lenient = simple(notional=Notional(Decimal(5), Decimal(1000), False, False, 5))
    assert validate_market_sell(lenient, q, NOW, PARAMS, qty=Decimal(1)) == []
    capped = simple(notional=Notional(Decimal(5), Decimal(100), True, True, 5))
    assert validate_market_sell(
        capped, quote(Decimal(200)), NOW, PARAMS, qty=Decimal(1)
    )
    problems = validate_protection(
        simple(),
        quote(Decimal(600)),
        NOW,
        PARAMS,
        qty=Decimal(2),
        take_profit=Decimal(660),
        stop=Decimal(590),
        limit_price=None,
    )
    assert any("notional 1320" in p.detail for p in problems)


def test_prices_off_tick_and_quantities_off_step_are_violations() -> None:
    problems = validate_protection(
        simple(),
        quote(Decimal(10)),
        NOW,
        PARAMS,
        qty=Decimal("1.5"),
        take_profit=Decimal("11.005"),
        stop=Decimal("9.90"),
        limit_price=None,
    )
    details = " ".join(p.detail for p in problems)
    assert "not a multiple of tick" in details and "not a multiple of step" in details


@pytest.mark.parametrize(
    ("snapshot", "fragment"),
    [
        (quote(Decimal(10), mins=15), "over 15 min, filter uses 5"),
        (quote(Decimal(10), age=timedelta(minutes=5)), "stale"),
        (
            replace(quote(Decimal(10)), reference=None),
            "reference price was not fetched",
        ),
        (replace(quote(Decimal(10)), avg=None), "average price was not fetched"),
    ],
    ids=["window-mismatch", "stale", "no-reference", "no-average"],
)
def test_unverifiable_reference_fails_closed(
    snapshot: QuoteSnapshot, fragment: str
) -> None:
    problems = validate_market_sell(simple(), snapshot, NOW, PARAMS, qty=Decimal(1))
    assert problems and problems[0].reason is Skip.REFERENCE_UNVERIFIED
    assert fragment in problems[0].detail


def test_reference_selection_order() -> None:
    q = quote(Decimal(10), reference=Decimal(7))
    assert q.reference_for(5, NOW) == Decimal(7)  # non-null reference wins
    q = quote(Decimal(10))
    assert q.reference_for(5, NOW) == Decimal(10)  # average over the same window
    q = replace(quote(Decimal(10)), avg=AveragePrice(Decimal(9), 15, NOW))
    assert q.reference_for(0, NOW) == Decimal(10)  # avgPriceMins 0 → last trade
    with pytest.raises(ReferenceUnverified):
        q.reference_for(5, NOW)


def test_percent_by_side_bounds_the_entry_price() -> None:
    filters = REAL["BTCUSDT"]
    far = quote(Decimal(84448), reference=Decimal(10000))  # entry > 5 × reference
    with pytest.raises(EntryRejected) as caught:
        plan(filters, Decimal(84448), Decimal(14), snapshot=far)
    assert "entry price" in caught.value.detail


def test_stop_rounds_down_and_fallback_limit_is_below_it() -> None:
    filters = simple(
        order_types=frozenset({"LIMIT", "LIMIT_MAKER", "MARKET", "STOP_LOSS_LIMIT"})
    )
    result = plan(filters, Decimal("10.00"), Decimal(28), swing=Decimal("9.8765"))
    assert result.stop_type == "STOP_LOSS_LIMIT"
    assert result.stop_estimate == Decimal("9.87")
    assert result.stop_limit_estimate == Decimal("9.82")  # 0.5% below, rounded down
    none = simple(order_types=frozenset({"LIMIT", "LIMIT_MAKER", "MARKET"}))
    with pytest.raises(EntryRejected) as caught:
        check_symbol(none, PARAMS)
    assert caught.value.reason is Skip.ORDER_TYPE_NOT_ALLOWED


def test_stop_limit_fallback_still_needs_a_valid_market_exit() -> None:
    # Review 2026-09-30 (EXEC-03): BTC without STOP_LOSS and a MARKET_LOT_SIZE
    # max of 0.0001. The OCO fits as limit orders, but the guard/emergency
    # exit is a MARKET sell of 0.00015 the exchange would refuse.
    filters = replace(
        REAL["BTCUSDT"],
        order_types=REAL["BTCUSDT"].order_types - {"STOP_LOSS"},
        market_max_qty=Decimal("0.00010"),
    )
    assert stop_order_type(filters) == "STOP_LOSS_LIMIT"
    with pytest.raises(EntryRejected) as caught:
        plan(filters, Decimal("84448.01"), Decimal("13.86"))
    assert caught.value.reason is Skip.FILTER_VIOLATION
    assert "market sell" in caught.value.detail
    # The same symbol with a MARKET max that fits is admitted.
    roomy = replace(filters, market_max_qty=Decimal("100"))
    assert plan(roomy, Decimal("84448.01"), Decimal("13.86")).quantity == Decimal(
        "0.00015000"
    )


def test_symbol_level_refusals() -> None:
    for filters, reason in (
        (simple(status="HALT"), Skip.SYMBOL_NOT_TRADING),
        (simple(oco_allowed=False), Skip.OCO_NOT_ALLOWED),
        (simple(unknown=("NEW_RULE",)), Skip.UNKNOWN_FILTER),
    ):
        with pytest.raises(EntryRejected) as caught:
            check_symbol(filters, PARAMS)
        assert caught.value.reason is reason


def test_symbol_permission_sets_against_the_account() -> None:
    """Every set needs one of the account's permissions (Binance semantics)."""
    group = frozenset({"TRD_GRP_072"})
    spot = frozenset({"SPOT"})
    one_set = simple(permission_sets=(frozenset({"SPOT", "TRD_GRP_072"}),))
    check_symbol(one_set, PARAMS, group)
    check_symbol(one_set, PARAMS, spot)
    check_symbol(simple(), PARAMS, group)  # no sets: unrestricted
    check_symbol(
        simple(permission_sets=(frozenset({"SPOT", "TRD_GRP_004"}),)), PARAMS
    )  # no account permissions given: not checked
    for filters in (
        simple(permission_sets=(frozenset({"SPOT", "TRD_GRP_004"}),)),
        simple(
            permission_sets=(
                frozenset({"SPOT", "TRD_GRP_072"}),
                frozenset({"TRD_GRP_005"}),
            )
        ),
    ):
        with pytest.raises(EntryRejected) as caught:
            check_symbol(filters, PARAMS, group)
        assert caught.value.reason is Skip.SYMBOL_NOT_PERMITTED
        assert "TRD_GRP_072" in caught.value.detail


def test_max_position_limits_the_entry() -> None:
    with pytest.raises(EntryRejected) as caught:
        plan(simple(max_position=Decimal(1)), Decimal(10), Decimal(50))
    assert "maxPosition" in caught.value.detail


def test_dust() -> None:
    assert sellable(simple(), Decimal(1), Decimal(5))
    assert not sellable(simple(), Decimal("0.9"), Decimal(10))  # below minQty
    assert not sellable(simple(), Decimal(1), Decimal("4.99"))  # below minNotional


# --- the cache --------------------------------------------------------------


def test_unknown_filter_in_raw_json_fails_closed() -> None:
    entry = json.loads(json.dumps(RAW["symbols"][0]))
    entry["filters"].append({"filterType": "BRAND_NEW_RULE", "x": 1})
    filters = parse_symbol(entry)
    assert filters.unknown == ("BRAND_NEW_RULE",)
    with pytest.raises(EntryRejected):
        check_symbol(filters, PARAMS)


def test_cache_reads_sdk_models_and_raw_json_and_refetches_after_invalidate() -> None:
    fake = FakeExchange()
    fake.list_symbol(SymbolRules(symbol="BTCUSDT", base="BTC"), Decimal(100))
    calls: list[str] = []
    moment = [0.0]

    async def fetch(network: str, symbol: str) -> Any:
        calls.append(symbol)
        if len(calls) == 2:
            return {"symbols": RAW["symbols"]}  # the SDK fell back to raw JSON
        return fake.exchange_info(symbol=symbol).data()

    cache = FilterCache(fetch, ttl=60, clock=lambda: moment[0])

    async def scenario() -> None:
        first = await cache.get("live", "BTCUSDT")
        assert first.tick == Decimal("0.01") and first.unknown == ()
        assert await cache.get("live", "BTCUSDT") is first
        cache.invalidate("live", "BTCUSDT")
        second = await cache.get("live", "BTCUSDT")
        assert second == REAL["BTCUSDT"]
        moment[0] = 61
        await cache.get("live", "BTCUSDT")
        assert len(calls) == 3
        with pytest.raises(LookupError):
            await cache.get("live", "NOPEUSDT")

    asyncio.run(scenario())
