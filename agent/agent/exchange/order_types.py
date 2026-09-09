"""Domain types for the order layer.

These are the agent's own, thin representations of Binance orders/order events,
so strategies, the protector and the risk layer depend on stable types instead
of the generated ``binance_sdk_spot`` models. Numeric fields are floats here
(the raw Binance payloads carry them as strings).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

OrderSide = Literal["BUY", "SELL"]

# Order types the agent places today. The exchange accepts more, but we expose
# only what the execution layer is expected to use.
OrderType = Literal["LIMIT", "MARKET", "STOP_LOSS_LIMIT", "TAKE_PROFIT_LIMIT"]

TimeInForce = Literal["GTC", "IOC", "FOK"]


@dataclass(frozen=True)
class NewOrder:
    """A new order to send to the exchange (domain request)."""

    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: float
    price: float | None = None
    time_in_force: TimeInForce | None = None
    stop_price: float | None = None
    client_order_id: str | None = None

    @property
    def requires_limit(self) -> bool:
        """Whether the order must carry a limit price."""
        return self.order_type in ("LIMIT", "STOP_LOSS_LIMIT", "TAKE_PROFIT_LIMIT")


@dataclass(frozen=True)
class OrderResult:
    """Outcome of placing an order."""

    order_id: int
    symbol: str
    side: OrderSide
    order_type: OrderType
    status: str
    price: float
    quantity: float
    executed_qty: float = 0.0
    client_order_id: str | None = None


@dataclass(frozen=True)
class OrderStatus:
    """Current state of an order on the exchange."""

    order_id: int
    symbol: str
    side: OrderSide
    order_type: OrderType
    status: str
    price: float
    quantity: float
    executed_qty: float = 0.0
    stop_price: float | None = None
    client_order_id: str | None = None


@dataclass(frozen=True)
class CancelResult:
    """Outcome of cancelling an order."""

    order_id: int
    symbol: str
    status: str
    executed_qty: float = 0.0
    client_order_id: str | None = None
