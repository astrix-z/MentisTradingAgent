"""EXEC-02: trade client — wire format, classification, no retries, budget.

Requests go through the installed SDK's real serializer and signer; only the
HTTP session underneath is replaced, so what we assert is what would be sent.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs

import pytest
import requests
from binance_common.configuration import ConfigurationRestAPI
from binance_sdk_spot.spot import Spot

from agent.exchange import trade
from agent.exchange.budget import BudgetExhausted, Priority, RequestBudget
from agent.exchange.rest import ExchangeRest
from agent.exchange.trade import (
    RECV_WINDOW_MS,
    Outcome,
    TradeCallError,
    TradeClient,
    wire,
)

API_KEY = "test-api-key-0123456789"
API_SECRET = "test-api-secret-9876543210"


def _response(
    status: int,
    body: Any = None,
    *,
    headers: dict[str, str] | None = None,
    raw: bytes | None = None,
) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.headers.update(headers or {})
    if raw is not None:
        response._content = raw
    else:
        response.headers.setdefault("Content-Type", "application/json")
        response._content = json.dumps(body if body is not None else {}).encode()
    return response


class Transport:
    """Stands in for ``requests.Session.request``; records every call."""

    def __init__(self, *answers: requests.Response | Exception) -> None:
        self.answers = list(answers)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> requests.Response:
        params = parse_qs(kwargs.get("params") or "", keep_blank_values=True)
        self.calls.append(
            {
                "method": kwargs["method"],
                "path": kwargs["url"].split(".com", 1)[-1],
                "params": {k: v[0] for k, v in params.items()},
            }
        )
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    @property
    def last(self) -> dict[str, Any]:
        return self.calls[-1]


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def install(spot: Spot, transport: Transport) -> None:
    """Replace the SDK's HTTP session call; everything above it is real."""
    setattr(spot.rest_api._session, "request", transport)  # noqa: B010


def make_client(
    transport: Transport, budget: RequestBudget | None = None
) -> TradeClient:
    config = ConfigurationRestAPI(
        api_key=API_KEY,
        api_secret=API_SECRET,
        base_path="https://api.binance.com",
        timeout=trade.TRADE_TIMEOUT_MS,
        retries=0,
    )
    spot = Spot(config_rest_api=config)
    install(spot, transport)
    return TradeClient(
        "live",
        spot,
        budget=budget or RequestBudget(),
        secrets=(API_KEY, API_SECRET),
    )


def run(make: Callable[[], Any]) -> Any:
    return asyncio.run(make())


ORDER = {
    "symbol": "BTCUSDT",
    "orderId": 7,
    "clientOrderId": "mta-abc-e1",
    "status": "FILLED",
    "executedQty": "0.00100000",
    "fills": [],
}


# --- wire format ------------------------------------------------------------


def test_wire_is_fixed_point_never_exponent() -> None:
    assert str(Decimal("0.00000001")) == "1E-8"
    assert wire(Decimal("0.00000001")) == "0.00000001"
    assert wire(Decimal("1E+2")) == "100"
    assert wire(Decimal("12.3400")) == "12.3400"
    for bad in (Decimal("-1"), Decimal("NaN"), Decimal("Infinity")):
        with pytest.raises(ValueError):
            wire(bad)


def test_limit_fok_entry_serializes_exactly() -> None:
    transport = Transport(_response(200, ORDER))
    client = make_client(transport)
    run(
        lambda: client.new_order(
            symbol="BTCUSDT",
            side="BUY",
            order_type="LIMIT",
            quantity=Decimal("0.00000001"),
            price=Decimal("65000.10"),
            time_in_force="FOK",
            client_order_id="mta-abc-e1",
            priority=Priority.ENTRY,
        )
    )
    call = transport.last
    assert call["method"] == "POST" and call["path"] == "/api/v3/order"
    params = call["params"]
    assert params["quantity"] == "0.00000001"
    assert params["price"] == "65000.10"
    assert params["timeInForce"] == "FOK"
    assert params["type"] == "LIMIT" and params["side"] == "BUY"
    assert params["newClientOrderId"] == "mta-abc-e1"
    assert params["newOrderRespType"] == "FULL"
    assert params["recvWindow"] == str(RECV_WINDOW_MS)
    assert params["signature"]


def test_oco_with_market_stop_has_no_limit_price_on_the_stop_leg() -> None:
    transport = Transport(_response(200, {"orderListId": 1}))
    client = make_client(transport)
    run(
        lambda: client.order_list_oco(
            symbol="BTCUSDT",
            quantity=Decimal("0.00100000"),
            take_profit=Decimal("71500.00"),
            stop=Decimal("64350.00"),
            stop_limit_price=None,
            list_client_order_id="mta-abc-pl1",
            above_client_order_id="mta-abc-pt1",
            below_client_order_id="mta-abc-ps1",
        )
    )
    call = transport.last
    assert call["path"] == "/api/v3/orderList/oco"
    params = call["params"]
    assert params["side"] == "SELL"
    assert params["quantity"] == "0.00100000"
    assert params["aboveType"] == "LIMIT_MAKER"
    assert params["abovePrice"] == "71500.00"
    assert params["belowType"] == "STOP_LOSS"
    assert params["belowStopPrice"] == "64350.00"
    assert "belowPrice" not in params
    assert "belowTimeInForce" not in params
    assert params["listClientOrderId"] == "mta-abc-pl1"
    assert params["aboveClientOrderId"] == "mta-abc-pt1"
    assert params["belowClientOrderId"] == "mta-abc-ps1"
    assert params["recvWindow"] == str(RECV_WINDOW_MS)


def test_oco_fallback_stop_limit_leg_is_gtc_with_a_price() -> None:
    transport = Transport(_response(200, {"orderListId": 1}))
    client = make_client(transport)
    run(
        lambda: client.order_list_oco(
            symbol="BTCUSDT",
            quantity=Decimal("0.001"),
            take_profit=Decimal("71500"),
            stop=Decimal("64350"),
            stop_limit_price=Decimal("64000.5"),
            list_client_order_id="mta-abc-pl2",
            above_client_order_id="mta-abc-pt2",
            below_client_order_id="mta-abc-ps2",
        )
    )
    params = transport.last["params"]
    assert params["belowType"] == "STOP_LOSS_LIMIT"
    assert params["belowPrice"] == "64000.5"
    assert params["belowTimeInForce"] == "GTC"


def test_every_signed_call_carries_recv_window_and_time_does_not() -> None:
    transport = Transport(_response(200, {}))
    client = make_client(transport)

    async def calls() -> None:
        await client.get_order(symbol="BTCUSDT", client_order_id="mta-abc-e1")
        await client.get_order_list(list_client_order_id="mta-abc-pl1")
        await client.delete_order_list(symbol="BTCUSDT", list_client_order_id="x")
        await client.delete_order(symbol="BTCUSDT", client_order_id="x")
        await client.get_account()
        transport.answers = [_response(200, [])]
        await client.my_trades(symbol="BTCUSDT", from_id=5)
        await client.open_order_list()

    run(calls)
    assert len(transport.calls) == 7
    for call in transport.calls:
        assert call["params"]["recvWindow"] == str(RECV_WINDOW_MS), call["path"]
        assert call["params"]["signature"]

    transport = Transport(_response(200, {"serverTime": 1_700_000_000_000}))
    client = make_client(transport)
    run(client.clock_offset)
    assert "recvWindow" not in transport.last["params"]


def test_client_id_longer_than_36_is_refused_before_sending() -> None:
    transport = Transport(_response(200, ORDER))
    client = make_client(transport)
    with pytest.raises(ValueError):
        run(
            lambda: client.new_order(
                symbol="BTCUSDT",
                side="BUY",
                order_type="LIMIT",
                quantity=Decimal(1),
                price=Decimal(1),
                time_in_force="FOK",
                client_order_id="m" * 37,
                priority=Priority.ENTRY,
            )
        )
    assert transport.calls == []


# --- classification ---------------------------------------------------------


def _place(client: TradeClient) -> Callable[[], Any]:
    return lambda: client.new_order(
        symbol="BTCUSDT",
        side="BUY",
        order_type="LIMIT",
        quantity=Decimal(1),
        price=Decimal(1),
        time_in_force="FOK",
        client_order_id="mta-abc-e1",
        priority=Priority.ENTRY,
    )


def _error(answer: requests.Response | Exception) -> TradeCallError:
    client = make_client(Transport(answer))
    with pytest.raises(TradeCallError) as caught:
        run(_place(client))
    return caught.value


@pytest.mark.parametrize(
    ("answer", "outcome", "code", "http"),
    [
        (
            _response(400, {"code": -1007, "msg": "Timeout"}),
            Outcome.UNKNOWN,
            -1007,
            None,
        ),
        (
            _response(400, {"code": -1006, "msg": "Unexpected"}),
            Outcome.UNKNOWN,
            -1006,
            None,
        ),
        (
            _response(400, {"code": -2010, "msg": "Insufficient"}),
            Outcome.REJECTED,
            -2010,
            400,
        ),
        (_response(400, raw=b"<html>bad gateway</html>"), Outcome.UNKNOWN, None, 400),
        (
            _response(401, {"code": -2015, "msg": "Invalid key"}),
            Outcome.REJECTED,
            -2015,
            401,
        ),
        (_response(403, raw=b"waf"), Outcome.REJECTED, None, 403),
        (_response(503, {"code": -1008, "msg": "busy"}), Outcome.UNKNOWN, None, 503),
        (_response(500, raw=b"oops"), Outcome.UNKNOWN, None, 500),
        (
            _response(409, {"code": -2022, "msg": "partial"}),
            Outcome.UNKNOWN,
            None,
            None,
        ),
        (_response(200, raw=b"not json at all"), Outcome.UNKNOWN, None, None),
        (requests.Timeout("read timed out"), Outcome.UNKNOWN, None, None),
        (requests.ConnectionError("reset"), Outcome.UNKNOWN, None, None),
    ],
    ids=[
        "400/-1007",
        "400/-1006",
        "400/-2010",
        "400-no-body",
        "401",
        "403-waf",
        "503",
        "500",
        "409",
        "unparsable-200",
        "timeout",
        "connection",
    ],
)
def test_classification(
    answer: requests.Response | Exception,
    outcome: Outcome,
    code: int | None,
    http: int | None,
) -> None:
    error = _error(answer)
    assert error.outcome is outcome
    assert error.exchange_code == code
    assert error.http_status == http


def test_429_pauses_the_budget_and_is_not_retried() -> None:
    clock = FakeClock()
    budget = RequestBudget(clock=clock, sleep=clock.sleep)
    transport = Transport(
        _response(429, {"code": -1003, "msg": "Too many"}, headers={"Retry-After": "7"})
    )
    client = make_client(transport, budget)
    with pytest.raises(TradeCallError) as caught:
        run(_place(client))
    assert caught.value.outcome is Outcome.RATE_LIMITED
    assert caught.value.retry_after == 7
    assert len(transport.calls) == 1
    assert budget.paused_until == pytest.approx(clock.now + 7)


def test_418_bans_and_later_calls_fail_fast_without_sending() -> None:
    clock = FakeClock()
    budget = RequestBudget(clock=clock, sleep=clock.sleep)
    transport = Transport(
        _response(418, {"code": -1003, "msg": "banned"}, headers={"Retry-After": "120"})
    )
    client = make_client(transport, budget)
    with pytest.raises(TradeCallError) as caught:
        run(_place(client))
    assert caught.value.outcome is Outcome.BANNED
    assert budget.banned()
    with pytest.raises(TradeCallError) as again:
        run(lambda: client.get_order(symbol="BTCUSDT", client_order_id="x"))
    assert again.value.outcome is Outcome.BANNED
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    ("call", "method"),
    [
        (
            lambda c: (
                lambda: c.delete_order_list(symbol="BTCUSDT", list_client_order_id="x")
            ),
            "DELETE",
        ),
        (lambda c: lambda: c.get_order(symbol="BTCUSDT", client_order_id="x"), "GET"),
        (_place, "POST"),
    ],
    ids=["delete", "get", "post"],
)
def test_no_request_is_ever_retried(
    call: Callable[[TradeClient], Callable[[], Any]], method: str
) -> None:
    for answer in (_response(503, raw=b"busy"), requests.ConnectionError("reset")):
        transport = Transport(answer)
        client = make_client(transport)
        with pytest.raises(TradeCallError):
            run(call(client))
        assert len(transport.calls) == 1
        assert transport.last["method"] == method


def test_errors_are_redacted() -> None:
    leak = requests.ConnectionError(
        f"failed for https://api.binance.com/api/v3/order?symbol=X&signature=deadbeef "
        f"key={API_KEY}"
    )
    error = _error(leak)
    assert "deadbeef" not in str(error) and API_KEY not in str(error)
    assert "signature=***" in error.message
    assert json.dumps(error.as_record())


def test_my_trades_pages_forward_with_inclusive_from_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(trade, "MY_TRADES_PAGE", 2)

    def page(*ids: int) -> requests.Response:
        return _response(
            200,
            [
                {
                    "symbol": "BTCUSDT",
                    "id": i,
                    "orderId": 1,
                    "price": "1",
                    "qty": "1",
                    "quoteQty": "1",
                    "commission": "0",
                    "commissionAsset": "BNB",
                    "time": 1,
                    "isBuyer": True,
                    "isMaker": False,
                    "isBestMatch": True,
                }
                for i in ids
            ],
        )

    transport = Transport(page(10, 11), page(12, 13), page(14))
    client = make_client(transport)
    trades = run(lambda: client.my_trades_since(symbol="BTCUSDT", from_id=10))
    assert [t.id for t in trades] == [10, 11, 12, 13, 14]
    assert [c["params"]["fromId"] for c in transport.calls] == ["10", "12", "14"]


# --- budget -----------------------------------------------------------------


def test_market_load_never_takes_protection_headroom() -> None:
    clock = FakeClock()
    budget = RequestBudget(limit=100, clock=clock, sleep=clock.sleep)

    async def scenario() -> None:
        for _ in range(60):
            await budget.acquire(Priority.MARKET, 1, max_wait=0)
        with pytest.raises(BudgetExhausted):
            await budget.acquire(Priority.MARKET, 1, max_wait=0)
        for _ in range(15):
            await budget.acquire(Priority.ENTRY, 1, max_wait=0)
        with pytest.raises(BudgetExhausted):
            await budget.acquire(Priority.ENTRY, 1, max_wait=0)
        for _ in range(25):
            await budget.acquire(Priority.PROTECT, 1, max_wait=0)
        with pytest.raises(BudgetExhausted):
            await budget.acquire(Priority.PROTECT, 1, max_wait=0)
        clock.now += 61  # the minute rolls over
        await budget.acquire(Priority.MARKET, 1, max_wait=0)

    asyncio.run(scenario())


def test_pause_after_429_holds_everyone_until_retry_after() -> None:
    clock = FakeClock()
    budget = RequestBudget(clock=clock, sleep=clock.sleep)
    budget.on_rate_limited(5)

    async def scenario() -> None:
        with pytest.raises(BudgetExhausted):
            await budget.acquire(Priority.PROTECT, 1, max_wait=1)
        start = clock.now
        await budget.acquire(Priority.PROTECT, 1)
        assert clock.now >= start + 3

    asyncio.run(scenario())


def test_exchange_count_from_headers_corrects_the_estimate() -> None:
    transport = Transport(
        _response(200, {"serverTime": 1}, headers={"X-MBX-USED-WEIGHT-1M": "4321"})
    )
    budget = RequestBudget()
    client = make_client(transport, budget)
    run(client.clock_offset)
    assert budget.used() >= 4321


def test_market_data_reads_feed_the_shared_budget() -> None:
    budget = RequestBudget()
    config = ConfigurationRestAPI(base_path="https://api.binance.com")
    spot = Spot(config_rest_api=config)
    install(
        spot,
        Transport(
            _response(200, {"symbols": []}, headers={"X-MBX-USED-WEIGHT-1M": "777"})
        ),
    )
    rest = ExchangeRest("live", spot, budget)
    asyncio.run(rest.symbols())
    assert budget.used() >= 777
