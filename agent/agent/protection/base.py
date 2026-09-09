"""Position-protection base class, registry and execution context.

A *protector* is an overlay that watches open positions and manages protective
(mostly stop) orders, independently of which strategy opened them. Like a
strategy it is selectable by name and registered with ``@protector``.

Protective orders are SELL-side, so the risk gate always allows them — the
protector must be able to cut any position even when account limits are reached.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from agent.exchange.binance import ExchangeError
from agent.exchange.order_types import NewOrder, OrderResult, OrderSide

if TYPE_CHECKING:
    from agent.exchange.execution import ExecutionService
    from agent.fills import FillTracker
    from agent.marketdata import MarketData
    from agent.portfolio import Portfolio, Position
    from agent.risk import RiskManager

logger = logging.getLogger(__name__)


class ProtectionContext:
    """The toolset a protector uses to manage protective orders."""

    def __init__(
        self,
        *,
        execution: ExecutionService,
        portfolio: Portfolio,
        market: MarketData,
        risk: RiskManager,
        config: dict[str, Any],
        fills: FillTracker | None = None,
    ) -> None:
        self._execution = execution
        self._portfolio = portfolio
        self._market = market
        self._risk = risk
        self._fills = fills
        self.config = config

    def param(self, key: str, default: Any) -> Any:
        """Read a protector parameter with a fallback default."""
        return self.config.get(key, default)

    def position(self, symbol: str) -> Position | None:
        """Current open position for ``symbol`` (``None`` when flat)."""
        return self._portfolio.position(symbol)

    def open_positions(self) -> list[Position]:
        """All currently open positions."""
        return self._portfolio.open_positions()

    def current_price(self, symbol: str) -> float | None:
        """Latest mid price for ``symbol`` from the shared market cache."""
        quote = self._market.get_quote(symbol)
        return quote.mid if quote else None

    async def place_stop_sell(
        self,
        *,
        symbol: str,
        quantity: float,
        stop_price: float,
        limit_price: float | None,
        tag: str,
    ) -> OrderResult:
        """Place a SELL stop-limit to exit ``quantity`` if ``stop_price`` trips.

        ``limit_price`` (optional) is the worst-case execution limit once the
        stop triggers; when omitted the stop becomes a market stop.
        """
        if stop_price <= 0:
            raise ExchangeError("stop price must be positive")
        if quantity <= 0:
            raise ExchangeError("stop quantity must be positive")
        # The exchange needs a limit price for STOP_LOSS_LIMIT; default to just
        # below the stop so a triggered stop is not left unfilled on a gap.
        exec_limit = limit_price if limit_price is not None else stop_price * 0.999
        if exec_limit <= 0:
            raise ExchangeError("limit price must be positive")

        order = NewOrder(
            symbol=symbol,
            side="SELL",
            order_type="STOP_LOSS_LIMIT",
            quantity=quantity,
            price=exec_limit,
            stop_price=stop_price,
            time_in_force="GTC",
        )
        # SELL-side: the risk gate always allows reducing a position.
        await self._risk.assert_can_place(order, quote_price=stop_price)
        result = await self._execution.place_tagged(order, tag)
        if self._fills is not None:
            self._fills.watch(
                result.order_id, symbol=symbol, side="SELL", price=exec_limit
            )
        return result

    async def cancel(self, *, symbol: str, order_id: int) -> None:
        """Best-effort cancel of a protective order owned by the protector."""
        try:
            await self._execution.cancel(symbol=symbol, order_id=order_id)
        except ExchangeError:
            logger.warning("protector failed to cancel order %s on %s", order_id, symbol)


class Protector:
    """A position-protection algorithm. Hook methods are driven by the engine."""

    #: Registry key set by the ``protector`` decorator.
    name: str = ""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    async def on_start(self, ctx: ProtectionContext) -> None:
        """Called once when the engine starts this protector."""

    async def on_tick(self, ctx: ProtectionContext, symbol: str) -> None:
        """Called whenever a new quote arrives for a tracked ``symbol``."""

    async def on_fill(
        self,
        ctx: ProtectionContext,
        symbol: str,
        side: OrderSide,
        quantity: float,
        price: float,
    ) -> None:
        """Called when a position-relevant fill is reported."""

    async def on_stop(self, ctx: ProtectionContext) -> None:
        """Called when the engine stops this protector."""


# Protector registry: name -> Protector subclass.
PROTECTORS: dict[str, type[Protector]] = {}


def protector(name: str) -> Callable[[type[Protector]], type[Protector]]:
    """Decorator that registers a :class:`Protector` subclass under ``name``."""

    def register(cls: type[Protector]) -> type[Protector]:
        PROTECTORS[name] = cls
        cls.name = name
        return cls

    return register


def available_protectors() -> list[str]:
    """Sorted names of all registered protectors (for the UI/command layer)."""
    return sorted(PROTECTORS)
