"""Shared in-memory cache of the latest market quotes per tracked symbol.

The websocket feed (``agent.exchange.streams``) pushes best bid/ask into a
single :class:`MarketData` instance. Strategies, the position protector and the
engine all read from this one source instead of each consuming the raw stream
differently.

Everything runs in a single asyncio event loop, so no locking is needed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Quote:
    """Latest known best bid/ask for a symbol."""

    symbol: str
    best_bid: float
    best_ask: float
    ts: datetime

    @property
    def mid(self) -> float:
        """Midpoint between the best bid and the best ask."""
        return (self.best_bid + self.best_ask) / 2.0

    @property
    def spread(self) -> float:
        """Absolute spread between the best ask and the best bid."""
        return self.best_ask - self.best_bid


class MarketData:
    """Latest quote per symbol plus a way to wake up waiters on updates."""

    def __init__(self) -> None:
        self._quotes: dict[str, Quote] = {}
        # Set on every quote update; consumers await then clear it themselves
        # so rapid updates coalesce into a single wake-up.
        self._updated = asyncio.Event()

    def update_book(
        self,
        symbol: str,
        best_bid: float,
        best_ask: float,
        ts: datetime | None = None,
    ) -> None:
        """Record a new best bid/ask and notify any waiters."""
        if best_bid <= 0 or best_ask <= 0 or best_ask < best_bid:
            return
        self._quotes[symbol.upper()] = Quote(
            symbol=symbol.upper(),
            best_bid=best_bid,
            best_ask=best_ask,
            ts=ts or _now(),
        )
        self._updated.set()

    def get_quote(self, symbol: str) -> Quote | None:
        """Return the latest quote for ``symbol`` (``None`` if not seen yet)."""
        return self._quotes.get(symbol.upper())

    @property
    def symbols(self) -> frozenset[str]:
        """Symbols that have produced at least one quote."""
        return frozenset(self._quotes)

    def quote_count(self) -> int:
        """Number of symbols with a recorded quote."""
        return len(self._quotes)

    async def wait_updated(self, timeout: float | None = None) -> bool:
        """Block until a new quote arrives (or ``timeout`` elapses).

        Returns ``True`` if a quote update woke us, ``False`` on timeout.
        """
        try:
            await asyncio.wait_for(self._updated.wait(), timeout)
        except TimeoutError:
            return False
        self._updated.clear()
        return True
