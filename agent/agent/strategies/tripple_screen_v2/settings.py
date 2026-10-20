"""Validated, immutable startup configuration of Triple Screen v2.

Same contract as :mod:`agent.strategies.tripple_screen.settings`: field
``title``s are the UI labels and the model travels to the backend/UI through
the published catalog. The stochastic and force-index parameters of v1 are
gone (the wave screen is an EMA alignment); the ripple screen adds the EMA
pair and the percentile read from the Binance DB ``prcntl_*`` tables.
"""

from __future__ import annotations

from decimal import Decimal
from itertools import pairwise
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent.common.intervals import interval_ms
from agent.strategies.tripple_screen.settings import (
    TIMEFRAME_MESSAGE,
    TRAIL_CAP_MESSAGE,
)

PositiveInt = Annotated[int, Field(gt=0)]

#: EMA periods that have a ``ma_<N>_delta`` column in the ``prcntl_*`` tables.
PercentilePeriod = Literal[5, 8, 13, 21, 34, 55, 89, 144]

#: Percentile rows (``index`` = ``"<N>%"``) the ripple screen can compare to.
Percentile = Literal["50", "55", "60", "65", "70", "75", "80", "85", "90", "95"]

TIDE_EMA_MESSAGE = "EMA tide periods must be three strictly increasing positive periods"
WAVE_EMA_MESSAGE = "EMA wave periods must be three strictly increasing positive periods"
RIPPLE_EMA_MESSAGE = "EMA ripple periods must be two strictly increasing periods"


class TripleScreenV2Settings(BaseModel):
    model_config = ConfigDict(frozen=True, allow_inf_nan=False)

    quote_asset: Literal["USDT", "USDC"] = Field(default="USDT", title="Quote asset")
    tide_interval: str = Field(
        default="4h",
        title="Tide interval",
        json_schema_extra={"ui": {"widget": "interval"}},
    )
    wave_interval: str = Field(
        default="1h",
        title="Wave interval",
        json_schema_extra={"ui": {"widget": "interval"}},
    )
    ripple_interval: str = Field(
        default="15m",
        title="Ripple interval",
        description=(
            "Also names the percentile table: prcntl_<symbol>_<ripple interval>."
        ),
        json_schema_extra={"ui": {"widget": "interval"}},
    )
    ema_tide_periods: tuple[PositiveInt, PositiveInt, PositiveInt] = Field(
        default=(5, 13, 34), title="EMA tide periods"
    )
    ema_wave_periods: tuple[PositiveInt, PositiveInt, PositiveInt] = Field(
        default=(5, 13, 34),
        title="EMA wave periods",
        description="The wave screen passes when EMA fast > EMA mid > EMA slow.",
    )
    ema_ripple_periods: tuple[PercentilePeriod, PercentilePeriod] = Field(
        default=(5, 13),
        title="EMA ripple periods",
        description=(
            "The ripple screen passes when the one-bar % change of each of "
            "these EMAs is above its percentile."
        ),
    )
    ripple_percentile: Percentile = Field(
        default="95",
        title="Ripple percentile",
        description=(
            "Row of the prcntl_* table the EMA % changes must exceed (95 = 95%)."
        ),
    )
    swing_low_wave_n: PositiveInt = Field(default=2, title="Swing low WAVE N")
    trail_ripple_n: PositiveInt = Field(default=2, title="Trail RIPPLE N")
    stop_buffer_pct: Decimal = Field(
        default=Decimal("0.1"), ge=0, lt=100, title="Stop buffer %"
    )
    min_stop_distance_pct: Decimal = Field(
        default=Decimal(1), gt=0, lt=100, title="Min stop distance %"
    )
    tp_pct: Decimal = Field(default=Decimal(10), gt=0, title="Take profit %")
    trail_cap_pct: Decimal = Field(
        default=Decimal(4),
        ge=0,
        title="Trailing stop cap %",
        description=(
            "The trailing stop never rises above fill × (1 + cap/100); must be "
            "below the take-profit %."
        ),
    )
    max_hold_hours: Decimal = Field(
        default=Decimal(8),
        gt=0,
        title="Max hold (hours)",
        description="A position still open this long after its fill is sold.",
    )
    cooldown_minutes: int = Field(default=60, ge=0, title="Cooldown (minutes)")
    signal_validity_minutes: PositiveInt = Field(
        default=15, title="Signal validity (minutes)"
    )
    rest_concurrency: int = Field(default=3, ge=3, le=5, title="REST concurrency")
    ws_top_n: PositiveInt = Field(
        default=30,
        le=100,
        title="Max active symbols",
        description=(
            "Stage-2 cap: of the symbols that pass the EMA13/EMA34 screen and "
            "have a percentile table, at most this many are streamed and fully "
            "processed, ranked by trend strength (EMA13-over-EMA34 spread)."
        ),
    )
    volume_window: Literal["24h"] = Field(default="24h", title="Volume window")
    kline_window: PositiveInt = Field(default=1000, title="Kline window")

    @model_validator(mode="after")
    def coherent(self) -> Self:
        for periods, message in (
            (self.ema_tide_periods, TIDE_EMA_MESSAGE),
            (self.ema_wave_periods, WAVE_EMA_MESSAGE),
            (self.ema_ripple_periods, RIPPLE_EMA_MESSAGE),
        ):
            if any(left >= right for left, right in pairwise(periods)):
                raise ValueError(message)
        tide, wave, ripple = (
            interval_ms(value)
            for value in (self.tide_interval, self.wave_interval, self.ripple_interval)
        )
        if not tide > wave > ripple:
            raise ValueError(TIMEFRAME_MESSAGE)
        if not self.trail_cap_pct < self.tp_pct:
            raise ValueError(TRAIL_CAP_MESSAGE)
        needed = max(
            self.ema_tide_periods[2] + 1,
            self.ema_wave_periods[2],
            self.ema_ripple_periods[1] + 1,
            self.swing_low_wave_n,
            self.trail_ripple_n,
            2,
        )
        if self.kline_window < needed:
            raise ValueError(f"kline_window must be at least {needed}")
        return self

    @classmethod
    def from_params(cls, params: dict[str, object]) -> TripleScreenV2Settings:
        """Ignore shared/unknown command keys, validate all known parameters."""
        return cls.model_validate(
            {k: v for k, v in params.items() if k in cls.model_fields}
        )


__all__ = [
    "RIPPLE_EMA_MESSAGE",
    "TIDE_EMA_MESSAGE",
    "WAVE_EMA_MESSAGE",
    "TripleScreenV2Settings",
]
