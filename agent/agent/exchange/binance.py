"""Binance Spot REST client built on ``binance-sdk-spot``.

This is the only module that talks to ``binance_sdk_spot``. It creates a Spot
client for a given network (testnet / live) using the credentials from
settings and exposes account queries such as the balance.

The SDK's REST methods are synchronous/blocking, so calls are executed through
``asyncio.to_thread`` so they never block the agent's event loop.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from binance_common.configuration import ConfigurationRestAPI
from binance_sdk_spot import SPOT_REST_API_PROD_URL, SPOT_REST_API_TESTNET_URL, Spot
from binance_sdk_spot.rest_api.models.enums import (
    NewOrderSideEnum,
    NewOrderTimeInForceEnum,
    NewOrderTypeEnum,
)

from agent.core.config import settings
from agent.exchange.order_types import (
    CancelResult,
    NewOrder,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
)

logger = logging.getLogger(__name__)


class ExchangeError(RuntimeError):
    """Raised when a Binance request/connection fails."""


@dataclass(frozen=True)
class AssetBalance:
    asset: str
    free: float
    locked: float


def _network_credentials(
    network: str,
) -> tuple[str, str]:
    """Return (api_key, api_secret) for the given network from settings."""
    if network == "live":
        key, secret = settings.BINANCE_API_KEY, settings.BINANCE_API_SECRET
    elif network == "testnet":
        key, secret = settings.BINANCE_TESTNET_API_KEY, settings.BINANCE_TESTNET_API_SECRET
    else:
        raise ExchangeError(f"unknown network: {network!r}")

    if not key or not secret:
        raise ExchangeError(
            f"Binance credentials for network={network!r} are not configured"
        )
    return key, secret


def _to_float(value: str | None) -> float:
    try:
        return float(value) if value not in (None, "") else 0.0
    except ValueError:
        return 0.0


def _to_side(side: OrderSide) -> NewOrderSideEnum:
    return NewOrderSideEnum[side]


def _to_type(order_type: OrderType) -> NewOrderTypeEnum:
    return NewOrderTypeEnum[order_type]


def _to_time_in_force(time_in_force: TimeInForce | None) -> NewOrderTimeInForceEnum | None:
    if time_in_force is None:
        return None
    return NewOrderTimeInForceEnum[time_in_force]


def _side_of(value: str | None) -> OrderSide:
    # ``cast`` only helps type-checkers; exchange always returns BUY/SELL.
    return value.upper() if value in ("BUY", "SELL") else "BUY"  # type: ignore[return-value]


def _type_of(value: str | None) -> OrderType:
    supported = ("LIMIT", "MARKET", "STOP_LOSS_LIMIT", "TAKE_PROFIT_LIMIT")
    return value if value in supported else "MARKET"  # type: ignore[return-value]


def _order_status_from(model: object, *, fallback_symbol: str = "") -> OrderStatus:
    """Build an :class:`OrderStatus` from any Binance order model.

    The SDK's order models (new/get/open/cancel) share the same field names;
    reading via ``getattr`` keeps this independent of the exact generated class.
    """
    symbol = _attr(model, "symbol")
    client_order_id = _attr(model, "client_order_id")
    stop_price = _to_float(_attr(model, "stop_price"))
    return OrderStatus(
        order_id=int(_attr(model, "order_id") or 0),
        symbol=str(symbol or fallback_symbol).upper(),
        side=_side_of(_attr(model, "side")),
        order_type=_type_of(_attr(model, "type")),
        status=str(_attr(model, "status") or "UNKNOWN"),
        price=_to_float(_attr(model, "price")),
        quantity=_to_float(_attr(model, "orig_qty")),
        executed_qty=_to_float(_attr(model, "executed_qty")),
        stop_price=stop_price if stop_price > 0 else None,
        client_order_id=str(client_order_id) if client_order_id is not None else None,
    )


def _attr(model: object, name: str) -> Any:
    """Return a model attribute that may be ``None`` without raising."""
    return getattr(model, name, None)


class BinanceSpot:
    """Thin async wrapper around a ``binance_sdk_spot.Spot`` REST client."""

    def __init__(self, spot: Spot) -> None:
        self._spot = spot

    @classmethod
    def connect(cls, network: str) -> BinanceSpot:
        """Build a client for ``network`` using the matching credentials."""
        api_key, api_secret = _network_credentials(network)
        base_path = (
            SPOT_REST_API_PROD_URL if network == "live" else SPOT_REST_API_TESTNET_URL
        )
        config = ConfigurationRestAPI(
            api_key=api_key,
            api_secret=api_secret,
            base_path=base_path,
        )
        return cls(Spot(config_rest_api=config))

    async def fetch_balances(self) -> list[AssetBalance]:
        """Return non-zero account balances (free + locked per asset)."""
        return await asyncio.to_thread(self._fetch_balances_sync)

    def _fetch_balances_sync(self) -> list[AssetBalance]:
        try:
            response = self._spot.rest_api.get_account(omit_zero_balances=True)
        except Exception as exc:  # noqa: BLE001 - SDK raises many typed errors
            raise ExchangeError(f"failed to fetch account balance: {exc}") from exc

        account = response.data()
        balances: list[AssetBalance] = []
        for item in account.balances or []:
            if item.asset is None:
                continue
            balances.append(
                AssetBalance(
                    asset=item.asset,
                    free=_to_float(item.free),
                    locked=_to_float(item.locked),
                )
            )
        return balances

    # --- Order management (thin, network-scoped, async wrappers) ----------

    async def place_order(self, order: NewOrder) -> OrderResult:
        """Send a new order to the exchange and return its outcome."""
        return await asyncio.to_thread(self._place_order_sync, order)

    def _place_order_sync(self, order: NewOrder) -> OrderResult:
        try:
            response = self._spot.rest_api.new_order(
                symbol=order.symbol,
                side=_to_side(order.side),
                type=_to_type(order.order_type),
                time_in_force=_to_time_in_force(order.time_in_force),
                quantity=order.quantity,
                price=order.price,
                stop_price=order.stop_price,
                new_client_order_id=order.client_order_id,
            )
        except Exception as exc:  # noqa: BLE001 - SDK raises many typed errors
            raise ExchangeError(f"failed to place order: {exc}") from exc
        model = response.data()
        return OrderResult(
            order_id=int(model.order_id or 0),
            symbol=order.symbol.upper(),
            side=order.side,
            order_type=order.order_type,
            status=str(model.status or "UNKNOWN"),
            price=_to_float(model.price) or order.price or 0.0,
            quantity=_to_float(model.orig_qty) or order.quantity,
            executed_qty=_to_float(model.executed_qty),
            client_order_id=order.client_order_id,
        )

    async def cancel_order(
        self,
        *,
        symbol: str,
        order_id: int | None = None,
        client_order_id: str | None = None,
    ) -> CancelResult:
        """Cancel an open order (by id or client id)."""
        return await asyncio.to_thread(
            self._cancel_order_sync, symbol, order_id, client_order_id
        )

    def _cancel_order_sync(
        self,
        symbol: str,
        order_id: int | None,
        client_order_id: str | None,
    ) -> CancelResult:
        try:
            response = self._spot.rest_api.delete_order(
                symbol=symbol,
                order_id=order_id,
                orig_client_order_id=client_order_id,
            )
        except Exception as exc:  # noqa: BLE001
            raise ExchangeError(f"failed to cancel order: {exc}") from exc
        model = response.data()
        order_id = int(model.order_id or 0)
        client_id = getattr(model, "client_order_id", None) or client_order_id
        return CancelResult(
            order_id=order_id,
            symbol=str(model.symbol or symbol).upper(),
            status=str(model.status or "UNKNOWN"),
            executed_qty=_to_float(model.executed_qty),
            client_order_id=str(client_id) if client_id is not None else None,
        )

    async def get_order(
        self,
        *,
        symbol: str,
        order_id: int | None = None,
        client_order_id: str | None = None,
    ) -> OrderStatus | None:
        """Return the current state of a single order."""
        return await asyncio.to_thread(
            self._get_order_sync, symbol, order_id, client_order_id
        )

    def _get_order_sync(
        self,
        symbol: str,
        order_id: int | None,
        client_order_id: str | None,
    ) -> OrderStatus | None:
        try:
            response = self._spot.rest_api.get_order(
                symbol=symbol,
                order_id=order_id,
                orig_client_order_id=client_order_id,
            )
        except Exception as exc:  # noqa: BLE001
            raise ExchangeError(f"failed to fetch order: {exc}") from exc
        model = response.data()
        if model is None:
            return None
        return _order_status_from(model, fallback_symbol=symbol)

    async def open_orders(self, symbol: str | None = None) -> list[OrderStatus]:
        """List currently open orders (optionally for one symbol)."""
        return await asyncio.to_thread(self._open_orders_sync, symbol)

    def _open_orders_sync(self, symbol: str | None) -> list[OrderStatus]:
        try:
            response = self._spot.rest_api.get_open_orders(symbol=symbol)
        except Exception as exc:  # noqa: BLE001
            raise ExchangeError(f"failed to list open orders: {exc}") from exc
        data = response.data()
        items = data if isinstance(data, list) else ([data] if data is not None else [])
        return [_order_status_from(item, fallback_symbol=symbol or "") for item in items]
