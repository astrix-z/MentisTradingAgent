"""Binance Spot REST access — a thin adapter over ``binance_sdk_spot``.

This is the **only** module in the agent that talks to the REST API directly.
It deliberately does not define its own order/quote/balance models: every call
returns the SDK's own response objects (``NewOrderResponse``,
``GetAccountResponseBalancesInner``, ...). If we need a value from the exchange,
it comes from the SDK; we never re-implement it.

The SDK's REST methods are synchronous/blocking, so each call is dispatched
through :func:`asyncio.to_thread` to keep the agent's event loop free.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from binance_common.configuration import ConfigurationRestAPI
from binance_sdk_spot import (
    SPOT_REST_API_PROD_URL,
    SPOT_REST_API_TESTNET_URL,
)
from binance_sdk_spot.rest_api.models import (
    DeleteOrderResponse,
    GetAccountResponse,
    GetAccountResponseBalancesInner,
    GetOrderResponse,
    KlinesIntervalEnum,
    NewOrderResponse,
    NewOrderSideEnum,
    NewOrderTimeInForceEnum,
    NewOrderTypeEnum,
)
from binance_sdk_spot.spot import Spot

from agent.core.config import settings

Network = Literal["testnet", "live"]

# REST base URL per network (single source of truth is the SDK constants).
_REST_URL: dict[str, str] = {
    "live": SPOT_REST_API_PROD_URL,
    "testnet": SPOT_REST_API_TESTNET_URL,
}


class ExchangeError(RuntimeError):
    """Raised when a Binance REST request cannot be completed."""


def _credentials(network: Network) -> tuple[str, str]:
    """Return ``(api_key, api_secret)`` for a network, or raise."""
    if network == "live":
        key, secret = settings.BINANCE_API_KEY, settings.BINANCE_API_SECRET
    elif network == "testnet":
        key = settings.BINANCE_TESTNET_API_KEY
        secret = settings.BINANCE_TESTNET_API_SECRET
    else:  # pragma: no cover - guarded by the Literal type
        raise ExchangeError(f"unknown network: {network!r}")

    if not key or not secret:
        raise ExchangeError(
            f"Binance credentials for network={network!r} are not configured"
        )
    return key, secret


def balance_map(balances: list[GetAccountResponseBalancesInner]) -> dict[str, float]:
    """Collapse SDK balance rows into ``{asset: free}`` for non-zero assets."""
    result: dict[str, float] = {}
    for row in balances:
        if row.asset is None:
            continue
        free = _to_float(row.free)
        locked = _to_float(row.locked)
        if free > 0 or locked > 0:
            result[row.asset] = free
    return result


def _to_float(value: Any) -> float:
    """Best-effort conversion of the SDK's string numerics to ``float``."""
    try:
        return float(value) if value not in (None, "") else 0.0
    except TypeError, ValueError:
        return 0.0


class ExchangeRest:
    """Network-scoped, async facade over ``Spot.rest_api``."""

    def __init__(self, network: Network, client: Spot) -> None:
        self._network = network
        self._client = client

    @property
    def network(self) -> Network:
        return self._network

    @classmethod
    def connect(cls, network: Network) -> ExchangeRest:
        """Build a REST client for ``network`` using the matching credentials."""
        api_key, api_secret = _credentials(network)
        config = ConfigurationRestAPI(
            api_key=api_key,
            api_secret=api_secret,
            base_path=_REST_URL[network],
        )
        return cls(network, Spot(config_rest_api=config))

    def _call(self, method: Any, /, **kwargs: Any) -> Any:
        """Run a blocking SDK method in a worker thread, wrapping failures."""
        try:
            response = method(**kwargs)
            return response.data()
        except ExchangeError:
            raise
        except Exception as exc:  # noqa: BLE001 - SDK raises many typed errors
            raise ExchangeError(f"Binance REST call failed: {exc}") from exc

    # --- account -----------------------------------------------------------

    async def account(self) -> GetAccountResponse:
        """Return the full account snapshot."""
        return await asyncio.to_thread(
            self._call, self._client.rest_api.get_account, omit_zero_balances=True
        )

    async def balances(self) -> list[GetAccountResponseBalancesInner]:
        """Return the account's balance rows (SDK model, strings for amounts)."""
        account = await self.account()
        return account.balances or []

    # --- market data -------------------------------------------------------

    async def klines(
        self,
        *,
        symbol: str,
        interval: str,
        limit: int | None = None,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list[Any]:
        """Return raw kline arrays (``open_time, open, high, low, close, ...``)."""
        return await asyncio.to_thread(
            self._call,
            self._client.rest_api.klines,
            symbol=symbol,
            interval=KlinesIntervalEnum(interval).value,
            limit=limit,
            start_time=start_time,
            end_time=end_time,
        )

    async def ticker_price(self, symbol: str) -> Any:
        """Return the latest price for a symbol."""
        return await asyncio.to_thread(
            self._call, self._client.rest_api.ticker_price, symbol=symbol
        )

    async def exchange_info(self, symbol: str | None = None) -> Any:
        """Return exchange metadata (filters, symbol status, ...)."""
        return await asyncio.to_thread(
            self._call, self._client.rest_api.exchange_info, symbol=symbol
        )

    # --- orders ------------------------------------------------------------

    async def new_order(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str,
        quantity: float | None = None,
        quote_order_qty: float | None = None,
        price: float | None = None,
        time_in_force: str | None = None,
        stop_price: float | None = None,
        client_order_id: str | None = None,
    ) -> NewOrderResponse:
        """Place a new order and return the SDK response model."""
        return await asyncio.to_thread(
            self._call,
            self._client.rest_api.new_order,
            symbol=symbol,
            side=NewOrderSideEnum(side),
            type=NewOrderTypeEnum(order_type),
            quantity=quantity,
            quote_order_qty=quote_order_qty,
            price=price,
            time_in_force=(
                NewOrderTimeInForceEnum(time_in_force) if time_in_force else None
            ),
            stop_price=stop_price,
            new_client_order_id=client_order_id,
        )

    async def cancel_order(
        self,
        *,
        symbol: str,
        order_id: int | None = None,
        client_order_id: str | None = None,
    ) -> DeleteOrderResponse:
        """Cancel a resting order by exchange id or client id."""
        return await asyncio.to_thread(
            self._call,
            self._client.rest_api.delete_order,
            symbol=symbol,
            order_id=order_id,
            orig_client_order_id=client_order_id,
        )

    async def open_orders(self, symbol: str | None = None) -> list[Any]:
        """List currently open orders, optionally scoped to one symbol."""
        return await asyncio.to_thread(
            self._call, self._client.rest_api.get_open_orders, symbol=symbol
        )

    async def get_order(
        self,
        *,
        symbol: str,
        order_id: int | None = None,
        client_order_id: str | None = None,
    ) -> GetOrderResponse:
        """Return the current state of a single order."""
        return await asyncio.to_thread(
            self._call,
            self._client.rest_api.get_order,
            symbol=symbol,
            order_id=order_id,
            orig_client_order_id=client_order_id,
        )


__all__ = [
    "ExchangeError",
    "ExchangeRest",
    "Network",
    "balance_map",
]
