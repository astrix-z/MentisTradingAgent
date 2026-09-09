"""Execution service: the single way the agent places/cancels orders.

Strategies and the position protector never talk to ``BinanceSpot`` directly —
they go through this service. That keeps order handling independent of any one
strategy and gives a stable place to add retries, idempotency tags and (later)
risk checks.

An instance is bound to one exchange client, so it is scoped to a single
network. The engine recreates it whenever it (re)starts on a network.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import replace

from agent.exchange.binance import BinanceSpot, ExchangeError
from agent.exchange.order_types import (
    CancelResult,
    NewOrder,
    OrderResult,
    OrderSide,
    OrderStatus,
)

logger = logging.getLogger(__name__)

# In-flight limit orders are tracked so the grid/protector can tell how many
# resting orders exist without re-querying the exchange on every tick.
_OPEN_STATUSES = {"NEW", "PARTIALLY_FILLED"}


class ExecutionService:
    """Convenient, network-scoped order operations on top of a ``BinanceSpot``."""

    def __init__(self, exchange: BinanceSpot, run_id: str | None = None) -> None:
        self._exchange = exchange
        self._run_id = run_id or uuid.uuid4().hex[:8]

    # --- low-level placement ------------------------------------------------

    def make_client_order_id(self, tag: str) -> str:
        """Build a unique, idempotent client order id for this run."""
        return f"{self._run_id}-{tag}-{uuid.uuid4().hex[:12]}"

    async def place(self, order: NewOrder) -> OrderResult:
        """Send a (pre-validated) order to the exchange."""
        return await self._exchange.place_order(order)

    async def place_tagged(self, order: NewOrder, tag: str) -> OrderResult:
        """Place an order with a unique, idempotent client order id for ``tag``.

        Used for arbitrary order types (e.g. stop-loss/take-profit) that have no
        dedicated convenience method here.
        """
        tagged = replace(order, client_order_id=self.make_client_order_id(tag))
        return await self.place(tagged)

    # --- typed helpers -------------------------------------------------------

    async def market(self, *, symbol: str, side: OrderSide, quantity: float) -> OrderResult:
        """Place an immediate market order (BUY/SELL)."""
        if quantity <= 0:
            raise ExchangeError("market order quantity must be positive")
        return await self.place(
            NewOrder(symbol=symbol, side=side, order_type="MARKET", quantity=quantity)
        )

    async def limit(
        self,
        *,
        symbol: str,
        side: OrderSide,
        quantity: float,
        price: float,
        tag: str = "limit",
    ) -> OrderResult:
        """Place a resting limit order (GTC) with an idempotent client id."""
        if quantity <= 0:
            raise ExchangeError("limit order quantity must be positive")
        if price <= 0:
            raise ExchangeError("limit order price must be positive")
        return await self.place(
            NewOrder(
                symbol=symbol,
                side=side,
                order_type="LIMIT",
                quantity=quantity,
                price=price,
                time_in_force="GTC",
                client_order_id=self.make_client_order_id(tag),
            )
        )

    # --- cancellation ---------------------------------------------------------

    async def cancel(
        self,
        *,
        symbol: str,
        order_id: int | None = None,
        client_order_id: str | None = None,
    ) -> CancelResult | None:
        """Cancel one order; ``None`` if neither an id nor a client id is given."""
        if order_id is None and client_order_id is None:
            return None
        return await self._exchange.cancel_order(
            symbol=symbol,
            order_id=order_id,
            client_order_id=client_order_id,
        )

    async def cancel_all(self, symbol: str) -> int:
        """Cancel every open order on ``symbol``; returns how many were cancelled."""
        open_orders = await self._exchange.open_orders(symbol=symbol)
        cancelled = 0
        for order in open_orders:
            if order.status not in _OPEN_STATUSES:
                continue
            try:
                await self._exchange.cancel_order(symbol=symbol, order_id=order.order_id)
                cancelled += 1
            except ExchangeError:
                logger.warning("failed to cancel order %s on %s", order.order_id, symbol)
        return cancelled

    # --- queries ----------------------------------------------------------------

    async def open_orders(self, symbol: str | None = None) -> list[OrderStatus]:
        return await self._exchange.open_orders(symbol=symbol)

    async def get_order(
        self,
        *,
        symbol: str,
        order_id: int | None = None,
        client_order_id: str | None = None,
    ) -> OrderStatus | None:
        return await self._exchange.get_order(
            symbol=symbol, order_id=order_id, client_order_id=client_order_id
        )

    async def is_order_open(self, symbol: str, order_id: int) -> bool:
        """Return whether ``order_id`` is still resting on the exchange."""
        order = await self._exchange.get_order(symbol=symbol, order_id=order_id)
        return order is not None and order.status in _OPEN_STATUSES
