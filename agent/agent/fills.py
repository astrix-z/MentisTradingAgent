"""Fill detection for orders the agent placed itself.

In this spot bot every trade is initiated by the agent, so fills can be derived
deterministically from our own order lifecycle instead of a user-data websocket:
a resting order we watch is polled (via the execution service) each engine tick;
any increase in its ``executed_qty`` is turned into a :class:`Fill` and applied
to the :class:`~agent.portfolio.Portfolio`. This captures partial fills and
fills that complete after an order leaves the open-orders list (FILLED /
CANCELED with a partial execution).

Market orders fill immediately at placement; the engine can apply those to the
portfolio directly with the current market price and does not need this tracker.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent.exchange.order_types import OrderSide
from agent.portfolio import Portfolio

if TYPE_CHECKING:
    from agent.exchange.execution import ExecutionService
    from agent.exchange.order_types import OrderStatus

logger = logging.getLogger(__name__)

_EPSILON = 1e-12


@dataclass(frozen=True)
class Fill:
    """An executed trade to be applied to the portfolio."""

    symbol: str
    side: OrderSide
    quantity: float
    price: float


@dataclass
class _Watched:
    symbol: str
    side: OrderSide
    price: float
    last_executed: float = 0.0


class FillTracker:
    """Watches resting orders and turns executions into portfolio fills."""

    def __init__(self, portfolio: Portfolio, execution: ExecutionService) -> None:
        self._portfolio = portfolio
        self._execution = execution
        self._watched: dict[int, _Watched] = {}

    def watch(self, order_id: int, *, symbol: str, side: OrderSide, price: float) -> None:
        """Start tracking a resting order so its fills can be detected."""
        self._watched[order_id] = _Watched(
            symbol=symbol.upper(), side=side, price=price
        )

    def forget(self, order_id: int) -> None:
        """Stop tracking an order (e.g. after an explicit cancel)."""
        self._watched.pop(order_id, None)

    @property
    def watched_count(self) -> int:
        return len(self._watched)

    async def reconcile(self, symbol: str | None = None) -> list[Fill]:
        """Poll watched orders and return any newly executed fills.

        Applies each fill to the portfolio and stops watching orders that are no
        longer open (filled, cancelled or expired).
        """
        fills: list[Fill] = []
        for order_id in list(self._watched):
            watched = self._watched[order_id]
            if symbol is not None and watched.symbol != symbol.upper():
                continue
            try:
                status = await self._execution.get_order(
                    symbol=watched.symbol, order_id=order_id
                )
            except Exception:  # noqa: BLE001 - a transient query must not kill the tick
                logger.warning("fill reconcile failed for order %s", order_id, exc_info=True)
                continue

            executed = status.executed_qty if status else 0.0
            delta = executed - watched.last_executed
            if delta > _EPSILON:
                self._portfolio.record_fill(
                    watched.symbol, watched.side, delta, watched.price
                )
                fills.append(
                    Fill(watched.symbol, watched.side, delta, watched.price)
                )
                watched.last_executed = executed

            # Stop watching once the order is no longer resting on the book.
            if status is None or not self._is_open(status):
                self._watched.pop(order_id, None)
        return fills

    @staticmethod
    def _is_open(status: OrderStatus) -> bool:
        return status.status in {"NEW", "PARTIALLY_FILLED"}
