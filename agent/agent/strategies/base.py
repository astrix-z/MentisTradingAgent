"""Strategy base class, registry and execution context.

A strategy *decides* what to do; it never touches the exchange directly. It
places orders through :class:`StrategyContext`, which routes every proposal
through the risk gate and then the execution service, so nothing can bypass the
account-wide limits.

Adding a strategy is a new module that registers itself:

.. code-block:: python

    @strategy("grid")
    class Grid(Strategy):
        async def on_start(self, ctx: StrategyContext) -> None: ...

The registry (:data:`STRATEGIES`) mirrors the command registry in ``commands.py``
so the engine can look a strategy up by name without new wiring.
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
    from agent.portfolio import Portfolio
    from agent.risk import RiskManager

logger = logging.getLogger(__name__)


class StrategyContext:
    """Everything a strategy needs to act, with the risk gate applied."""

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

    @property
    def symbols(self) -> list[str]:
        """Symbols this run should manage (from the start command)."""
        raw = self.config.get("symbols", [])
        return [str(s).upper() for s in raw]

    def param(self, key: str, default: Any) -> Any:
        """Read a strategy parameter with a fallback default."""
        return self.config.get(key, default)

    def current_price(self, symbol: str) -> float | None:
        """Latest mid price for ``symbol`` from the shared market cache."""
        quote = self._market.get_quote(symbol)
        return quote.mid if quote else None

    async def buy_limit(
        self, *, symbol: str, quantity: float, price: float, tag: str
    ) -> OrderResult:
        """Place a resting BUY limit order (GTC) if the risk gate allows it."""
        await self._risk.assert_can_place(
            NewOrder(
                symbol=symbol,
                side="BUY",
                order_type="LIMIT",
                quantity=quantity,
                price=price,
                time_in_force="GTC",
            ),
            quote_price=price,
        )
        result = await self._execution.limit(
            symbol=symbol, side="BUY", quantity=quantity, price=price, tag=tag
        )
        self._watch_resting(result, symbol=symbol, side="BUY", price=price)
        return result

    async def sell_limit(
        self, *, symbol: str, quantity: float, price: float, tag: str
    ) -> OrderResult:
        """Place a resting SELL limit order (GTC) if the risk gate allows it."""
        await self._risk.assert_can_place(
            NewOrder(
                symbol=symbol,
                side="SELL",
                order_type="LIMIT",
                quantity=quantity,
                price=price,
                time_in_force="GTC",
            ),
            quote_price=price,
        )
        result = await self._execution.limit(
            symbol=symbol, side="SELL", quantity=quantity, price=price, tag=tag
        )
        self._watch_resting(result, symbol=symbol, side="SELL", price=price)
        return result

    def _watch_resting(
        self, result: OrderResult, *, symbol: str, side: OrderSide, price: float
    ) -> None:
        """Start watching a resting order so its fills feed the portfolio."""
        if self._fills is not None:
            self._fills.watch(result.order_id, symbol=symbol, side=side, price=price)

    async def cancel(self, *, symbol: str, order_id: int) -> None:
        """Best-effort cancel of one order owned by the strategy."""
        try:
            await self._execution.cancel(symbol=symbol, order_id=order_id)
        except ExchangeError:
            logger.warning("strategy failed to cancel order %s on %s", order_id, symbol)


class Strategy:
    """A decision strategy. Hook methods are driven by the engine."""

    #: Registry key set by the ``strategy`` decorator.
    name: str = ""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    async def on_start(self, ctx: StrategyContext) -> None:
        """Called once when the engine starts this strategy."""

    async def on_tick(self, ctx: StrategyContext, symbol: str) -> None:
        """Called whenever a new quote arrives for a tracked ``symbol``."""

    async def on_fill(
        self,
        ctx: StrategyContext,
        symbol: str,
        side: OrderSide,
        quantity: float,
        price: float,
    ) -> None:
        """Called when one of our orders is reported as filled."""

    async def on_stop(self, ctx: StrategyContext) -> None:
        """Called when the engine stops this strategy."""


# Strategy registry: name -> Strategy subclass.
STRATEGIES: dict[str, type[Strategy]] = {}


def strategy(name: str) -> Callable[[type[Strategy]], type[Strategy]]:
    """Decorator that registers a :class:`Strategy` subclass under ``name``."""

    def register(cls: type[Strategy]) -> type[Strategy]:
        STRATEGIES[name] = cls
        cls.name = name
        return cls

    return register


def available_strategies() -> list[str]:
    """Sorted names of all registered strategies (for the UI/command layer)."""
    return sorted(STRATEGIES)
