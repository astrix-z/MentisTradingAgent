"""Trade client: the exchange calls that move money (plan §6.8, §6.9).

A separate ``Spot`` client from market data, configured for mutations:

* ``timeout=15000`` ms and ``retries=0`` — the SDK retries GET and DELETE on its
  own, and a silently repeated cancel or query hides what really happened;
* ``recv_window=5000`` passed to **every** signed call (the SDK has no
  configuration field for it);
* every request draws weight from the shared :mod:`agent.exchange.budget`.

Failures come back as :class:`TradeCallError` with an :class:`Outcome`:
``REJECTED`` only when the exchange provably did not act; ``UNKNOWN`` for
anything that may have reached the matching engine (timeouts, 5xx, -1006/-1007,
broken connections, unparsable answers). The caller never retries an UNKNOWN
mutation — it reconciles.

Decimals cross the SDK boundary as fixed-point strings (``format(d, "f")``):
``str(Decimal("0.00000001"))`` is ``"1E-8"``, which the exchange rejects. The
SDK annotates these parameters as ``float`` but serializes strings unchanged,
so the conversion lives here and nowhere else. Responses are the SDK's models.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, cast

from binance_common.configuration import ConfigurationRestAPI
from binance_common.errors import (
    BadRequestError,
    ClientError,
    ForbiddenError,
    NetworkError,
    NotFoundError,
    RateLimitBanError,
    RequiredError,
    ServerError,
    TooManyRequestsError,
    UnauthorizedError,
)
from binance_common.models import ApiResponse, RateLimit
from binance_sdk_spot.rest_api.models import (
    DeleteOrderListResponse,
    DeleteOrderResponse,
    GetAccountResponse,
    GetOpenOrdersResponse,
    GetOrderListResponse,
    GetOrderResponse,
    MyTradesResponse,
    NewOrderNewOrderRespTypeEnum,
    NewOrderResponse,
    NewOrderSideEnum,
    NewOrderTimeInForceEnum,
    NewOrderTypeEnum,
    OpenOrderListResponse,
    OrderListOcoAboveTypeEnum,
    OrderListOcoBelowTimeInForceEnum,
    OrderListOcoBelowTypeEnum,
    OrderListOcoNewOrderRespTypeEnum,
    OrderListOcoResponse,
    OrderListOcoSideEnum,
    OrderTestResponse,
    OrderTestSideEnum,
    OrderTestTimeInForceEnum,
    OrderTestTypeEnum,
    TimeResponse,
)
from binance_sdk_spot.spot import Spot

from agent.exchange.budget import (
    BudgetExhausted,
    Priority,
    RequestBudget,
    budget_for,
    weight_of,
)
from agent.exchange.rest import _REST_URL, ExchangeError, Network, _credentials

TRADE_TIMEOUT_MS = 15_000
RECV_WINDOW_MS = 5_000
MAX_CLIENT_ID = 36
#: Largest page ``myTrades`` returns.
MY_TRADES_PAGE = 1000

#: Binance codes that mean "sent, outcome unknown" whatever HTTP status or SDK
#: exception class carries them (errors.md: -1006 UNEXPECTED_RESP, -1007 TIMEOUT).
_UNKNOWN_CODES = frozenset({-1006, -1007})


@dataclass(frozen=True, slots=True)
class _Shape:
    """What a successful answer of one SDK method must look like."""

    model: type
    #: Fields that must be present (not ``None``) for the answer to confirm
    #: anything — an empty ``{}`` parses into a model of all ``None``.
    required: tuple[str, ...] = ()
    many: bool = False


#: The SDK falls back to the raw JSON when its model rejects a 200 body
#: (``binance_common.utils.send_request``), so ``data()`` does not raise on an
#: answer like ``{"orderId": "not-an-integer"}``. Every call is checked against
#: its shape; a mismatch is UNKNOWN, never success.
_SHAPES: dict[str, _Shape] = {
    "new_order": _Shape(NewOrderResponse, ("order_id", "status")),
    "order_test": _Shape(OrderTestResponse),
    "order_list_oco": _Shape(OrderListOcoResponse, ("order_list_id",)),
    "delete_order_list": _Shape(DeleteOrderListResponse, ("order_list_id",)),
    "delete_order": _Shape(DeleteOrderResponse, ("order_id", "status")),
    "get_order": _Shape(GetOrderResponse, ("order_id", "status")),
    "get_order_list": _Shape(GetOrderListResponse, ("order_list_id",)),
    "get_open_orders": _Shape(GetOpenOrdersResponse, many=True),
    "open_order_list": _Shape(OpenOrderListResponse, many=True),
    "my_trades": _Shape(MyTradesResponse, many=True),
    "get_account": _Shape(GetAccountResponse),
    "time": _Shape(TimeResponse),
}


def _shape_problem(name: str, data: Any) -> str | None:
    """Why ``data`` is not a confirmed answer of ``name``; ``None`` if it is."""
    shape = _SHAPES[name]
    items = data if shape.many else [data]
    if shape.many and not isinstance(data, list):
        return f"{name}: expected a list, got {type(data).__name__}"
    for item in items:
        if not isinstance(item, shape.model):
            return f"{name}: unparsable answer ({type(item).__name__})"
        missing = [f for f in shape.required if getattr(item, f, None) is None]
        if missing:
            return f"{name}: answer lacks {missing}"
    return None


class Outcome(StrEnum):
    REJECTED = "rejected"
    UNKNOWN = "unknown"
    RATE_LIMITED = "rate_limited"
    BANNED = "banned"


class TradeCallError(ExchangeError):
    """A classified failure of one trade-client call."""

    def __init__(
        self,
        outcome: Outcome,
        message: str,
        *,
        exchange_code: int | None = None,
        http_status: int | None = None,
        retry_after: int | None = None,
        error_type: str = "",
    ) -> None:
        super().__init__(f"{outcome.value}: {message}")
        self.outcome = outcome
        self.message = message
        self.exchange_code = exchange_code
        self.http_status = http_status
        self.retry_after = retry_after
        self.error_type = error_type

    def as_record(self) -> dict[str, Any]:
        """A JSON-safe, secret-free record for ``order_attempt.error``."""
        return {
            "outcome": self.outcome.value,
            "message": self.message,
            "exchange_code": self.exchange_code,
            "http_status": self.http_status,
            "retry_after": self.retry_after,
            "error_type": self.error_type,
        }


_SIGNATURE = re.compile(r"(signature=)[^&\s'\"]+", re.IGNORECASE)


def redact(text: str, secrets: tuple[str | None, ...] = ()) -> str:
    """Remove signatures and the given secrets from a message."""
    text = _SIGNATURE.sub(r"\1***", text)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def classify(
    exc: BaseException, secrets: tuple[str | None, ...] = ()
) -> TradeCallError:
    """Map an exception raised by an SDK call to a :class:`TradeCallError`.

    ``binance-common`` 4.4.0 puts the Binance code in ``status_code`` for HTTP
    400/401/403/404/418/429 and the HTTP status for 5xx; the pairs below keep
    the two apart.
    """
    if isinstance(exc, TradeCallError):
        return exc
    kind = type(exc).__name__
    message = redact(str(getattr(exc, "error_message", None) or exc), secrets)
    code = getattr(exc, "status_code", None)
    code = code if isinstance(code, int) else None

    def error(
        outcome: Outcome,
        *,
        exchange_code: int | None = None,
        http_status: int | None = None,
        retry_after: int | None = None,
    ) -> TradeCallError:
        return TradeCallError(
            outcome,
            message,
            exchange_code=exchange_code,
            http_status=http_status,
            retry_after=retry_after,
            error_type=kind,
        )

    if isinstance(exc, ServerError):
        return error(Outcome.UNKNOWN, http_status=code)
    if code in _UNKNOWN_CODES:
        return error(Outcome.UNKNOWN, exchange_code=code)
    if isinstance(exc, TooManyRequestsError):
        return error(
            Outcome.RATE_LIMITED,
            exchange_code=code,
            http_status=429,
            retry_after=exc.retry_after,
        )
    if isinstance(exc, RateLimitBanError):
        return error(
            Outcome.BANNED,
            exchange_code=code,
            http_status=418,
            retry_after=exc.retry_after,
        )
    if isinstance(exc, BadRequestError):
        # A 400 without a Binance code is not a proven refusal.
        outcome = Outcome.REJECTED if code is not None else Outcome.UNKNOWN
        return error(outcome, exchange_code=code, http_status=400)
    if isinstance(exc, UnauthorizedError):
        return error(Outcome.REJECTED, exchange_code=code, http_status=401)
    if isinstance(exc, ForbiddenError):
        return error(Outcome.REJECTED, exchange_code=code, http_status=403)
    if isinstance(exc, NotFoundError):
        return error(Outcome.REJECTED, exchange_code=code, http_status=404)
    if isinstance(exc, RequiredError):
        return error(Outcome.REJECTED)  # never left the process
    if isinstance(exc, BudgetExhausted):
        return error(Outcome.BANNED if exc.banned else Outcome.RATE_LIMITED)
    if isinstance(exc, NetworkError | ClientError | json.JSONDecodeError):
        return error(Outcome.UNKNOWN)
    return error(Outcome.UNKNOWN)


def wire(value: Decimal | None) -> Any:
    """A Decimal as the fixed-point string the exchange expects.

    Typed ``Any`` because the SDK annotates these parameters as ``float`` while
    passing strings through unchanged; see the module docstring.
    """
    if value is None:
        return None
    if not value.is_finite() or value < 0:
        raise ValueError(f"not a valid exchange amount: {value!r}")
    return format(value, "f")


@dataclass(frozen=True, slots=True)
class TradeResult[T]:
    data: T
    http_status: int
    rate_limits: list[RateLimit] = field(default_factory=list)


def _check_client_id(*ids: str | None) -> None:
    for value in ids:
        if value is not None and not 0 < len(value) <= MAX_CLIENT_ID:
            raise ValueError(
                f"client order id must be 1..{MAX_CLIENT_ID} chars: {value!r}"
            )


class TradeClient:
    """Signed, non-retrying access to the exchange for one network."""

    def __init__(
        self,
        network: Network,
        client: Spot,
        *,
        budget: RequestBudget | None = None,
        secrets: tuple[str | None, ...] = (),
    ) -> None:
        self.network = network
        self._client = client
        self.budget = budget if budget is not None else budget_for(network)
        self._secrets = secrets
        #: Requests whose thread is still running; held so an abandoned one is
        #: not collected before its feedback is applied.
        self._inflight: set[asyncio.Future[Any]] = set()

    @classmethod
    def connect(cls, network: Network) -> TradeClient:
        api_key, api_secret = _credentials(network)
        config = ConfigurationRestAPI(
            api_key=api_key,
            api_secret=api_secret,
            base_path=_REST_URL[network],
            timeout=TRADE_TIMEOUT_MS,
            retries=0,
        )
        return cls(network, Spot(config_rest_api=config), secrets=(api_key, api_secret))

    async def _call[T](
        self,
        name: str,
        priority: Priority,
        *,
        max_wait: float | None = None,
        weight: int | None = None,
        signed: bool = True,
        **kwargs: Any,
    ) -> TradeResult[T]:
        """Spend budget, run one signed SDK method in a thread, classify."""
        try:
            await self.budget.acquire(
                priority, weight or weight_of(name), max_wait=max_wait
            )
        except BudgetExhausted as exc:
            raise classify(exc) from exc
        method: Callable[..., ApiResponse[T]] = getattr(self._client.rest_api, name)
        if signed:
            kwargs["recv_window"] = RECV_WINDOW_MS
        # The request runs in a thread that cancelling the caller cannot stop,
        # so the budget feedback (429/418/weight headers) is applied by the
        # task itself when the thread finishes, awaited or not.
        call = asyncio.ensure_future(asyncio.to_thread(self._invoke, method, kwargs))
        self._inflight.add(call)
        call.add_done_callback(self._feedback)
        response = await asyncio.shield(call)
        data = response.data()
        problem = _shape_problem(name, data)
        if problem is not None:
            raise TradeCallError(
                Outcome.UNKNOWN,
                problem,
                http_status=response.status,
                error_type="UnexpectedResponse",
            )
        return TradeResult(data, response.status, list(response.rate_limits))

    def _feedback(self, call: asyncio.Future[ApiResponse[Any]]) -> None:
        self._inflight.discard(call)
        if call.cancelled():
            return
        error = call.exception()
        if error is None:
            self.budget.observe(call.result().rate_limits)
        elif isinstance(error, TradeCallError):
            if error.outcome is Outcome.RATE_LIMITED:
                self.budget.on_rate_limited(error.retry_after)
            elif error.outcome is Outcome.BANNED:
                self.budget.on_banned(error.retry_after)

    def _invoke[T](
        self, method: Callable[..., ApiResponse[T]], kwargs: dict[str, Any]
    ) -> ApiResponse[T]:
        try:
            response = method(**kwargs)
            response.data()  # an unparsable body surfaces here, inside the thread
            return response
        except Exception as exc:  # noqa: BLE001 - classified, never swallowed
            raise classify(exc, self._secrets) from exc

    # --- orders ------------------------------------------------------------

    async def new_order(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Decimal | None = None,
        price: Decimal | None = None,
        time_in_force: str | None = None,
        client_order_id: str,
        priority: Priority,
        max_wait: float | None = None,
    ) -> TradeResult[NewOrderResponse]:
        """Place an order; the response carries fills (``FULL``)."""
        _check_client_id(client_order_id)
        return await self._call(
            "new_order",
            priority,
            max_wait=max_wait,
            symbol=symbol,
            side=NewOrderSideEnum(side),
            type=NewOrderTypeEnum(order_type),
            quantity=wire(quantity),
            price=wire(price),
            time_in_force=NewOrderTimeInForceEnum(time_in_force)
            if time_in_force
            else None,
            new_client_order_id=client_order_id,
            new_order_resp_type=NewOrderNewOrderRespTypeEnum.FULL,
        )

    async def order_test(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Decimal | None = None,
        price: Decimal | None = None,
        time_in_force: str | None = None,
    ) -> TradeResult[Any]:
        """Validate an order without placing it (dry-run stage)."""
        return await self._call(
            "order_test",
            Priority.ENTRY,
            symbol=symbol,
            side=OrderTestSideEnum(side),
            type=OrderTestTypeEnum(order_type),
            quantity=wire(quantity),
            price=wire(price),
            time_in_force=OrderTestTimeInForceEnum(time_in_force)
            if time_in_force
            else None,
        )

    async def order_list_oco(
        self,
        *,
        symbol: str,
        quantity: Decimal,
        take_profit: Decimal,
        stop: Decimal,
        stop_limit_price: Decimal | None,
        list_client_order_id: str,
        above_client_order_id: str,
        below_client_order_id: str,
    ) -> TradeResult[OrderListOcoResponse]:
        """SELL OCO protecting a long: ``LIMIT_MAKER`` take-profit above and
        ``STOP_LOSS`` below, or ``STOP_LOSS_LIMIT`` (GTC) when a limit price is
        given for symbols that do not allow a market stop (plan §6.2)."""
        _check_client_id(
            list_client_order_id, above_client_order_id, below_client_order_id
        )
        limit_leg = stop_limit_price is not None
        return await self._call(
            "order_list_oco",
            Priority.PROTECT,
            symbol=symbol,
            side=OrderListOcoSideEnum.SELL,
            quantity=wire(quantity),
            above_type=OrderListOcoAboveTypeEnum.LIMIT_MAKER,
            above_price=wire(take_profit),
            above_client_order_id=above_client_order_id,
            below_type=(
                OrderListOcoBelowTypeEnum.STOP_LOSS_LIMIT
                if limit_leg
                else OrderListOcoBelowTypeEnum.STOP_LOSS
            ),
            below_stop_price=wire(stop),
            below_price=wire(stop_limit_price),
            below_time_in_force=OrderListOcoBelowTimeInForceEnum.GTC
            if limit_leg
            else None,
            below_client_order_id=below_client_order_id,
            list_client_order_id=list_client_order_id,
            new_order_resp_type=OrderListOcoNewOrderRespTypeEnum.FULL,
        )

    async def delete_order_list(
        self, *, symbol: str, list_client_order_id: str
    ) -> TradeResult[DeleteOrderListResponse]:
        return await self._call(
            "delete_order_list",
            Priority.PROTECT,
            symbol=symbol,
            list_client_order_id=list_client_order_id,
        )

    async def delete_order(
        self, *, symbol: str, client_order_id: str
    ) -> TradeResult[DeleteOrderResponse]:
        return await self._call(
            "delete_order",
            Priority.PROTECT,
            symbol=symbol,
            orig_client_order_id=client_order_id,
        )

    # --- queries -----------------------------------------------------------

    async def get_order(
        self,
        *,
        symbol: str,
        client_order_id: str,
        priority: Priority = Priority.RECONCILE,
    ) -> TradeResult[GetOrderResponse]:
        return await self._call(
            "get_order", priority, symbol=symbol, orig_client_order_id=client_order_id
        )

    async def get_order_list(
        self, *, list_client_order_id: str, priority: Priority = Priority.RECONCILE
    ) -> TradeResult[GetOrderListResponse]:
        return await self._call(
            "get_order_list", priority, orig_client_order_id=list_client_order_id
        )

    async def open_orders(
        self, *, symbol: str, priority: Priority = Priority.RECONCILE
    ) -> TradeResult[list[GetOpenOrdersResponse]]:
        """Open orders of one symbol (for quarantine and divergence checks)."""
        return await self._call("get_open_orders", priority, symbol=symbol)

    async def open_order_list(self) -> TradeResult[list[OpenOrderListResponse]]:
        return await self._call("open_order_list", Priority.RECONCILE)

    async def my_trades(
        self,
        *,
        symbol: str,
        from_id: int | None = None,
        order_id: int | None = None,
        limit: int = MY_TRADES_PAGE,
        priority: Priority = Priority.RECONCILE,
    ) -> TradeResult[list[MyTradesResponse]]:
        """One page of the account's trades. ``from_id`` is inclusive."""
        return await self._call(
            "my_trades",
            priority,
            weight=5 if order_id is not None else None,
            symbol=symbol,
            from_id=from_id,
            order_id=order_id,
            limit=limit,
        )

    async def my_trades_since(
        self, *, symbol: str, from_id: int, priority: Priority = Priority.RECONCILE
    ) -> list[MyTradesResponse]:
        """Every trade with id ≥ ``from_id``, page by page (``fromId`` is inclusive)."""
        trades: list[MyTradesResponse] = []
        cursor = from_id
        while True:
            page = (
                await self.my_trades(symbol=symbol, from_id=cursor, priority=priority)
            ).data
            fresh = [t for t in page if t.id is not None and t.id >= cursor]
            trades.extend(fresh)
            if len(page) < MY_TRADES_PAGE or not fresh:
                return trades
            cursor = max(cast(int, t.id) for t in fresh) + 1

    async def get_account(self) -> TradeResult[GetAccountResponse]:
        return await self._call(
            "get_account", Priority.RECONCILE, omit_zero_balances=True
        )

    async def clock_offset(self) -> timedelta:
        """Exchange time minus local time, measured at the request's midpoint."""
        before = time.time()
        result: TradeResult[Any] = await self._call(
            "time", Priority.RECONCILE, signed=False
        )
        after = time.time()
        server_ms = getattr(result.data, "server_time", None)
        if not isinstance(server_ms, int):
            raise TradeCallError(Outcome.UNKNOWN, "no serverTime in /api/v3/time")
        return timedelta(milliseconds=server_ms - (before + after) * 500)


__all__ = [
    "MAX_CLIENT_ID",
    "RECV_WINDOW_MS",
    "TRADE_TIMEOUT_MS",
    "Outcome",
    "TradeCallError",
    "TradeClient",
    "TradeResult",
    "classify",
    "redact",
    "wire",
]
