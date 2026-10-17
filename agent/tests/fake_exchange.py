"""A controllable in-memory exchange at the SDK boundary (EXEC-12).

:class:`FakeExchange` stands in for ``Spot.rest_api``: it has the SDK's method
names and keyword arguments, returns ``ApiResponse`` objects whose data are the
SDK's own models (built with ``from_dict`` from Binance-shaped JSON) and raises
the SDK's own exceptions. Everything above it — :class:`TradeClient`, error
classification, the request budget, the runtime — is production code.

It is not an HTTP mock and not a second order protocol: it only answers the
questions the real exchange answers, with the real field names and statuses.

**Its lifetime is independent of the runtime.** A test keeps one exchange and
builds runtimes on top of it as often as it likes; orders, locked balances,
trades, delayed answers and requests already accepted all survive. A request
whose caller stopped waiting (cancelled coroutine, dead runtime) still runs to
completion in its worker thread, exactly like a real one: see :meth:`gate`.

Scripting, per SDK method name (see :meth:`fault`):

* ``reject`` (code), ``rate_limit``, ``ban`` — refused before acting;
* ``timeout_before`` — lost before the exchange saw it;
* ``timeout_after`` / ``server_error_after`` — acted on, answer lost;
* ``no_fills`` — acted on, response without ``fills``;
* ``not_found`` (``times``) — queries answer -2013 although the order exists
  (the delayed not-found of an order that is still propagating).

Trades can be withheld from ``my_trades`` (:meth:`hide_trades`) to model a
history that lags the order status.
"""

from __future__ import annotations

import itertools
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from typing import Any

from binance_common.errors import (
    BadRequestError,
    NetworkError,
    RateLimitBanError,
    ServerError,
    TooManyRequestsError,
)
from binance_common.models import ApiResponse
from binance_sdk_spot.rest_api.models import (
    AvgPriceResponse,
    DeleteOrderListResponse,
    DeleteOrderResponse,
    ExchangeInfoResponse,
    GetAccountResponse,
    GetOpenOrdersResponse,
    GetOrderListResponse,
    GetOrderResponse,
    GetTradesResponse,
    MyTradesResponse,
    NewOrderResponse,
    OpenOrderListResponse,
    OrderListOcoResponse,
    ReferencePriceResponse,
    TickerBookTickerResponse,
    TickerPriceResponse,
    TimeResponse,
)
from binance_sdk_spot.spot import Spot

EIGHT = Decimal("0.00000001")

UNKNOWN_ORDER = -2013  # "Order does not exist."
CANCEL_REJECTED = -2011  # "Unknown order sent."
NEW_ORDER_REJECTED = -2010


def fmt(value: Decimal) -> str:
    return format(value.quantize(EIGHT, rounding=ROUND_DOWN), "f")


def dec(value: Any) -> Decimal:
    if value is None:
        raise BadRequestError("Mandatory parameter was not sent.", -1102)
    return Decimal(str(value))


def enum_value(value: Any) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", value))


def response[T](
    model: Any, payload: Any, headers: dict[str, str] | None = None
) -> ApiResponse[T]:
    data = model.from_dict(payload) if model is not None else payload
    return ApiResponse(
        data_function=lambda: data, status=200, headers=headers or {}, rate_limits=[]
    )


@dataclass
class SymbolRules:
    symbol: str
    base: str
    quote: str = "USDT"
    tick: Decimal = Decimal("0.01")
    step: Decimal = Decimal("0.00001")
    min_qty: Decimal = Decimal("0.00001")
    max_qty: Decimal = Decimal("9000")
    min_notional: Decimal = Decimal("5")
    max_notional: Decimal = Decimal("9000000")
    avg_price_mins: int = 5
    order_types: tuple[str, ...] = (
        "LIMIT",
        "LIMIT_MAKER",
        "MARKET",
        "STOP_LOSS",
        "STOP_LOSS_LIMIT",
        "TAKE_PROFIT",
        "TAKE_PROFIT_LIMIT",
    )
    oco_allowed: bool = True
    status: str = "TRADING"
    extra_filters: list[dict[str, Any]] = field(default_factory=list)

    def document(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "status": self.status,
            "baseAsset": self.base,
            "quoteAsset": self.quote,
            "baseAssetPrecision": 8,
            "quotePrecision": 8,
            "orderTypes": list(self.order_types),
            "ocoAllowed": self.oco_allowed,
            "otoAllowed": True,
            "isSpotTradingAllowed": True,
            "permissionSets": [["SPOT"]],
            "filters": [
                {
                    "filterType": "PRICE_FILTER",
                    "minPrice": fmt(self.tick),
                    "maxPrice": "1000000.00000000",
                    "tickSize": fmt(self.tick),
                },
                {
                    "filterType": "LOT_SIZE",
                    "minQty": fmt(self.min_qty),
                    "maxQty": fmt(self.max_qty),
                    "stepSize": fmt(self.step),
                },
                {
                    "filterType": "MARKET_LOT_SIZE",
                    "minQty": "0.00000000",
                    "maxQty": fmt(self.max_qty),
                    "stepSize": "0.00000000",
                },
                {
                    "filterType": "NOTIONAL",
                    "minNotional": fmt(self.min_notional),
                    "applyMinToMarket": True,
                    "maxNotional": fmt(self.max_notional),
                    "applyMaxToMarket": False,
                    "avgPriceMins": self.avg_price_mins,
                },
                {
                    "filterType": "PERCENT_PRICE_BY_SIDE",
                    "bidMultiplierUp": "5",
                    "bidMultiplierDown": "0.2",
                    "askMultiplierUp": "5",
                    "askMultiplierDown": "0.2",
                    "avgPriceMins": self.avg_price_mins,
                },
                *self.extra_filters,
            ],
        }


@dataclass
class Order:
    order_id: int
    symbol: str
    client_order_id: str
    side: str
    type: str
    quantity: Decimal
    price: Decimal | None
    stop_price: Decimal | None
    time_in_force: str | None
    time: int
    order_list_id: int = -1
    status: str = "NEW"
    executed: Decimal = Decimal(0)
    quote: Decimal = Decimal(0)
    triggered: bool = False
    update_time: int = 0

    @property
    def open(self) -> bool:
        return self.status in ("NEW", "PARTIALLY_FILLED")

    def document(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "orderId": self.order_id,
            "orderListId": self.order_list_id,
            "clientOrderId": self.client_order_id,
            "price": fmt(self.price or Decimal(0)),
            "origQty": fmt(self.quantity),
            "executedQty": fmt(self.executed),
            "cummulativeQuoteQty": fmt(self.quote),
            "origQuoteOrderQty": "0.00000000",
            "status": self.status,
            "timeInForce": self.time_in_force or "GTC",
            "type": self.type,
            "side": self.side,
            "stopPrice": fmt(self.stop_price or Decimal(0)),
            "icebergQty": "0.00000000",
            "time": self.time,
            "updateTime": self.update_time or self.time,
            "isWorking": self.triggered or self.type in ("LIMIT", "LIMIT_MAKER"),
            "workingTime": self.time,
            "selfTradePreventionMode": "EXPIRE_MAKER",
        }


@dataclass
class OrderList:
    order_list_id: int
    symbol: str
    list_client_order_id: str
    orders: list[Order]
    time: int
    status: str = "EXECUTING"  # listOrderStatus
    status_type: str = "EXEC_STARTED"  # listStatusType

    def document(self, *, reports: bool = False) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "orderListId": self.order_list_id,
            "contingencyType": "OCO",
            "listStatusType": self.status_type,
            "listOrderStatus": self.status,
            "listClientOrderId": self.list_client_order_id,
            "transactionTime": self.time,
            "symbol": self.symbol,
            "orders": [
                {
                    "symbol": o.symbol,
                    "orderId": o.order_id,
                    "clientOrderId": o.client_order_id,
                }
                for o in self.orders
            ],
        }
        if reports:
            doc["orderReports"] = [o.document() for o in self.orders]
        return doc


@dataclass
class Fault:
    kind: str
    code: int | None = None
    times: int = 1
    retry_after: int | None = None


class FakeExchange:
    """In-memory spot exchange; see the module docstring."""

    def __init__(
        self,
        *,
        uid: int = 1001,
        balances: dict[str, Decimal] | None = None,
        commission_rate: Decimal = Decimal("0.001"),
        commission_asset: str = "BNB",
        bnb_price: Decimal = Decimal(600),
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.uid = uid
        self.permissions = ["SPOT"]
        self.can_trade = True
        self.free: dict[str, Decimal] = dict(balances or {"USDT": Decimal(100)})
        self.locked: dict[str, Decimal] = {}
        self.commission_rate = commission_rate
        #: "BNB", "base" (the asset received: base on BUY, quote on SELL) or
        #: "quote".
        self.commission_asset = commission_asset
        self.bnb_price = bnb_price
        #: Tests: the agent's commission price source (not the exchange) is down.
        self.valuation_down = False
        self._clock = clock or (lambda: 1_800_000_000_000)
        self.rules: dict[str, SymbolRules] = {}
        self.last: dict[str, Decimal] = {}
        self.last_time: dict[str, int] = {}
        self.avg: dict[str, tuple[Decimal, int]] = {}
        self.reference: dict[str, Decimal | None] = {}
        self.book: dict[str, tuple[Decimal, Decimal]] = {}
        self.klines_data: dict[tuple[str, str], list[list[Any]]] = {}
        self.orders: dict[int, Order] = {}
        self.lists: dict[int, OrderList] = {}
        self.trades: list[dict[str, Any]] = []
        self.hidden_orders: set[int] = set()
        self.faults: dict[str, list[Fault]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._gates: dict[str, threading.Event] = {}
        self._entered: dict[str, threading.Event] = {}
        self._ids = itertools.count(1)
        self._list_ids = itertools.count(1)
        self._trade_ids = itertools.count(1)
        self._lock = threading.RLock()
        # Next outcome of a FOK BUY: "fill" (at the last price), "expire".
        self.fok_outcome = "fill"
        # A MARKET SELL fills at most this fraction (1 = full; less = EXPIRED).
        self.market_fill_ratio = Decimal(1)

    # --- setup -------------------------------------------------------------

    def spot(self) -> Spot:
        """A real ``Spot`` whose ``rest_api`` is this exchange."""
        spot = Spot()
        setattr(spot, "_rest_api", self)  # noqa: B010 - the SDK's own lazy slot
        return spot

    def list_symbol(self, rules: SymbolRules, price: Decimal) -> None:
        with self._lock:
            self.rules[rules.symbol] = rules
            self.set_price(rules.symbol, price)
            self.avg[rules.symbol] = (price, rules.avg_price_mins)
            self.reference[rules.symbol] = None

    def now(self) -> int:
        return self._clock()

    @staticmethod
    def _trading(rules: SymbolRules) -> None:
        """New orders on a symbol out of ``TRADING`` are refused; cancels are not."""
        if rules.status != "TRADING":
            raise BadRequestError("Market is closed.", NEW_ORDER_REJECTED)

    # --- scripting ---------------------------------------------------------

    def fault(
        self,
        method: str,
        kind: str,
        *,
        code: int | None = None,
        times: int = 1,
        retry_after: int | None = None,
    ) -> None:
        self.faults.setdefault(method, []).append(Fault(kind, code, times, retry_after))

    def gate(self, method: str) -> tuple[threading.Event, threading.Event]:
        """Hold ``method``'s next calls until the returned *release* is set.

        Returns ``(entered, release)``: ``entered`` is set once a call is
        blocked. A blocked call has already been accepted when the ``*_after``
        fault or plain success applies; it finishes when released, whoever is
        still waiting for it.
        """
        release = threading.Event()
        entered = threading.Event()
        self._gates[method] = release
        self._entered[method] = entered
        return entered, release

    def hide_trades(self, order_id: int) -> None:
        """Keep ``order_id``'s trades out of ``my_trades`` until revealed."""
        self.hidden_orders.add(order_id)

    def reveal_trades(self) -> None:
        self.hidden_orders.clear()

    def _take_fault(self, method: str, kinds: tuple[str, ...]) -> Fault | None:
        with self._lock:
            queue = self.faults.get(method, [])
            for fault in queue:
                if fault.kind in kinds:
                    fault.times -= 1
                    if fault.times <= 0:
                        queue.remove(fault)
                    return fault
            return None

    def _before(self, method: str, kwargs: dict[str, Any]) -> None:
        self.calls.append((method, kwargs))
        fault = self._take_fault(
            method, ("reject", "rate_limit", "ban", "timeout_before")
        )
        if fault is None:
            return
        if fault.kind == "reject":
            raise BadRequestError(
                "Rejected by script.", fault.code or NEW_ORDER_REJECTED
            )
        if fault.kind == "rate_limit":
            raise TooManyRequestsError("Too many requests.", -1003, fault.retry_after)
        if fault.kind == "ban":
            raise RateLimitBanError("IP banned.", -1003, fault.retry_after)
        raise NetworkError("Network error: read timed out (before)")

    def _after(self, method: str) -> Fault | None:
        with self._lock:
            gate = self._gates.pop(method, None)
            entered = self._entered.pop(method, None)
        if gate is not None and entered is not None:
            entered.set()
            gate.wait(timeout=30)
        fault = self._take_fault(method, ("timeout_after", "server_error_after"))
        if fault is None:
            return None
        if fault.kind == "timeout_after":
            raise NetworkError("Network error: read timed out (after)")
        raise ServerError("Server error: 503", 503)

    def _hidden_query(self, method: str) -> bool:
        return self._take_fault(method, ("not_found",)) is not None

    # --- balances ----------------------------------------------------------

    def _move(self, asset: str, amount: Decimal) -> None:
        self.free[asset] = self.free.get(asset, Decimal(0)) + amount

    def _lock_funds(self, asset: str, amount: Decimal) -> None:
        if self.free.get(asset, Decimal(0)) < amount:
            raise BadRequestError(
                "Account has insufficient balance for requested action.",
                NEW_ORDER_REJECTED,
            )
        self.free[asset] -= amount
        self.locked[asset] = self.locked.get(asset, Decimal(0)) + amount

    def _unlock(self, asset: str, amount: Decimal) -> None:
        self.locked[asset] = self.locked.get(asset, Decimal(0)) - amount
        self._move(asset, amount)

    # --- matching ----------------------------------------------------------

    def _fill(
        self,
        order: Order,
        qty: Decimal,
        price: Decimal,
        *,
        maker: bool,
        from_locked: bool,
    ) -> dict[str, Any]:
        rules = self.rules[order.symbol]
        quote = qty * price
        if self.commission_asset == "BNB":
            commission = (quote * self.commission_rate / self.bnb_price).quantize(EIGHT)
            commission_asset = "BNB"
        elif self.commission_asset == "base":
            # "In the asset received", as Binance charges without BNB: the base
            # asset on a BUY, the quote asset on a SELL.
            if order.side == "BUY":
                commission = (qty * self.commission_rate).quantize(EIGHT)
                commission_asset = rules.base
            else:
                commission = (quote * self.commission_rate).quantize(EIGHT)
                commission_asset = rules.quote
        else:
            commission = (quote * self.commission_rate).quantize(EIGHT)
            commission_asset = rules.quote
        if order.side == "BUY":
            self._move(rules.quote, -quote)
            self._move(rules.base, qty)
        else:
            if from_locked:
                self.locked[rules.base] -= qty
            else:
                self._move(rules.base, -qty)
            self._move(rules.quote, quote)
        self._move(commission_asset, -commission)
        order.executed += qty
        order.quote += quote
        order.update_time = self.now()
        order.status = (
            "FILLED" if order.executed >= order.quantity else "PARTIALLY_FILLED"
        )
        trade = {
            "symbol": order.symbol,
            "id": next(self._trade_ids),
            "orderId": order.order_id,
            "orderListId": order.order_list_id,
            "price": fmt(price),
            "qty": fmt(qty),
            "quoteQty": fmt(quote),
            "commission": fmt(commission),
            "commissionAsset": commission_asset,
            "time": self.now(),
            "isBuyer": order.side == "BUY",
            "isMaker": maker,
            "isBestMatch": True,
        }
        self.trades.append(trade)
        return trade

    def _order(self, **values: Any) -> Order:
        order = Order(order_id=next(self._ids), time=self.now(), **values)
        self.orders[order.order_id] = order
        return order

    def _open_client_ids(self) -> set[str]:
        return {o.client_order_id for o in self.orders.values() if o.open}

    def set_price(self, symbol: str, price: Decimal) -> None:
        """Move the last trade price and run triggers, like the market would."""
        with self._lock:
            self.last[symbol] = price
            self.last_time[symbol] = self.now()
            self.book[symbol] = (price, price)
            for order_list in list(self.lists.values()):
                if order_list.symbol == symbol and order_list.status == "EXECUTING":
                    self._run_list(order_list, price)

    def _run_list(self, order_list: OrderList, price: Decimal) -> None:
        above, below = order_list.orders
        if above.open and above.price is not None and price >= above.price:
            self._fill(
                above,
                above.quantity - above.executed,
                above.price,
                maker=True,
                from_locked=True,
            )
        elif below.open and below.stop_price is not None and price <= below.stop_price:
            below.triggered = True
            self._settle_list(order_list)  # the trigger cancels the sibling now
        if below.open and below.triggered:
            if below.type == "STOP_LOSS":
                self._fill(
                    below,
                    below.quantity - below.executed,
                    price,
                    maker=False,
                    from_locked=True,
                )
            elif below.price is not None and price >= below.price:
                self._fill(
                    below,
                    below.quantity - below.executed,
                    price,
                    maker=False,
                    from_locked=True,
                )
        self._settle_list(order_list)

    def _finish_list(self, order_list: OrderList, status: str) -> None:
        """Close the list: open legs get ``status``, the one shared lock is freed.

        Both legs of an OCO lock the same base quantity once; whatever the legs
        did not execute between them goes back to free.
        """
        rules = self.rules[order_list.symbol]
        remaining = order_list.orders[0].quantity - sum(
            o.executed for o in order_list.orders
        )
        if remaining > 0:
            self._unlock(rules.base, remaining)
        for order in order_list.orders:
            if order.open:
                order.status = status
                order.update_time = self.now()
        order_list.status = "ALL_DONE"
        order_list.status_type = "ALL_DONE"

    def _settle_list(self, order_list: OrderList) -> None:
        """Binance OCO: once a leg is triggered or executes at all, even
        partly, its sibling is cancelled (EXPIRED). The list ends when no leg
        is open any more.

        A stop-limit leg that triggered but did not fill (a gap past its
        limit) rests as a limit order; the take-profit is gone by then, so a
        rebound can fill only the one leg. The two legs share one lock of the
        list quantity, so the sibling can never sell what the other sold.
        """
        active = [o for o in order_list.orders if o.executed > 0 or o.triggered]
        if not active:
            return
        for order in order_list.orders:
            if order.open and order not in active:
                order.status = "EXPIRED"
                order.update_time = self.now()
        if not any(o.open for o in order_list.orders):
            self._finish_list(order_list, "EXPIRED")

    def fill_leg(self, client_order_id: str, qty: Decimal, price: Decimal) -> None:
        """Script a (partial) fill of one resting order."""
        with self._lock:
            order = self._by_client_id(client_order_id)
            if order is None or not order.open:
                raise AssertionError(f"no open order {client_order_id}")
            self._fill(
                order, qty, price, maker=order.type == "LIMIT_MAKER", from_locked=True
            )
            if order.order_list_id >= 0:
                self._settle_list(self.lists[order.order_list_id])

    def _by_client_id(self, client_order_id: str | None) -> Order | None:
        matches = [
            o for o in self.orders.values() if o.client_order_id == client_order_id
        ]
        return matches[-1] if matches else None

    def _list_by_client_id(self, list_client_order_id: str | None) -> OrderList | None:
        matches = [
            ol
            for ol in self.lists.values()
            if ol.list_client_order_id == list_client_order_id
        ]
        return matches[-1] if matches else None

    # --- SDK surface: trading ----------------------------------------------

    def new_order(self, **kwargs: Any) -> ApiResponse[NewOrderResponse]:
        with self._lock:
            self._before("new_order", kwargs)
            symbol = kwargs["symbol"]
            rules = self.rules[symbol]
            self._trading(rules)
            side = enum_value(kwargs["side"])
            kind = enum_value(kwargs["type"])
            qty = dec(kwargs.get("quantity"))
            client_id = kwargs.get("new_client_order_id") or f"auto-{next(self._ids)}"
            if client_id in self._open_client_ids():
                raise BadRequestError("Duplicate order sent.", NEW_ORDER_REJECTED)
            price = dec(kwargs["price"]) if kwargs.get("price") is not None else None
            tif = enum_value(kwargs.get("time_in_force"))
            order = self._order(
                symbol=symbol,
                client_order_id=client_id,
                side=side,
                type=kind,
                quantity=qty,
                price=price,
                stop_price=None,
                time_in_force=tif,
            )
            fills: list[dict[str, Any]] = []
            last = self.last[symbol]
            if side == "BUY" and kind == "LIMIT" and tif == "FOK":
                if self.fok_outcome == "fill" and price is not None and last <= price:
                    if self.free.get(rules.quote, Decimal(0)) < qty * last:
                        del self.orders[order.order_id]
                        raise BadRequestError(
                            "Account has insufficient balance for requested action.",
                            NEW_ORDER_REJECTED,
                        )
                    fills.append(
                        self._fill(order, qty, last, maker=False, from_locked=False)
                    )
                else:
                    order.status = "EXPIRED"
            elif side == "SELL" and kind == "MARKET":
                if self.free.get(rules.base, Decimal(0)) < qty:
                    del self.orders[order.order_id]
                    raise BadRequestError(
                        "Account has insufficient balance for requested action.",
                        NEW_ORDER_REJECTED,
                    )
                filled = (qty * self.market_fill_ratio).quantize(
                    rules.step, rounding=ROUND_DOWN
                )
                if filled > 0:
                    fills.append(
                        self._fill(order, filled, last, maker=False, from_locked=False)
                    )
                if order.executed < qty:
                    order.status = "EXPIRED"
            else:
                raise AssertionError(
                    f"fake exchange does not model {side} {kind} {tif}"
                )
        no_fills = self._take_fault("new_order", ("no_fills",)) is not None
        self._after("new_order")
        payload = order.document() | {"transactTime": order.time}
        if not no_fills:
            payload["fills"] = [
                {
                    "price": t["price"],
                    "qty": t["qty"],
                    "commission": t["commission"],
                    "commissionAsset": t["commissionAsset"],
                    "tradeId": t["id"],
                }
                for t in fills
            ]
        return response(NewOrderResponse, payload)

    def order_test(self, **kwargs: Any) -> ApiResponse[Any]:
        with self._lock:
            self._before("order_test", kwargs)
        return response(None, {})

    def order_list_oco(self, **kwargs: Any) -> ApiResponse[OrderListOcoResponse]:
        with self._lock:
            self._before("order_list_oco", kwargs)
            symbol = kwargs["symbol"]
            rules = self.rules[symbol]
            self._trading(rules)
            qty = dec(kwargs["quantity"])
            tp = dec(kwargs["above_price"])
            stop = dec(kwargs["below_stop_price"])
            below_type = enum_value(kwargs["below_type"])
            limit = kwargs.get("below_price")
            last = self.last[symbol]
            if tp <= last:
                raise BadRequestError(
                    "Order would immediately match and take.", NEW_ORDER_REJECTED
                )
            if stop >= last:
                raise BadRequestError(
                    "Order would trigger immediately.", NEW_ORDER_REJECTED
                )
            ids = [
                kwargs.get("above_client_order_id"),
                kwargs.get("below_client_order_id"),
            ]
            if set(ids) & self._open_client_ids():
                raise BadRequestError("Duplicate order sent.", NEW_ORDER_REJECTED)
            self._lock_funds(rules.base, qty)
            list_id = next(self._list_ids)
            above = self._order(
                symbol=symbol,
                client_order_id=ids[0],
                side="SELL",
                type="LIMIT_MAKER",
                quantity=qty,
                price=tp,
                stop_price=None,
                time_in_force="GTC",
                order_list_id=list_id,
            )
            below = self._order(
                symbol=symbol,
                client_order_id=ids[1],
                side="SELL",
                type=below_type,
                quantity=qty,
                price=dec(limit) if limit is not None else None,
                stop_price=stop,
                time_in_force="GTC" if limit is not None else None,
                order_list_id=list_id,
            )
            order_list = OrderList(
                list_id,
                symbol,
                kwargs["list_client_order_id"],
                [above, below],
                self.now(),
            )
            self.lists[list_id] = order_list
        self._after("order_list_oco")
        return response(OrderListOcoResponse, order_list.document(reports=True))

    def delete_order_list(self, **kwargs: Any) -> ApiResponse[DeleteOrderListResponse]:
        with self._lock:
            self._before("delete_order_list", kwargs)
            order_list = self._list_by_client_id(kwargs.get("list_client_order_id"))
            if order_list is None or order_list.status != "EXECUTING":
                raise BadRequestError("Unknown order sent.", CANCEL_REJECTED)
            self._finish_list(order_list, "CANCELED")
        self._after("delete_order_list")
        return response(DeleteOrderListResponse, order_list.document(reports=True))

    def delete_order(self, **kwargs: Any) -> ApiResponse[DeleteOrderResponse]:
        with self._lock:
            self._before("delete_order", kwargs)
            order = self._by_client_id(kwargs.get("orig_client_order_id"))
            if order is None or not order.open:
                raise BadRequestError("Unknown order sent.", CANCEL_REJECTED)
            order.status = "CANCELED"
        self._after("delete_order")
        return response(DeleteOrderResponse, order.document())

    # --- SDK surface: queries ----------------------------------------------

    def get_order(self, **kwargs: Any) -> ApiResponse[GetOrderResponse]:
        with self._lock:
            self._before("get_order", kwargs)
            if kwargs.get("order_id") is not None:
                order = self.orders.get(kwargs["order_id"])
            else:
                order = self._by_client_id(kwargs.get("orig_client_order_id"))
            if order is None or self._hidden_query("get_order"):
                raise BadRequestError("Order does not exist.", UNKNOWN_ORDER)
            payload = order.document()
        return response(GetOrderResponse, payload)

    def get_order_list(self, **kwargs: Any) -> ApiResponse[GetOrderListResponse]:
        with self._lock:
            self._before("get_order_list", kwargs)
            order_list = self._list_by_client_id(kwargs.get("orig_client_order_id"))
            if order_list is None or self._hidden_query("get_order_list"):
                raise BadRequestError("Order list does not exist.", UNKNOWN_ORDER)
            payload = order_list.document()
        return response(GetOrderListResponse, payload)

    def open_order_list(
        self, **kwargs: Any
    ) -> ApiResponse[list[OpenOrderListResponse]]:
        with self._lock:
            self._before("open_order_list", kwargs)
            payload = [
                ol.document() for ol in self.lists.values() if ol.status == "EXECUTING"
            ]
        return response(OpenOrderListResponse, payload)

    def get_open_orders(
        self, **kwargs: Any
    ) -> ApiResponse[list[GetOpenOrdersResponse]]:
        with self._lock:
            self._before("get_open_orders", kwargs)
            symbol = kwargs.get("symbol")
            payload = [
                o.document()
                for o in self.orders.values()
                if o.open and (symbol is None or o.symbol == symbol)
            ]
        return response(GetOpenOrdersResponse, payload)

    def my_trades(self, **kwargs: Any) -> ApiResponse[list[MyTradesResponse]]:
        with self._lock:
            self._before("my_trades", kwargs)
            symbol = kwargs["symbol"]
            from_id = kwargs.get("from_id")
            start_time = kwargs.get("start_time")
            order_id = kwargs.get("order_id")
            limit = kwargs.get("limit") or 500
            rows = [
                t
                for t in self.trades
                if t["symbol"] == symbol
                and t["orderId"] not in self.hidden_orders
                and (from_id is None or t["id"] >= from_id)
                and (start_time is None or t["time"] >= start_time)
                and (order_id is None or t["orderId"] == order_id)
            ]
            # Like Binance: from fromId or startTime forward, else the most
            # recent trades.
            forward = from_id is not None or start_time is not None
            payload = rows[:limit] if forward else rows[-limit:]
        return response(MyTradesResponse, payload)

    def get_account(self, **kwargs: Any) -> ApiResponse[GetAccountResponse]:
        with self._lock:
            self._before("get_account", kwargs)
            assets = sorted(set(self.free) | set(self.locked))
            payload = {
                "uid": self.uid,
                "canTrade": self.can_trade,
                "canWithdraw": False,
                "canDeposit": True,
                "accountType": "SPOT",
                "permissions": list(self.permissions),
                "balances": [
                    {
                        "asset": asset,
                        "free": fmt(self.free.get(asset, Decimal(0))),
                        "locked": fmt(self.locked.get(asset, Decimal(0))),
                    }
                    for asset in assets
                ],
            }
        return response(GetAccountResponse, payload)

    def time(self, **kwargs: Any) -> ApiResponse[TimeResponse]:
        self._before("time", kwargs)
        return response(TimeResponse, {"serverTime": self.now()})

    # --- SDK surface: market data ------------------------------------------

    def exchange_info(self, **kwargs: Any) -> ApiResponse[ExchangeInfoResponse]:
        with self._lock:
            self._before("exchange_info", kwargs)
            symbol = kwargs.get("symbol")
            payload = {
                "timezone": "UTC",
                "serverTime": self.now(),
                "rateLimits": [],
                "exchangeFilters": [],
                "symbols": [
                    rules.document()
                    for rules in self.rules.values()
                    if symbol is None or rules.symbol == symbol
                ],
            }
        return response(ExchangeInfoResponse, payload)

    def get_trades(self, **kwargs: Any) -> ApiResponse[list[GetTradesResponse]]:
        with self._lock:
            self._before("get_trades", kwargs)
            symbol = kwargs["symbol"]
            price = self.last[symbol]
            payload = [
                {
                    "id": 1,
                    "price": fmt(price),
                    "qty": "1.00000000",
                    "quoteQty": fmt(price),
                    "time": self.last_time[symbol],
                    "isBuyerMaker": False,
                    "isBestMatch": True,
                }
            ]
        return response(GetTradesResponse, payload)

    def avg_price(self, **kwargs: Any) -> ApiResponse[AvgPriceResponse]:
        with self._lock:
            self._before("avg_price", kwargs)
            symbol = kwargs["symbol"]
            price, mins = self.avg[symbol]
            payload = {
                "mins": mins,
                "price": fmt(price),
                "closeTime": self.last_time[symbol],
            }
        return response(AvgPriceResponse, payload)

    def reference_price(self, **kwargs: Any) -> ApiResponse[ReferencePriceResponse]:
        with self._lock:
            self._before("reference_price", kwargs)
            symbol = kwargs["symbol"]
            value = self.reference.get(symbol)
            payload = {
                "symbol": symbol,
                "referencePrice": fmt(value) if value is not None else None,
                "timestamp": self.now(),
            }
        return response(ReferencePriceResponse, payload)

    def ticker_book_ticker(
        self, **kwargs: Any
    ) -> ApiResponse[TickerBookTickerResponse]:
        with self._lock:
            self._before("ticker_book_ticker", kwargs)
            symbol = kwargs["symbol"]
            bid, ask = self.book[symbol]
            payload = {
                "symbol": symbol,
                "bidPrice": fmt(bid),
                "bidQty": "1.00000000",
                "askPrice": fmt(ask),
                "askQty": "1.00000000",
            }
        return response(TickerBookTickerResponse, payload)

    def ticker_price(self, **kwargs: Any) -> ApiResponse[TickerPriceResponse]:
        with self._lock:
            self._before("ticker_price", kwargs)
            symbol = kwargs["symbol"]
            if symbol not in self.last:
                raise BadRequestError("Invalid symbol.", -1121)
            payload = {"symbol": symbol, "price": fmt(self.last[symbol])}
        return response(TickerPriceResponse, payload)

    def klines(self, **kwargs: Any) -> ApiResponse[list[list[Any]]]:
        with self._lock:
            self._before("klines", kwargs)
            interval = enum_value(kwargs["interval"]) or ""
            rows = self.klines_data.get((kwargs["symbol"], interval), [])
            start = kwargs.get("start_time")
            end = kwargs.get("end_time")
            rows = [
                r
                for r in rows
                if (start is None or r[0] >= start) and (end is None or r[0] <= end)
            ]
            limit = kwargs.get("limit")
            if limit:
                rows = rows[-limit:] if start is None else rows[:limit]
        return response(None, rows)

    # --- inspection ----------------------------------------------------------

    def balance(self, asset: str) -> tuple[Decimal, Decimal]:
        return self.free.get(asset, Decimal(0)), self.locked.get(asset, Decimal(0))

    def orders_of(self, symbol: str, side: str | None = None) -> list[Order]:
        return [
            o
            for o in self.orders.values()
            if o.symbol == symbol and (side is None or o.side == side)
        ]

    def calls_to(self, method: str) -> int:
        return sum(1 for name, _ in self.calls if name == method)


__all__ = ["FakeExchange", "Fault", "Order", "OrderList", "SymbolRules"]
