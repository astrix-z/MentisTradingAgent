"""Position size and exchange-rule checks for every leg (plan §7, §6.1–6.2).

Pure functions: rules come in as :class:`SymbolFilters`, prices as a
:class:`QuoteSnapshot`, money as Decimal. Nothing here talks to the exchange
or the database, so the same code serves admission, the last check before a
BUY, and the checks before protection and exits.

Sizing (owner decisions Q1, Q18)::

    slots = min(max_symbols, floor(E / slot_quote))      # E < 7 → no entries
    size  = position_share × E / slots
    qty   = roundUp(max(size / cap, qty_min), step)      # grows to the minimum

Growing to the exchange minimum may spend the 10% reserve; the only ceiling is
the free quote minus what pending and UNKNOWN entries already hold (§7.5).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum

from agent.execution.filters import (
    SymbolFilters,
    is_multiple,
    round_down,
    round_up,
)
from agent.execution.quotes import (
    DEFAULT_MAX_TRADE_AGE,
    QuoteSnapshot,
    ReferenceUnverified,
)
from agent.strategies.base import ExitPolicy

_HUNDRED = Decimal(100)


class Skip(StrEnum):
    """Why an ENTRY was not executed; stored as the intent's reason."""

    QUOTE_NOT_SUPPORTED = "quote_not_supported"
    SYMBOL_NOT_TRADING = "symbol_not_trading"
    SYMBOL_NOT_PERMITTED = "symbol_not_permitted"
    OCO_NOT_ALLOWED = "oco_not_allowed"
    ORDER_TYPE_NOT_ALLOWED = "order_type_not_allowed"
    UNKNOWN_FILTER = "unknown_filter"
    NO_SLOTS = "no_slots"
    SLOTS_FULL = "slots_full"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    REFERENCE_UNVERIFIED = "reference_unverified"
    STOP_ABOVE_PRICE = "stop_above_price"
    FILTER_VIOLATION = "filter_violation"


class EntryRejected(Exception):
    def __init__(self, reason: Skip, detail: str = "") -> None:
        super().__init__(f"{reason.value}: {detail}" if detail else reason.value)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class SizingParams:
    max_symbols: int = 3
    slot_quote: Decimal = Decimal(7)
    position_share: Decimal = Decimal("0.9")
    max_slippage_pct: Decimal = Decimal("0.3")
    stop_limit_gap_pct: Decimal = Decimal("0.5")
    quote_asset: str = "USDT"
    max_trade_age: timedelta = DEFAULT_MAX_TRADE_AGE


@dataclass(frozen=True)
class Slots:
    equity: Decimal
    slots: int
    size: Decimal
    reserve: Decimal


def equity(
    quote_free: Decimal,
    quote_locked: Decimal,
    positions: Iterable[tuple[Decimal, Decimal]] = (),
) -> Decimal:
    """Quote balance plus open positions at their last price (§7.3).

    ``positions`` are ``(ledger quantity, last price)``; BNB and dust are not
    part of it.
    """
    return quote_free + quote_locked + sum((q * p for q, p in positions), Decimal(0))


def slots_for(total: Decimal, params: SizingParams) -> Slots:
    count = min(
        params.max_symbols,
        int((total / params.slot_quote).to_integral_value(rounding=ROUND_FLOOR)),
    )
    count = max(count, 0)
    size = params.position_share * total / count if count else Decimal(0)
    return Slots(
        equity=total,
        slots=count,
        size=size,
        reserve=total - params.position_share * total,
    )


# --- rule checks -----------------------------------------------------------


@dataclass(frozen=True)
class Violation:
    reason: Skip
    detail: str


def _price_rule(filters: SymbolFilters, price: Decimal, label: str) -> list[Violation]:
    problems = []
    if price < filters.min_price or (
        filters.max_price > 0 and price > filters.max_price
    ):
        problems.append(
            f"{label} {price} outside [{filters.min_price}, {filters.max_price}]"
        )
    if not is_multiple(price - filters.min_price, filters.tick):
        problems.append(f"{label} {price} not a multiple of tick {filters.tick}")
    return [Violation(Skip.FILTER_VIOLATION, p) for p in problems]


def _lot_rule(
    filters: SymbolFilters, qty: Decimal, label: str, *, market: bool
) -> list[Violation]:
    problems = []
    if qty < filters.min_qty or qty > filters.max_qty:
        problems.append(
            f"{label} qty {qty} outside [{filters.min_qty}, {filters.max_qty}]"
        )
    if not is_multiple(qty - filters.min_qty, filters.step):
        problems.append(f"{label} qty {qty} not a multiple of step {filters.step}")
    if market and filters.market_max_qty is not None:
        low = filters.market_min_qty or Decimal(0)
        high = filters.market_max_qty if filters.market_max_qty > 0 else qty
        if not low <= qty <= high:
            problems.append(f"{label} market qty {qty} outside [{low}, {high}]")
        if filters.market_step and not is_multiple(qty - low, filters.market_step):
            problems.append(f"{label} qty {qty} not a multiple of market step")
    return [Violation(Skip.FILTER_VIOLATION, p) for p in problems]


def _reference(
    quote: QuoteSnapshot, mins: int, now: datetime, params: SizingParams, label: str
) -> Decimal | Violation:
    try:
        return quote.reference_for(mins, now, max_trade_age=params.max_trade_age)
    except ReferenceUnverified as exc:
        return Violation(Skip.REFERENCE_UNVERIFIED, f"{label}: {exc}")


def _notional_rule(
    filters: SymbolFilters,
    quote: QuoteSnapshot,
    now: datetime,
    params: SizingParams,
    qty: Decimal,
    price: Decimal | None,
    label: str,
) -> list[Violation]:
    """``price=None`` means a market order: the exchange uses its reference."""
    rule = filters.notional
    if rule is None:
        return []
    if price is not None:
        value = qty * price
        low, high = rule.min, rule.max
    else:
        if not (rule.apply_min_to_market or rule.apply_max_to_market):
            return []
        ref = _reference(quote, rule.avg_price_mins, now, params, label)
        if isinstance(ref, Violation):
            return [ref]
        value = qty * ref
        low = rule.min if rule.apply_min_to_market else Decimal(0)
        high = rule.max if rule.apply_max_to_market else None
    if value < low or (high is not None and value > high):
        return [
            Violation(
                Skip.FILTER_VIOLATION,
                f"{label} notional {value} outside [{low}, {high}]",
            )
        ]
    return []


def _percent_rule(
    filters: SymbolFilters,
    quote: QuoteSnapshot,
    now: datetime,
    params: SizingParams,
    price: Decimal,
    side: str,
    label: str,
) -> list[Violation]:
    rule = filters.percent_by_side
    if rule is None:
        return []
    ref = _reference(quote, rule.avg_price_mins, now, params, label)
    if isinstance(ref, Violation):
        return [ref]
    up, down = (
        (rule.bid_up, rule.bid_down) if side == "BUY" else (rule.ask_up, rule.ask_down)
    )
    if not ref * down <= price <= ref * up:
        return [
            Violation(
                Skip.FILTER_VIOLATION,
                f"{label} price {price} outside {down}..{up} × reference {ref}",
            )
        ]
    return []


def validate_entry(
    filters: SymbolFilters,
    quote: QuoteSnapshot,
    now: datetime,
    params: SizingParams,
    *,
    qty: Decimal,
    price: Decimal,
) -> list[Violation]:
    """The LIMIT FOK BUY, with its final rounded quantity and price."""
    return [
        *_price_rule(filters, price, "entry"),
        *_lot_rule(filters, qty, "entry", market=False),
        *_notional_rule(filters, quote, now, params, qty, price, "entry"),
        *_percent_rule(filters, quote, now, params, price, "BUY", "entry"),
    ]


def stop_order_type(filters: SymbolFilters) -> str | None:
    """``STOP_LOSS`` when allowed, else ``STOP_LOSS_LIMIT`` (+ guard), else none."""
    if "STOP_LOSS" in filters.order_types:
        return "STOP_LOSS"
    if "STOP_LOSS_LIMIT" in filters.order_types:
        return "STOP_LOSS_LIMIT"
    return None


def stop_limit_price(
    filters: SymbolFilters, stop: Decimal, params: SizingParams
) -> Decimal:
    return round_down(stop * (1 - params.stop_limit_gap_pct / _HUNDRED), filters.tick)


def validate_protection(
    filters: SymbolFilters,
    quote: QuoteSnapshot,
    now: datetime,
    params: SizingParams,
    *,
    qty: Decimal,
    take_profit: Decimal,
    stop: Decimal,
    limit_price: Decimal | None,
    last: Decimal | None = None,
) -> list[Violation]:
    """Both legs of the SELL OCO, exactly as they would be sent.

    The stop leg is a market order once triggered, so it must also be sellable
    at the stop price; a ``STOP_LOSS_LIMIT`` leg is checked at its limit price
    too, which is lower than the stop.
    """
    problems = [
        *_price_rule(filters, take_profit, "take-profit"),
        *_price_rule(filters, stop, "stop"),
        *_lot_rule(filters, qty, "protection", market=limit_price is None),
        *_notional_rule(filters, quote, now, params, qty, take_profit, "take-profit"),
        *_percent_rule(filters, quote, now, params, take_profit, "SELL", "take-profit"),
        *_notional_rule(filters, quote, now, params, qty, stop, "stop at trigger"),
    ]
    if limit_price is None:
        problems += _notional_rule(
            filters, quote, now, params, qty, None, "stop (market)"
        )
    else:
        problems += [
            *_price_rule(filters, limit_price, "stop limit"),
            *_notional_rule(
                filters, quote, now, params, qty, limit_price, "stop limit"
            ),
            *_percent_rule(
                filters, quote, now, params, limit_price, "SELL", "stop limit"
            ),
        ]
    if last is not None and not stop < last < take_profit:
        problems.append(
            Violation(
                Skip.STOP_ABOVE_PRICE,
                f"need stop {stop} < last {last} < take-profit {take_profit}",
            )
        )
    return problems


def validate_market_sell(
    filters: SymbolFilters,
    quote: QuoteSnapshot,
    now: datetime,
    params: SizingParams,
    *,
    qty: Decimal,
) -> list[Violation]:
    """An exit at market (time exit, gap, emergency, manual close)."""
    return [
        *_lot_rule(filters, qty, "market sell", market=True),
        *_notional_rule(filters, quote, now, params, qty, None, "market sell"),
    ]


def sellable(filters: SymbolFilters, qty: Decimal, price: Decimal) -> bool:
    """Whether a remainder can still be sold at all, or is dust (§8.4)."""
    notional = filters.notional.min if filters.notional else Decimal(0)
    qty = round_down(qty, filters.step)
    return qty >= filters.min_qty and qty > 0 and qty * price >= notional


# --- the entry plan ----------------------------------------------------------


@dataclass(frozen=True)
class EntryPlan:
    symbol: str
    quantity: Decimal
    cap_price: Decimal
    stop_estimate: Decimal
    take_profit_estimate: Decimal
    stop_type: str
    stop_limit_estimate: Decimal | None
    quote_cost: Decimal
    target_size: Decimal
    grew_to_minimum: bool
    slots: Slots

    def as_record(self) -> dict[str, str | int | bool | None]:
        return {
            "quantity": str(self.quantity),
            "cap_price": str(self.cap_price),
            "stop_estimate": str(self.stop_estimate),
            "take_profit_estimate": str(self.take_profit_estimate),
            "stop_type": self.stop_type,
            "stop_limit_estimate": (
                str(self.stop_limit_estimate) if self.stop_limit_estimate else None
            ),
            "quote_cost": str(self.quote_cost),
            "target_size": str(self.target_size),
            "grew_to_minimum": self.grew_to_minimum,
            "equity": str(self.slots.equity),
            "slots": self.slots.slots,
        }


def check_symbol(
    filters: SymbolFilters,
    params: SizingParams,
    account_permissions: frozenset[str] | None = None,
) -> str:
    """Raise unless the symbol can be traded and protected; return stop type.

    ``account_permissions`` (the account's ``permissions``) are checked
    against the symbol's permission sets when given.
    """
    if filters.quote_asset != params.quote_asset:
        raise EntryRejected(
            Skip.QUOTE_NOT_SUPPORTED,
            f"{filters.symbol} is quoted in {filters.quote_asset}; "
            f"execution supports {params.quote_asset} only",
        )
    if not filters.tradable:
        raise EntryRejected(Skip.SYMBOL_NOT_TRADING, filters.status)
    if account_permissions is not None and not filters.permits(account_permissions):
        raise EntryRejected(
            Skip.SYMBOL_NOT_PERMITTED,
            f"account permissions {sorted(account_permissions)} "
            f"are not allowed on {filters.symbol}",
        )
    if not filters.oco_allowed:
        raise EntryRejected(Skip.OCO_NOT_ALLOWED)
    if filters.unknown:
        raise EntryRejected(Skip.UNKNOWN_FILTER, ", ".join(filters.unknown))
    stop_type = stop_order_type(filters)
    missing = {"LIMIT", "LIMIT_MAKER", "MARKET"} - filters.order_types
    if stop_type is None or missing:
        raise EntryRejected(
            Skip.ORDER_TYPE_NOT_ALLOWED, f"missing {sorted(missing) or 'a stop type'}"
        )
    return stop_type


def plan_entry(
    *,
    filters: SymbolFilters,
    quote: QuoteSnapshot,
    now: datetime,
    params: SizingParams,
    entry_ref: Decimal,
    policy: ExitPolicy,
    total_equity: Decimal,
    free_quote: Decimal,
    reserved_quote: Decimal,
    occupied_slots: int,
    account_permissions: frozenset[str] | None = None,
) -> EntryPlan:
    """Size one ENTRY and check every order it will lead to.

    ``occupied_slots`` counts open positions plus pending and UNKNOWN entries;
    ``reserved_quote`` is what those pending and UNKNOWN entries still hold.
    Raises :class:`EntryRejected`.
    """
    stop_type = check_symbol(filters, params, account_permissions)
    slots = slots_for(total_equity, params)
    if slots.slots == 0:
        raise EntryRejected(
            Skip.NO_SLOTS, f"equity {total_equity} < {params.slot_quote}"
        )
    if occupied_slots >= slots.slots:
        raise EntryRejected(
            Skip.SLOTS_FULL, f"{occupied_slots} of {slots.slots} in use"
        )

    cap = round_down(entry_ref * (1 + params.max_slippage_pct / _HUNDRED), filters.tick)
    stop = round_down(policy.initial_stop(cap), filters.tick)
    tp = round_up(policy.take_profit(cap), filters.tick)
    limit = (
        stop_limit_price(filters, stop, params)
        if stop_type == "STOP_LOSS_LIMIT"
        else None
    )
    if stop <= 0 or (limit is not None and limit <= 0):
        raise EntryRejected(Skip.FILTER_VIOLATION, f"stop {stop} not positive")
    minimum_notional = filters.notional.min if filters.notional else Decimal(0)
    lowest_exit = min(p for p in (stop, limit, tp) if p is not None)
    qty_min = max(
        filters.min_qty,
        minimum_notional / cap,
        minimum_notional / lowest_exit,
    )
    target = slots.size / cap
    qty = round_up(max(target, qty_min), filters.step)
    cost = qty * cap
    available = free_quote - reserved_quote
    if cost > available:
        raise EntryRejected(
            Skip.INSUFFICIENT_BALANCE, f"needs {cost}, available {available}"
        )
    problems = validate_entry(filters, quote, now, params, qty=qty, price=cap)
    if filters.max_position is not None and qty > filters.max_position:
        problems.append(
            Violation(
                Skip.FILTER_VIOLATION,
                f"qty {qty} above maxPosition {filters.max_position}",
            )
        )
    problems += validate_protection(
        filters,
        quote,
        now,
        params,
        qty=qty,
        take_profit=tp,
        stop=stop,
        limit_price=limit,
    )
    if limit is not None:
        # A STOP_LOSS_LIMIT leg was checked as a limit order, but the guard,
        # time exit and emergency sale of this position are MARKET sells; a
        # quantity MARKET_LOT_SIZE refuses would leave no way out.
        problems += validate_market_sell(filters, quote, now, params, qty=qty)
    if problems:
        first = problems[0]
        raise EntryRejected(first.reason, "; ".join(p.detail for p in problems))
    return EntryPlan(
        symbol=filters.symbol,
        quantity=qty,
        cap_price=cap,
        stop_estimate=stop,
        take_profit_estimate=tp,
        stop_type=stop_type,
        stop_limit_estimate=limit,
        quote_cost=cost,
        target_size=slots.size,
        grew_to_minimum=qty_min > target,
        slots=slots,
    )


__all__ = [
    "EntryPlan",
    "EntryRejected",
    "SizingParams",
    "Skip",
    "Slots",
    "Violation",
    "check_symbol",
    "equity",
    "plan_entry",
    "sellable",
    "slots_for",
    "stop_limit_price",
    "stop_order_type",
    "validate_entry",
    "validate_market_sell",
    "validate_protection",
]
