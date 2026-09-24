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

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

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


__all__ = [
    "DEFAULT_MAX_FETCH_AGE",
    "DEFAULT_MAX_TRADE_AGE",
    "AveragePrice",
    "LastTrade",
    "QuoteSnapshot",
    "ReferencePrice",
    "ReferenceUnverified",
]
