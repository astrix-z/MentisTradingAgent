"""Validated, immutable startup configuration (Triple Screen spec §10)."""

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

    quote_asset: Literal["USDT", "USDC"] = "USDT"
    tide_interval: str = "4h"
    wave_interval: str = "1h"
    ripple_interval: str = "15m"
    ema_tide_periods: tuple[PositiveInt, PositiveInt, PositiveInt] = (5, 13, 34)
    stochastic_k_period: PositiveInt = 5
    stochastic_d_period: PositiveInt = 3
    stochastic_slow_period: PositiveInt = 3
    fi_ema_period: PositiveInt = 2
    oversold_threshold: Decimal = Field(default=Decimal(20), gt=0, lt=100)
    swing_low_h1_n: PositiveInt = 2
    trail_m15_n: PositiveInt = 2
    stop_buffer_pct: Decimal = Field(default=Decimal("0.1"), ge=0, lt=100)
    min_stop_distance_pct: Decimal = Field(default=Decimal(1), gt=0, lt=100)
    tp_pct: Decimal = Field(default=Decimal(10), gt=0)
    cooldown_minutes: int = Field(default=60, ge=0)
    signal_validity_minutes: PositiveInt = 15
    rest_concurrency: int = Field(default=3, ge=3, le=5)
    ws_top_n: PositiveInt = 50
    volume_window: Literal["24h"] = "24h"
    kline_window: PositiveInt = 1000

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
