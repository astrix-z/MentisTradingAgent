"""Exchange rules of one symbol, read from ``exchangeInfo`` (plan §7.2).

The SDK parses each filter into its own model (``PriceFilter``,
``NotionalFilter``, ...). When the exchange adds a filter type the SDK does not
know, the whole ``exchangeInfo`` answer comes back as raw JSON instead — so
the parser reads the Binance JSON shape (``to_dict()`` of a model, or the raw
dict), which is the same in both cases.

A filter that could constrain an order and is not understood here makes the
symbol **fail closed for entries**: we would rather skip a trade than place one
the exchange rejects after we already hold the asset.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

#: Filters that cannot make one of our orders invalid (count limits we never
#: approach, iceberg and trailing-delta rules for order kinds we never send).
IGNORED_FILTERS = frozenset(
    {
        "ICEBERG_PARTS",
        "MAX_NUM_ORDERS",
        "MAX_NUM_ALGO_ORDERS",
        "MAX_NUM_ICEBERG_ORDERS",
        "MAX_NUM_ORDER_LISTS",
        "MAX_NUM_ORDER_AMENDS",
        "TRAILING_DELTA",
    }
)


def round_down(value: Decimal, step: Decimal) -> Decimal:
    """Largest multiple of ``step`` not above ``value`` (``step`` 0 = as is)."""
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def round_up(value: Decimal, step: Decimal) -> Decimal:
    """Smallest multiple of ``step`` not below ``value``."""
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


def is_multiple(value: Decimal, step: Decimal) -> bool:
    return step <= 0 or value % step == 0


@dataclass(frozen=True)
class Notional:
    min: Decimal
    max: Decimal | None
    apply_min_to_market: bool
    apply_max_to_market: bool
    avg_price_mins: int


@dataclass(frozen=True)
class PercentBySide:
    bid_up: Decimal
    bid_down: Decimal
    ask_up: Decimal
    ask_down: Decimal
    avg_price_mins: int


@dataclass(frozen=True)
class SymbolFilters:
    """What one symbol allows, in exchange units; all Decimal."""

    symbol: str
    status: str
    base_asset: str
    quote_asset: str
    order_types: frozenset[str]
    oco_allowed: bool
    tick: Decimal
    min_price: Decimal
    max_price: Decimal
    step: Decimal
    min_qty: Decimal
    max_qty: Decimal
    market_step: Decimal | None = None
    market_min_qty: Decimal | None = None
    market_max_qty: Decimal | None = None
    notional: Notional | None = None
    percent_by_side: PercentBySide | None = None
    max_position: Decimal | None = None
    unknown: tuple[str, ...] = field(default_factory=tuple)

    @property
    def tradable(self) -> bool:
        return self.status == "TRADING"


def _dec(value: Any, default: str = "0") -> Decimal:
    return Decimal(str(value)) if value not in (None, "") else Decimal(default)


def _raw(item: Any) -> dict[str, Any]:
    """The Binance JSON of a symbol or filter, whether parsed by the SDK or not."""
    if isinstance(item, Mapping):
        return dict(item)
    inner = getattr(item, "actual_instance", item)
    to_dict = getattr(inner, "to_dict", None)
    if to_dict is None:
        raise ValueError(f"cannot read exchange rule {item!r}")
    result: dict[str, Any] = to_dict()
    return result


def parse_symbol(entry: Any) -> SymbolFilters:
    """Build :class:`SymbolFilters` from one ``exchangeInfo`` symbol."""
    raw = _raw(entry)
    if not isinstance(entry, Mapping):
        # The model's own dict drops nothing, but filters are nested models.
        raw["filters"] = [_raw(f) for f in (getattr(entry, "filters", None) or [])]
    price: dict[str, Any] = {}
    lot: dict[str, Any] = {}
    values: dict[str, Any] = {}
    unknown: list[str] = []
    for item in raw.get("filters") or []:
        kind = str(item.get("filterType"))
        if kind == "PRICE_FILTER":
            price = item
        elif kind == "LOT_SIZE":
            lot = item
        elif kind == "MARKET_LOT_SIZE":
            values["market_step"] = _dec(item.get("stepSize"))
            values["market_min_qty"] = _dec(item.get("minQty"))
            values["market_max_qty"] = _dec(item.get("maxQty"))
        elif kind == "NOTIONAL":
            values["notional"] = Notional(
                min=_dec(item.get("minNotional")),
                max=_dec(item["maxNotional"]) if item.get("maxNotional") else None,
                apply_min_to_market=bool(item.get("applyMinToMarket")),
                apply_max_to_market=bool(item.get("applyMaxToMarket")),
                avg_price_mins=int(item.get("avgPriceMins") or 0),
            )
        elif kind == "MIN_NOTIONAL":
            # Legacy form; NOTIONAL supersedes it when both exist.
            values.setdefault(
                "notional",
                Notional(
                    min=_dec(item.get("minNotional")),
                    max=None,
                    apply_min_to_market=bool(item.get("applyToMarket")),
                    apply_max_to_market=False,
                    avg_price_mins=int(item.get("avgPriceMins") or 0),
                ),
            )
        elif kind == "PERCENT_PRICE_BY_SIDE":
            values["percent_by_side"] = PercentBySide(
                bid_up=_dec(item.get("bidMultiplierUp")),
                bid_down=_dec(item.get("bidMultiplierDown")),
                ask_up=_dec(item.get("askMultiplierUp")),
                ask_down=_dec(item.get("askMultiplierDown")),
                avg_price_mins=int(item.get("avgPriceMins") or 0),
            )
        elif kind == "PERCENT_PRICE":
            # Legacy symmetric form: same bounds on both sides.
            up, down = _dec(item.get("multiplierUp")), _dec(item.get("multiplierDown"))
            values.setdefault(
                "percent_by_side",
                PercentBySide(up, down, up, down, int(item.get("avgPriceMins") or 0)),
            )
        elif kind == "MAX_POSITION":
            values["max_position"] = _dec(item.get("maxPosition"))
        elif kind not in IGNORED_FILTERS:
            unknown.append(kind)
    if not price or not lot:
        unknown.append("missing PRICE_FILTER/LOT_SIZE")
    return SymbolFilters(
        symbol=str(raw.get("symbol")),
        status=str(raw.get("status")),
        base_asset=str(raw.get("baseAsset")),
        quote_asset=str(raw.get("quoteAsset")),
        order_types=frozenset(str(t) for t in raw.get("orderTypes") or []),
        oco_allowed=bool(raw.get("ocoAllowed")),
        tick=_dec(price.get("tickSize")),
        min_price=_dec(price.get("minPrice")),
        max_price=_dec(price.get("maxPrice")),
        step=_dec(lot.get("stepSize")),
        min_qty=_dec(lot.get("minQty")),
        max_qty=_dec(lot.get("maxQty"), "Infinity"),
        unknown=tuple(unknown),
        **values,
    )


class FilterCache:
    """Symbol rules per ``(network, symbol)``, refreshed on demand.

    ``fetch(symbol)`` returns the ``exchangeInfo`` answer for one symbol (the
    SDK model or raw JSON). Entries expire after ``ttl`` seconds and are
    dropped by :meth:`invalidate` whenever the exchange rejects an order, so a
    changed rule is re-read before the next attempt.
    """

    def __init__(
        self,
        fetch: Callable[[str, str], Awaitable[Any]],
        *,
        ttl: float = 3600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = fetch
        self._ttl = ttl
        self._clock = clock
        self._entries: dict[tuple[str, str], tuple[float, SymbolFilters]] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def get(self, network: str, symbol: str) -> SymbolFilters:
        key = (network, symbol)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._entries.get(key)
            if cached is not None and self._clock() - cached[0] < self._ttl:
                return cached[1]
            info = await self._fetch(network, symbol)
            symbols = (
                info.get("symbols") if isinstance(info, Mapping) else info.symbols
            ) or []
            matches = [s for s in symbols if _raw_symbol(s) == symbol]
            if not matches:
                raise LookupError(f"{symbol} is not listed on {network}")
            filters = parse_symbol(matches[0])
            self._entries[key] = (self._clock(), filters)
            return filters

    def invalidate(self, network: str, symbol: str) -> None:
        self._entries.pop((network, symbol), None)


def _raw_symbol(entry: Any) -> str | None:
    if isinstance(entry, Mapping):
        value = entry.get("symbol")
    else:
        value = getattr(entry, "symbol", None)
    return str(value) if value is not None else None


__all__ = [
    "IGNORED_FILTERS",
    "FilterCache",
    "Notional",
    "PercentBySide",
    "SymbolFilters",
    "is_multiple",
    "parse_symbol",
    "round_down",
    "round_up",
]
