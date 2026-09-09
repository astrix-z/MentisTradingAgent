"""Local view of the agent's open spot positions and realized PnL.

This is the single source of truth the strategies, the position protector and
the engine read to decide what to do next. It is fed by *fills* (from the
websocket ``user_data`` stream or from our own order confirmations) and, on
boot/restart, can be reconciled against the account balances so the agent knows
it already holds a position even if its entry cost is unknown.

Spot only: each symbol has one *long* position measured in the base asset
(e.g. ``BTC`` for ``BTCUSDT``); ``SELL`` reduces it. Everything runs on a single
asyncio loop, so no locking is needed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

logger = logging.getLogger(__name__)

# Anything at/below this quantity is treated as a fully closed position.
_EPSILON = 1e-12

OrderSide = Literal["BUY", "SELL"]


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Position:
    """An open long position in a spot symbol."""

    symbol: str
    quantity: float
    avg_price: float | None
    realized_pnl: float = 0.0
    opened_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)

    def unrealized_pnl(self, current_price: float) -> float:
        """Unrealized PnL at ``current_price`` (0 while the entry cost is unknown)."""
        if self.avg_price is None:
            return 0.0
        return (current_price - self.avg_price) * self.quantity

    def value(self, current_price: float) -> float:
        """Current market value of the held quantity."""
        return self.quantity * current_price


class Portfolio:
    """Tracks open positions per symbol and cumulative realized PnL."""

    def __init__(self) -> None:
        self._positions: dict[str, Position] = {}
        self._realized_pnl: float = 0.0

    @property
    def realized_pnl(self) -> float:
        """Total realized PnL across all traded symbols."""
        return self._realized_pnl

    def position(self, symbol: str) -> Position | None:
        """Return the open position for ``symbol`` (``None`` if flat)."""
        return self._positions.get(symbol.upper())

    @property
    def symbols(self) -> frozenset[str]:
        return frozenset(self._positions)

    def open_positions(self) -> list[Position]:
        """Positions that currently hold a non-zero quantity."""
        return list(self._positions.values())

    @property
    def open_count(self) -> int:
        return len(self._positions)

    def set_entry_price(self, symbol: str, avg_price: float) -> None:
        """Anchor the average entry price of an open position explicitly.

        Useful when the cost basis is recovered from trade history after a
        restart, for a position previously seeded without one.
        """
        position = self._positions.get(symbol.upper())
        if position is None:
            raise ValueError(f"no open position for {symbol!r} to anchor")
        if avg_price < 0:
            raise ValueError("entry price cannot be negative")
        position.avg_price = avg_price
        position.updated_at = _now()

    def record_fill(self, symbol: str, side: OrderSide, quantity: float, price: float) -> None:
        """Apply an executed trade to the position book.

        BUY increases the held quantity (rolling average entry price), SELL
        reduces it and realizes PnL. Quantity rounding leaves the position open
        until it is effectively zero.
        """
        symbol = symbol.upper()
        if quantity <= 0 or price < 0:
            logger.warning("ignoring invalid fill %s %s qty=%s price=%s", side, symbol, quantity, price)
            return
        position = self._positions.get(symbol)

        if side == "BUY":
            if position is None:
                self._positions[symbol] = Position(
                    symbol=symbol, quantity=quantity, avg_price=price
                )
            else:
                total_qty = position.quantity + quantity
                if position.avg_price is not None:
                    # Blend only when the running average is known; an unanchored
                    # position (from an account reconcile) keeps avg_price None
                    # rather than fabricating a cost basis of zero.
                    position.avg_price = (
                        position.avg_price * position.quantity + price * quantity
                    ) / total_qty
                position.quantity = total_qty
                position.updated_at = _now()
            return

        # SELL (reduce the long).
        if position is None:
            logger.warning("sell fill for %s with no open position ignored", symbol)
            return
        sell_qty = min(quantity, position.quantity)
        if position.avg_price is not None:
            position.realized_pnl += (price - position.avg_price) * sell_qty
            self._realized_pnl += (price - position.avg_price) * sell_qty
        position.quantity -= sell_qty
        position.updated_at = _now()
        if position.quantity <= _EPSILON:
            self._positions.pop(symbol, None)

    def reconcile_quantity(self, symbol: str, quantity: float) -> None:
        """Set the held base quantity from an account snapshot (boot/restart).

        After a restart the entry cost is unknown, so ``avg_price`` is kept only
        when the position already existed with the same quantity; otherwise it is
        reset to ``None`` and PnL reads 0 until the entry is anchored explicitly
        via :meth:`set_entry_price` (e.g. from trade history) or by seeding a
        fresh position through a BUY fill on a flat book.
        """
        symbol = symbol.upper()
        position = self._positions.get(symbol)
        if quantity <= _EPSILON:
            if position is not None:
                # Whole position disappeared on the exchange side.
                logger.info("portfolio: %s position vanished on account", symbol)
                self._positions.pop(symbol, None)
            return
        if position is None:
            self._positions[symbol] = Position(
                symbol=symbol, quantity=quantity, avg_price=None
            )
            return
        changed = abs(position.quantity - quantity) > _EPSILON
        position.quantity = quantity
        if changed:
            # Quantity moved underneath us: the running average entry is stale.
            position.avg_price = None
        position.updated_at = _now()
