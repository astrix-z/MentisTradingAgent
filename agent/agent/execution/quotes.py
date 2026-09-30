"""Quotes for admission and pre-submit, and which price each filter checks.

:class:`QuoteSnapshot` is the contract between the provider that fetches
quotes (EXEC-14) and the validator (plan §9.8). The validator never guesses a
price: for every dynamic filter it picks the reference the exchange itself uses
(Binance ``filters.md``), and when that price cannot be confirmed — missing,
stale, or averaged over a different window — the entry fails closed.

Order of preference, per filter:

1. ``referencePrice`` when the exchange reports one (non-null);
2. otherwise, a filter with ``avgPriceMins == 0`` uses the last trade;
3. otherwise the average price, **only** if ``avgPrice`` was computed over the
   same number of minutes as the filter's ``avgPriceMins``;
4. otherwise: unverifiable → ``reference_unverified``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from agent.exchange.budget import Priority
from agent.exchange.rest import ExchangeRest

logger = logging.getLogger(__name__)

DEFAULT_MAX_FETCH_AGE = timedelta(seconds=5)
DEFAULT_MAX_TRADE_AGE = timedelta(seconds=60)


class ReferenceUnverified(ValueError):
    """No price the exchange would use for a filter could be confirmed."""


@dataclass(frozen=True)
class LastTrade:
    price: Decimal
    time: datetime


@dataclass(frozen=True)
class AveragePrice:
    price: Decimal
    mins: int
    close_time: datetime  # time of the last trade in the window


@dataclass(frozen=True)
class ReferencePrice:
    price: Decimal | None  # null when the exchange uses the average instead
    timestamp: datetime


@dataclass(frozen=True)
class QuoteSnapshot:
    symbol: str
    network: str
    fetched_at: datetime
    last_trade: LastTrade | None
    avg: AveragePrice | None
    reference: ReferencePrice | None
    bid: Decimal | None = None
    ask: Decimal | None = None

    @property
    def spread_pct(self) -> Decimal | None:
        """Bid-ask spread in percent of the mid price, for the audit only."""
        if self.bid is None or self.ask is None or self.bid <= 0:
            return None
        return (self.ask - self.bid) / ((self.ask + self.bid) / 2) * 100

    def as_record(self) -> dict[str, str | int | None]:
        """What the decision was based on, JSON-safe, for the audit."""

        def text(value: object) -> str | None:
            return None if value is None else str(value)

        return {
            "fetched_at": self.fetched_at.isoformat(),
            "last": text(self.last_trade and self.last_trade.price),
            "last_at": text(self.last_trade and self.last_trade.time.isoformat()),
            "bid": text(self.bid),
            "ask": text(self.ask),
            "spread_pct": text(self.spread_pct),
            "avg": text(self.avg and self.avg.price),
            "avg_mins": self.avg.mins if self.avg else None,
            "reference": text(self.reference and self.reference.price),
        }

    def check_fresh(
        self,
        now: datetime,
        *,
        max_fetch_age: timedelta = DEFAULT_MAX_FETCH_AGE,
        max_trade_age: timedelta = DEFAULT_MAX_TRADE_AGE,
    ) -> None:
        """Raise :class:`ReferenceUnverified` unless the snapshot is usable now."""
        if now - self.fetched_at > max_fetch_age:
            raise ReferenceUnverified(
                f"quote fetched {(now - self.fetched_at).total_seconds():.1f}s ago"
            )
        if self.last_trade is None:
            raise ReferenceUnverified("no last trade")
        if now - self.last_trade.time > max_trade_age:
            raise ReferenceUnverified("last trade is stale")

    def last_price(self) -> Decimal:
        if self.last_trade is None:
            raise ReferenceUnverified("no last trade")
        return self.last_trade.price

    def reference_for(
        self,
        avg_price_mins: int,
        now: datetime,
        *,
        max_trade_age: timedelta = DEFAULT_MAX_TRADE_AGE,
    ) -> Decimal:
        """The price a filter with ``avg_price_mins`` is evaluated against."""
        if self.reference is None:
            raise ReferenceUnverified("reference price was not fetched")
        if now - self.reference.timestamp > max_trade_age:
            raise ReferenceUnverified("reference price is stale")
        if self.reference.price is not None:
            return self.reference.price
        if avg_price_mins == 0:
            return self.last_price()
        if self.avg is None:
            raise ReferenceUnverified("average price was not fetched")
        if self.avg.mins != avg_price_mins:
            raise ReferenceUnverified(
                f"average price is over {self.avg.mins} min, filter uses {avg_price_mins}"
            )
        if now - self.avg.close_time > max_trade_age:
            raise ReferenceUnverified("average price is stale")
        return self.avg.price


def _utc(ms: int | None) -> datetime | None:
    return datetime.fromtimestamp(ms / 1000, UTC) if isinstance(ms, int) else None


def _decimal(value: str | None) -> Decimal | None:
    return Decimal(value) if value not in (None, "") else None


class QuoteProvider:
    """Fresh quotes for a symbol, with or without a position (plan §9.8).

    Everything comes from the SDK's REST market-data methods through the shared
    request budget, at ENTRY priority: a quote is only needed to decide whether
    to buy, and protection outranks it. A read that fails leaves its part of the
    snapshot empty — the validator then fails the entry closed; nothing here
    substitutes one price for another.
    """

    def __init__(
        self,
        rest: ExchangeRest,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        max_wait: float | None = 2.0,
    ) -> None:
        self._rest = rest
        self._clock = clock
        self._max_wait = max_wait

    @property
    def network(self) -> str:
        return self._rest.network

    async def snapshot(self, symbol: str) -> QuoteSnapshot:
        entry, wait = Priority.ENTRY, self._max_wait
        trades, avg, reference, book = await asyncio.gather(
            self._rest.last_trades(symbol, priority=entry, max_wait=wait),
            self._rest.avg_price(symbol, priority=entry, max_wait=wait),
            self._rest.reference_price(symbol, priority=entry, max_wait=wait),
            self._rest.book_ticker(symbol, priority=entry, max_wait=wait),
            return_exceptions=True,
        )
        fetched_at = self._clock()
        for name, result in (
            ("trades", trades),
            ("avgPrice", avg),
            ("referencePrice", reference),
            ("bookTicker", book),
        ):
            if isinstance(result, BaseException):
                logger.warning("quote %s for %s unavailable: %s", name, symbol, result)
        return QuoteSnapshot(
            symbol=symbol,
            network=self.network,
            fetched_at=fetched_at,
            last_trade=_last_trade(trades),
            avg=_average(avg),
            reference=_reference(reference),
            bid=_book_side(book, "bid_price"),
            ask=_book_side(book, "ask_price"),
        )


def _last_trade(result: object) -> LastTrade | None:
    if not isinstance(result, list) or not result:
        return None
    trade = result[-1]
    price = _decimal(getattr(trade, "price", None))
    when = _utc(getattr(trade, "time", None))
    return LastTrade(price, when) if price is not None and when is not None else None


def _average(result: object) -> AveragePrice | None:
    price = _decimal(getattr(result, "price", None))
    mins = getattr(result, "mins", None)
    when = _utc(getattr(result, "close_time", None))
    if price is None or not isinstance(mins, int) or when is None:
        return None
    return AveragePrice(price, mins, when)


def _reference(result: object) -> ReferencePrice | None:
    when = _utc(getattr(result, "timestamp", None))
    if when is None:
        return None
    return ReferencePrice(_decimal(getattr(result, "reference_price", None)), when)


def _book_side(result: object, side: str) -> Decimal | None:
    inner = getattr(result, "actual_instance", result)
    return _decimal(getattr(inner, side, None))


__all__ = [
    "DEFAULT_MAX_FETCH_AGE",
    "DEFAULT_MAX_TRADE_AGE",
    "AveragePrice",
    "LastTrade",
    "QuoteProvider",
    "QuoteSnapshot",
    "ReferencePrice",
    "ReferenceUnverified",
]
