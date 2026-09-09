"""Risk gate: limits applied between a decision and an order hitting the book.

Strategies and the position protector propose orders, but nothing is placed
until the risk gate approves it. The gate guards the whole account, not a single
strategy: number of resting orders, per-symbol and total notional, the number of
concurrently held symbols and a daily-loss cap.

The gate reads the agent's current state from ``Portfolio`` (positions) and
``MarketData`` (last prices) and, optionally, a provider of the open orders on
the exchange (defaulting to "skip the count check" when none is supplied).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from agent.exchange.binance import ExchangeError
from agent.exchange.order_types import NewOrder, OrderStatus
from agent.marketdata import MarketData
from agent.portfolio import Portfolio

logger = logging.getLogger(__name__)


class RiskError(ExchangeError):
    """Raised when the risk gate rejects an order."""


@dataclass(frozen=True)
class RiskLimits:
    """Account-wide limits enforced by the gate."""

    max_symbols: int = 5
    max_position_notional: float = 1_000.0
    max_total_notional: float = 5_000.0
    max_open_orders: int = 20
    max_daily_loss: float = 200.0


@dataclass(frozen=True)
class RiskDecision:
    """Outcome of evaluating an order against the limits."""

    allowed: bool
    reason: str | None = None
    notional: float = 0.0
    open_orders: int = 0

    @classmethod
    def ok(cls, *, notional: float = 0.0, open_orders: int = 0) -> RiskDecision:
        return cls(allowed=True, notional=notional, open_orders=open_orders)

    @classmethod
    def reject(cls, reason: str) -> RiskDecision:
        return cls(allowed=False, reason=reason)

    def or_raise(self) -> None:
        """Return/raise so the caller can short-circuit on rejection."""
        if not self.allowed:
            raise RiskError(self.reason or "order rejected by risk gate")


# A callback that returns the currently resting orders on the exchange.
OrdersProvider = Callable[[], Awaitable[list[OrderStatus]]]


class RiskManager:
    """Evaluates order proposals against :class:`RiskLimits`."""

    def __init__(
        self,
        *,
        portfolio: Portfolio,
        market: MarketData,
        limits: RiskLimits | None = None,
        orders_provider: OrdersProvider | None = None,
    ) -> None:
        self._portfolio = portfolio
        self._market = market
        self._limits = limits or RiskLimits()
        self._orders_provider = orders_provider
        # Realized PnL at creation is the baseline for the daily-loss window.
        self._session_base = portfolio.realized_pnl
        self._opened_at: datetime = datetime.now(UTC)

    @property
    def session_realized(self) -> float:
        """Realized PnL accrued since this gate was created (loss = negative)."""
        return self._portfolio.realized_pnl - self._session_base

    # --- evaluation -----------------------------------------------------------

    async def can_place(
        self, order: NewOrder, *, quote_price: float | None = None
    ) -> RiskDecision:
        """Check whether ``order`` may be placed; never raises for a rejection."""
        open_orders = await self._open_order_count()

        if open_orders is not None and open_orders >= self._limits.max_open_orders:
            return RiskDecision.reject(
                f"too many open orders ({open_orders}/{self._limits.max_open_orders})"
            )

        # Reducing risk (SELL) is always allowed within the open-order cap, so
        # the protector can always cut a position even when limits are hit.
        if order.side == "SELL":
            return RiskDecision.ok(open_orders=open_orders or 0)

        price = order.price if order.price and order.price > 0 else quote_price
        if price is None or price <= 0:
            return RiskDecision.reject("cannot size a BUY without a reference price")
        notional = order.quantity * price

        if self.session_realized <= -self._limits.max_daily_loss:
            return RiskDecision.reject(
                f"daily loss limit hit ({self.session_realized:.2f})"
            )

        position = self._portfolio.position(order.symbol)
        current_notional = (
            position.quantity * self._price_of(order.symbol, price) if position else 0.0
        )
        new_symbol = position is None
        if current_notional + notional > self._limits.max_position_notional:
            return RiskDecision.reject(
                f"per-symbol notional would exceed "
                f"{self._limits.max_position_notional:.0f}"
            )
        if (
            new_symbol
            and self._portfolio.open_count >= self._limits.max_symbols
        ):
            return RiskDecision.reject(
                f"too many held symbols ({self._portfolio.open_count})"
            )

        total_notional = await self._total_notional() + notional
        if total_notional > self._limits.max_total_notional:
            return RiskDecision.reject(
                f"total notional would exceed {self._limits.max_total_notional:.0f}"
            )
        return RiskDecision.ok(notional=notional, open_orders=open_orders or 0)

    async def assert_can_place(
        self, order: NewOrder, *, quote_price: float | None = None
    ) -> None:
        """Like :meth:`can_place` but raises :class:`RiskError` on rejection."""
        decision = await self.can_place(order, quote_price=quote_price)
        decision.or_raise()

    # --- helpers ----------------------------------------------------------------

    def _price_of(self, symbol: str, fallback: float) -> float:
        quote = self._market.get_quote(symbol)
        return quote.mid if quote else fallback

    async def _total_notional(self) -> float:
        """Approximate current total exposure from held positions."""
        total = 0.0
        for position in self._portfolio.open_positions():
            quote = self._market.get_quote(position.symbol)
            if quote is not None:
                total += position.quantity * quote.mid
        return total

    async def _open_order_count(self) -> int | None:
        """Return resting-order count via the provider, or ``None`` if unknown."""
        if self._orders_provider is None:
            return None
        return len(await self._orders_provider())

    def snapshot(self) -> dict[str, Any]:
        """Current risk posture, for logging/UI without a network call."""
        return {
            "session_realized": self.session_realized,
            "session_started_at": self._opened_at.isoformat(),
            "open_positions": self._portfolio.open_count,
            "symbols": sorted(self._portfolio.symbols),
            "limits": {
                "max_symbols": self._limits.max_symbols,
                "max_position_notional": self._limits.max_position_notional,
                "max_total_notional": self._limits.max_total_notional,
                "max_open_orders": self._limits.max_open_orders,
                "max_daily_loss": self._limits.max_daily_loss,
            },
        }
