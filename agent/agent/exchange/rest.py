"""Binance Spot REST access — a thin adapter over ``binance_sdk_spot``.

This module is the market-data and account-read side; orders go through
:mod:`agent.exchange.trade`, which has its own non-retrying client.
It deliberately does not define its own order/quote/balance models: every call
returns the SDK's own response objects (``NewOrderResponse``,
``GetAccountResponseBalancesInner``, ...). If we need a value from the exchange,
it comes from the SDK; we never re-implement it.

The SDK's REST methods are synchronous/blocking, so each call is dispatched
through :func:`asyncio.to_thread` to keep the agent's event loop free.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from binance_common.configuration import ConfigurationRestAPI
from binance_common.errors import RateLimitBanError, TooManyRequestsError
from binance_sdk_spot import (
    SPOT_REST_API_PROD_URL,
    SPOT_REST_API_TESTNET_URL,
)
from binance_sdk_spot.rest_api.models import (
    AvgPriceResponse,
    ExchangeInfoResponseSymbolsInner,
    GetAccountResponse,
    GetAccountResponseBalancesInner,
    GetTradesResponse,
    KlinesIntervalEnum,
    ReferencePriceResponse,
    Ticker24hrResponse2Inner,
    TickerBookTickerResponse,
)
from binance_sdk_spot.spot import Spot

from agent.core.config import settings
from agent.exchange.budget import (
    BudgetExhausted,
    Priority,
    RequestBudget,
    budget_for,
    weight_of,
)

Network = Literal["testnet", "live"]

# REST base URL per network (single source of truth is the SDK constants).
_REST_URL: dict[str, str] = {
    "live": SPOT_REST_API_PROD_URL,
    "testnet": SPOT_REST_API_TESTNET_URL,
}


class ExchangeError(RuntimeError):
    """Raised when a Binance REST request cannot be completed."""


class UnknownSymbol(ExchangeError, LookupError):
    """The exchange does not list the symbol (-1121): never, or no longer."""


#: Binance's "Invalid symbol."
INVALID_SYMBOL = -1121


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


def _to_decimal(value: Any) -> Decimal:
    """Best-effort conversion of the SDK's string numerics to ``Decimal``."""
    try:
        return Decimal(str(value)) if value not in (None, "") else Decimal(0)
    except InvalidOperation, TypeError, ValueError:
        return Decimal(0)


def balance_snapshot(
    balances: list[GetAccountResponseBalancesInner],
) -> list[dict[str, str]]:
    """Non-zero balance rows as ``{asset, free, locked}`` with Decimal-precise
    string amounts (never binary float), preserving ``locked`` that
    :func:`balance_map` collapses away. An asset is included whenever
    ``free`` or ``locked`` is non-zero."""
    result: list[dict[str, str]] = []
    for row in balances:
        if row.asset is None:
            continue
        free = _to_decimal(row.free)
        locked = _to_decimal(row.locked)
        if free > 0 or locked > 0:
            result.append(
                {"asset": row.asset, "free": str(free), "locked": str(locked)}
            )
    return result


class ExchangeRest:
    """Network-scoped, async facade over ``Spot.rest_api``."""

    def __init__(
        self, network: Network, client: Spot, budget: RequestBudget | None = None
    ) -> None:
        self._network = network
        self._client = client
        #: Market data draws from the same per-IP budget as trading, at the
        #: lowest priority (plan §6.9).
        self.budget = budget if budget is not None else budget_for(network)

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

    @classmethod
    def connect_public(cls, network: Network) -> ExchangeRest:
        """Build a REST client for ``network`` without credentials.

        Exchange metadata and 24h statistics are public endpoints. Requiring
        keys for them would make the symbol universe unavailable to an agent
        configured for only one network (or for none yet), and it is the same
        SDK either way — the rule is that exchange access goes through this
        adapter, not that every call is authenticated.
        """
        config = ConfigurationRestAPI(base_path=_REST_URL[network])
        return cls(network, Spot(config_rest_api=config))

    def _call(self, method: Any, /, **kwargs: Any) -> Any:
        """Run a blocking SDK method in a worker thread, wrapping failures.

        Feeds the shared budget: the exchange's weight count on success, a
        pause on 429 and a ban on 418.
        """
        try:
            response = method(**kwargs)
            self.budget.observe(response.rate_limits)
            return response.data()
        except ExchangeError:
            raise
        except TooManyRequestsError as exc:
            self.budget.on_rate_limited(exc.retry_after)
            raise ExchangeError(f"Binance REST call failed: {exc}") from exc
        except RateLimitBanError as exc:
            self.budget.on_banned(exc.retry_after)
            raise ExchangeError(f"Binance REST call failed: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - SDK raises many typed errors
            raise ExchangeError(f"Binance REST call failed: {exc}") from exc

    async def _market(
        self,
        method: Any,
        /,
        *,
        priority: Priority = Priority.MARKET,
        max_wait: float | None = None,
        **kwargs: Any,
    ) -> Any:
        """Run a read under the shared budget (market-data priority by default)."""
        try:
            await self.budget.acquire(
                priority, weight_of(method.__name__), max_wait=max_wait
            )
        except BudgetExhausted as exc:
            raise ExchangeError(f"Binance REST call not sent: {exc}") from exc
        return await asyncio.to_thread(self._call, method, **kwargs)

    # --- account -----------------------------------------------------------

    async def account(self) -> GetAccountResponse:
        """Return the full account snapshot."""
        account: GetAccountResponse = await self._market(
            self._client.rest_api.get_account, omit_zero_balances=True
        )
        return account

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
        rows: list[Any] = await self._market(
            self._client.rest_api.klines,
            symbol=symbol,
            interval=KlinesIntervalEnum(interval).value,
            limit=limit,
            start_time=start_time,
            end_time=end_time,
        )
        return rows

    async def ticker_price(self, symbol: str) -> Any:
        """Return the latest price for a symbol."""
        return await self._market(self._client.rest_api.ticker_price, symbol=symbol)

    async def exchange_info(self, symbol: str | None = None) -> Any:
        """Return exchange metadata (filters, symbol status, ...).

        A ``symbol`` the exchange does not list raises :class:`UnknownSymbol`
        — after a delisting that is the only answer there is.
        """
        try:
            return await self._market(
                self._client.rest_api.exchange_info, symbol=symbol
            )
        except ExchangeError as exc:
            code = getattr(exc.__cause__, "status_code", None)
            if symbol is not None and code == INVALID_SYMBOL:
                raise UnknownSymbol(f"{symbol} is not listed") from exc
            raise

    async def symbols(self) -> list[ExchangeInfoResponseSymbolsInner]:
        """Every symbol the exchange lists, with its metadata.

        One call, no ``quoteAsset`` parameter exists: filtering by quote asset
        and status is the caller's job (see :mod:`agent.symbols`).
        """
        info = await self.exchange_info()
        return list(info.symbols or [])

    # --- quotes for execution (plan §9.8) ----------------------------------

    async def last_trades(
        self, symbol: str, *, priority: Priority, max_wait: float | None = None
    ) -> list[GetTradesResponse]:
        """The most recent trade (``GET /api/v3/trades?limit=1``).

        The SDK annotates this endpoint as a single model; it returns a list.
        """
        trades: list[GetTradesResponse] = await self._market(
            self._client.rest_api.get_trades,
            priority=priority,
            max_wait=max_wait,
            symbol=symbol,
            limit=1,
        )
        return trades

    async def avg_price(
        self, symbol: str, *, priority: Priority, max_wait: float | None = None
    ) -> AvgPriceResponse:
        result: AvgPriceResponse = await self._market(
            self._client.rest_api.avg_price,
            priority=priority,
            max_wait=max_wait,
            symbol=symbol,
        )
        return result

    async def reference_price(
        self, symbol: str, *, priority: Priority, max_wait: float | None = None
    ) -> ReferencePriceResponse:
        result: ReferencePriceResponse = await self._market(
            self._client.rest_api.reference_price,
            priority=priority,
            max_wait=max_wait,
            symbol=symbol,
        )
        return result

    async def book_ticker(
        self, symbol: str, *, priority: Priority, max_wait: float | None = None
    ) -> TickerBookTickerResponse:
        result: TickerBookTickerResponse = await self._market(
            self._client.rest_api.ticker_book_ticker,
            priority=priority,
            max_wait=max_wait,
            symbol=symbol,
        )
        return result

    async def tickers_24h(self) -> dict[str, Ticker24hrResponse2Inner]:
        """24h statistics per symbol, keyed by symbol.

        The endpoint returns either one object or a list depending on the
        parameters; asked without any, it returns every symbol.
        """
        response = await self._market(self._client.rest_api.ticker24hr)
        payload = getattr(response, "actual_instance", None)
        if payload is None:
            return {}
        entries = payload if isinstance(payload, list) else [payload]
        return {entry.symbol: entry for entry in entries if entry.symbol}


__all__ = [
    "ExchangeError",
    "ExchangeRest",
    "Network",
    "balance_map",
]
