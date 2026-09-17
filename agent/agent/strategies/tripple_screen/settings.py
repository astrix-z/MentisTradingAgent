"""Validated, immutable startup configuration (Triple Screen spec §10).

Field ``title``s are the UI labels and ``json_schema_extra`` carries the widget
hints the frontend needs; both travel to the backend/UI through the published
catalog (see :mod:`agent.protocol`), so this module stays the only place that
knows the parameter set — including how it should be presented.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any, Literal, Self

from binance_sdk_spot.rest_api.models import KlinesIntervalEnum
from binance_sdk_spot.websocket_streams.models import KlineIntervalEnum
from pydantic import BaseModel, ConfigDict, Field, model_validator

PositiveInt = Annotated[int, Field(gt=0)]


def interval_ms(interval: str) -> int:
    """Fixed UTC intraday SDK intervals; calendar/week alignment is unsupported."""
    KlinesIntervalEnum(interval)
    KlineIntervalEnum(interval)
    units = {"s": 1000, "m": 60_000, "h": 3_600_000}
    if interval[-1] not in units:
        raise ValueError("only fixed intraday SDK intervals are supported")
    return int(interval[:-1]) * units[interval[-1]]


class TripleScreenSettings(BaseModel):
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
        json_schema_extra={"ui": {"widget": "interval"}},
    )
    ema_tide_periods: tuple[PositiveInt, PositiveInt, PositiveInt] = Field(
        default=(5, 13, 34), title="EMA tide periods"
    )
    stochastic_k_period: PositiveInt = Field(default=5, title="Stochastic %K period")
    stochastic_d_period: PositiveInt = Field(default=3, title="Stochastic %D period")
    stochastic_slow_period: PositiveInt = Field(
        default=3, title="Stochastic slow period"
    )
    fi_ema_period: PositiveInt = Field(default=2, title="Force index EMA period")
    oversold_threshold: Decimal = Field(
        default=Decimal(20), gt=0, lt=100, title="Oversold threshold"
    )
    swing_low_h1_n: PositiveInt = Field(default=2, title="Swing low H1 N")
    trail_m15_n: PositiveInt = Field(default=2, title="Trail M15 N")
    stop_buffer_pct: Decimal = Field(
        default=Decimal("0.1"), ge=0, lt=100, title="Stop buffer %"
    )
    min_stop_distance_pct: Decimal = Field(
        default=Decimal(1), gt=0, lt=100, title="Min stop distance %"
    )
    tp_pct: Decimal = Field(default=Decimal(10), gt=0, title="Take profit %")
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
            "Stage-2 cap: of the symbols that pass the EMA13/EMA34 screen, at "
            "most this many are streamed and fully processed, ranked by trend "
            "strength (EMA13-over-EMA34 spread)."
        ),
    )
    volume_window: Literal["24h"] = Field(default="24h", title="Volume window")
    kline_window: PositiveInt = Field(default=1000, title="Kline window")

    @model_validator(mode="after")
    def coherent(self) -> Self:
        fast, mid, slow = self.ema_tide_periods
        if not fast < mid < slow:
            raise ValueError(
                "EMA periods must be three strictly increasing positive periods"
            )
        tide, wave, ripple = (
            interval_ms(value)
            for value in (self.tide_interval, self.wave_interval, self.ripple_interval)
        )
        if tide != 4 * wave or wave != 4 * ripple:
            raise ValueError("timeframes must have tide:wave:ripple ratio 16:4:1")
        needed = max(
            slow + 1,
            self.stochastic_k_period + self.stochastic_d_period,
            self.stochastic_k_period
            + self.stochastic_d_period
            + self.stochastic_slow_period
            - 2,
            self.fi_ema_period + 2,
            self.swing_low_h1_n,
            self.trail_m15_n,
            2,
        )
        if self.kline_window < needed:
            raise ValueError(f"kline_window must be at least {needed}")
        return self

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> TripleScreenSettings:
        """Ignore shared/unknown command keys, validate all known parameters."""
        return cls.model_validate(
            {k: v for k, v in params.items() if k in cls.model_fields}
        )


__all__ = ["TripleScreenSettings"]
