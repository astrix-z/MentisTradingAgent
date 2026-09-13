"""Pure indicator math for the Triple Screen strategy (spec §3).

Everything here is side-effect free and works on :class:`Candle` windows built
from closed bars. Prices are :class:`~decimal.Decimal` per the spec.

Note (``STRATEGY-05``): the EMA seed convention and the decimal/rounding policy
are still provisionally "SMA seed, then standard EMA"; they will be frozen with
hand-calculated fixtures.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from binance_sdk_spot.websocket_streams.models import KlineResponseK

_HUNDRED = Decimal(100)


@dataclass(frozen=True, slots=True)
class Candle:
    """One closed (or forming) candle, parsed from an SDK kline payload.

    This is the strategy's internal bar value — not a re-implementation of a
    Binance model; it is only the subset of fields the indicators need, already
    decoded to ``Decimal``.
    """

    open_time: int
    close_time: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    closed: bool

    @classmethod
    def from_rest_row(cls, row: list[Any]) -> Candle:
        """Build from a raw REST ``klines`` row.

        Row layout: ``open_time, open, high, low, close, volume, close_time, ...``.
        """
        return cls(
            open_time=int(row[0]),
            open=Decimal(str(row[1])),
            high=Decimal(str(row[2])),
            low=Decimal(str(row[3])),
            close=Decimal(str(row[4])),
            volume=Decimal(str(row[5])),
            close_time=int(row[6]),
            closed=True,
        )

    @classmethod
    def from_ws(cls, k: KlineResponseK) -> Candle:
        """Build from an SDK ``KlineResponseK`` websocket payload."""
        return cls(
            open_time=int(k.t or 0),
            close_time=int(k.T or 0),
            open=Decimal(str(k.o or 0)),
            high=Decimal(str(k.h or 0)),
            low=Decimal(str(k.l or 0)),
            close=Decimal(str(k.c or 0)),
            volume=Decimal(str(k.v or 0)),
            closed=bool(k.x),
        )


def sma_series(values: list[Decimal], period: int) -> list[Decimal | None]:
    """Simple moving average aligned to ``values`` (``None`` until defined)."""
    result: list[Decimal | None] = [None] * len(values)
    if period <= 0:
        return result
    running = Decimal(0)
    for i, value in enumerate(values):
        running += value
        if i >= period:
            running -= values[i - period]
        if i >= period - 1:
            result[i] = running / period
    return result


def sma_of_optional(values: list[Decimal | None], period: int) -> list[Decimal | None]:
    """SMA over a series that may contain ``None`` (a bar is skipped if any
    value in its window is missing)."""
    result: list[Decimal | None] = [None] * len(values)
    if period <= 0:
        return result
    for i in range(period - 1, len(values)):
        window = values[i - period + 1 : i + 1]
        total = Decimal(0)
        defined = True
        for value in window:
            if value is None:
                defined = False
                break
            total += value
        if not defined:
            continue
        result[i] = total / period
    return result


def ema_series(values: list[Decimal], period: int) -> list[Decimal | None]:
    """EMA aligned to ``values``, seeded with the SMA of the first ``period``."""
    result: list[Decimal | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return result
    alpha = Decimal(2) / (Decimal(period) + 1)
    seed = Decimal(0)
    for value in values[:period]:
        seed += value
    seed /= period
    result[period - 1] = seed
    previous = seed
    for i in range(period, len(values)):
        previous = alpha * values[i] + (Decimal(1) - alpha) * previous
        result[i] = previous
    return result


def stochastic_k_series(candles: list[Candle], period: int) -> list[Decimal | None]:
    """``%K`` aligned to ``candles``; ``None`` on a flat or too-short window."""
    result: list[Decimal | None] = [None] * len(candles)
    if period <= 0:
        return result
    for i in range(period - 1, len(candles)):
        window = candles[i - period + 1 : i + 1]
        highest = max(c.high for c in window)
        lowest = min(c.low for c in window)
        if highest == lowest:
            continue
        close = candles[i].close
        result[i] = _HUNDRED * (close - lowest) / (highest - lowest)
    return result


def force_index_series(candles: list[Candle], ema_period: int) -> list[Decimal | None]:
    """Elder's Force Index: ``EMA((close - prev_close) * volume, ema_period)``."""
    if not candles:
        return []
    raw: list[Decimal] = []
    for i in range(1, len(candles)):
        delta = candles[i].close - candles[i - 1].close
        raw.append(delta * candles[i].volume)
    return [None, *ema_series(raw, ema_period)]


__all__ = [
    "Candle",
    "ema_series",
    "force_index_series",
    "sma_of_optional",
    "sma_series",
    "stochastic_k_series",
]
