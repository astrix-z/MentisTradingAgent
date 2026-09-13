"""Hand-calculated indicator regressions, without network access."""

from decimal import Decimal

from agent.strategies.tripple_screen.indicators import (
    Candle,
    ema_series,
    force_index_series,
    sma_series,
    stochastic_k_series,
)


def candle(index: int, close: str) -> Candle:
    value = Decimal(close)
    return Candle(
        index * 900_000,
        (index + 1) * 900_000 - 1,
        value,
        Decimal(10),
        Decimal(9),
        value,
        Decimal(1),
        True,
    )


def test_sma_and_ema_series_align_and_seed() -> None:
    values = [Decimal(v) for v in (1, 2, 3, 4, 5)]
    assert sma_series(values, 3) == [None, None, Decimal(2), Decimal(3), Decimal(4)]
    assert ema_series(values, 3) == [None, None, Decimal(2), Decimal(3), Decimal(4)]


def test_stochastic_and_force_index() -> None:
    candles = [candle(i, "9.5") for i in range(4)] + [candle(4, "10")]
    assert stochastic_k_series(candles, 5)[-1] == Decimal(100)
    assert force_index_series(candles, 2)[-1] == Decimal(2) / 3 * Decimal("0.5")


def test_force_index_real_observations_and_alignment() -> None:
    assert force_index_series([], 2) == []
    assert force_index_series([candle(0, "10")], 2) == [None]
    bars = [candle(i, close) for i, close in enumerate(("10", "9", "9.4"))]
    assert force_index_series(bars, 2) == [None, None, Decimal("-0.3")]
