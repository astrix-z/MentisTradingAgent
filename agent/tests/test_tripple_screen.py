"""Unit tests for the Triple Screen strategy core (no network access)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from binance_sdk_spot.websocket_streams.models import KlineResponse, KlineResponseK

from agent.strategies.tripple_screen.indicators import (
    Candle,
    ema_series,
    force_index_series,
    sma_series,
    stochastic_k_series,
)
from agent.strategies.tripple_screen.strategy import TripleScreenStrategy


def make_candle(
    index: int,
    *,
    open_: str,
    high: str,
    low: str,
    close: str,
    volume: str = "1",
    interval_ms: int = 900_000,
) -> Candle:
    open_time = index * interval_ms
    return Candle(
        open_time=open_time,
        close_time=open_time + interval_ms - 1,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal(volume),
        closed=True,
    )


def kline_message(candle: Candle) -> KlineResponse:
    return KlineResponse(
        k=KlineResponseK(
            t=candle.open_time,
            T=candle.close_time,
            o=str(candle.open),
            h=str(candle.high),
            l=str(candle.low),
            c=str(candle.close),
            v=str(candle.volume),
            x=True,
        )
    )


def test_sma_and_ema_series_align_and_seed() -> None:
    values = [Decimal(v) for v in ("1", "2", "3", "4", "5")]
    assert sma_series(values, 3) == [None, None, Decimal(2), Decimal(3), Decimal(4)]

    ema = ema_series(values, 3)
    assert ema[0] is None and ema[1] is None
    assert ema[2] == Decimal(2)  # SMA seed
    assert ema[3] == Decimal(3)  # alpha=0.5: 0.5*4 + 0.5*2


def test_stochastic_and_force_index() -> None:
    candles = [
        make_candle(i, open_="9", high="10", low="9", close="9.5") for i in range(4)
    ]
    candles.append(make_candle(4, open_="9.5", high="10", low="9", close="10"))
    k = stochastic_k_series(candles, 5)
    # close 10 at the top of the 9..10 range -> %K == 100
    assert k[-1] == Decimal(100)

    fi = force_index_series(candles, 2)
    assert fi[-1] is not None


def test_bullish_tide_requires_enough_history() -> None:
    strategy = TripleScreenStrategy({"ema_tide_periods": [5, 13, 34]})
    assert strategy._bullish_tide("BTCUSDT") is None

    closes = [str(100 + i) for i in range(40)]
    strategy._candles["BTCUSDT"] = {
        "4h": [
            make_candle(i, open_=c, high=c, low=c, close=c)
            for i, c in enumerate(closes)
        ]
    }
    assert strategy._bullish_tide("BTCUSDT") is True


def test_entry_signal_then_epoch_is_consumed() -> None:
    strategy = TripleScreenStrategy({})
    strategy._bullish_tide = lambda symbol: True  # type: ignore[method-assign]
    strategy._pullback_up = lambda symbol, *, before_time: True  # type: ignore[method-assign]

    ripple = [
        make_candle(0, open_="10", high="11", low="10", close="10.5"),
        make_candle(1, open_="10.5", high="11.5", low="10.5", close="11.2"),
    ]
    strategy._candles["BTCUSDT"] = {"15m": ripple, "1h": ripple}

    signal = strategy._decide("BTCUSDT")
    assert signal is not None
    assert signal.type == "ENTRY"
    assert signal.epoch_id == f"BTCUSDT:{ripple[-1].open_time}"
    assert signal.entry_ref == pytest.approx(11.2)

    # A second evaluation on the same bar must not re-emit an entry.
    again = strategy._decide("BTCUSDT")
    assert again is not None
    assert again.type == "HOLD"
    assert again.reason == "epoch_consumed"


def test_position_open_holds() -> None:
    strategy = TripleScreenStrategy({})
    strategy._bullish_tide = lambda symbol: True  # type: ignore[method-assign]
    strategy._pullback_up = lambda symbol, *, before_time: True  # type: ignore[method-assign]
    ripple = [
        make_candle(0, open_="10", high="11", low="10", close="10.5"),
        make_candle(1, open_="10.5", high="11.5", low="10.5", close="11.2"),
    ]
    strategy._candles["BTCUSDT"] = {"15m": ripple, "1h": ripple}
    strategy.mark_position_open("BTCUSDT")

    signal = strategy._decide("BTCUSDT")
    assert signal is not None and signal.reason == "position_open"


def test_on_kline_updates_windows() -> None:
    import asyncio

    strategy = TripleScreenStrategy({})
    strategy._bullish_tide = lambda symbol: None  # type: ignore[method-assign]
    candle = make_candle(0, open_="1", high="2", low="1", close="1.5")
    asyncio.run(strategy._on_kline("BTCUSDT", "15m", kline_message(candle)))
    assert strategy._candles["BTCUSDT"]["15m"] == [candle]
    assert strategy._last_price["BTCUSDT"] == Decimal("1.5")
    _ = datetime.now(UTC)
