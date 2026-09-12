"""Configuration for the Triple Screen strategy (spec §10).

The strategy settings are a *versioned configuration*: they are fixed at start
and must not change mid-run. The UI's strategy page fills this model and the
values travel to the agent as the command's ``params`` dict; the agent validates
them here (so unknown/invalid values are rejected before the strategy starts).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class TripleScreenSettings(BaseModel):
    """All tunables of the Triple Screen strategy and their defaults (spec §10)."""

    quote_asset: str = "USDT"

    # Timeframes: tide / wave / ripple.
    tide_interval: str = "4h"
    wave_interval: str = "1h"
    ripple_interval: str = "15m"

    # Screen 1 — Tide (EMA periods on close).
    ema_tide_periods: list[int] = Field(default_factory=lambda: [5, 13, 34])

    # Screen 2 — Wave (Stochastic + Force Index).
    stochastic_k_period: int = 5
    stochastic_d_period: int = 3
    stochastic_slow_period: int = 3
    fi_ema_period: int = 2
    oversold_threshold: float = 20.0

    # Risk/exit rules handed to the planner (stop/tp built from fill prices).
    swing_low_h1_n: int = 2
    trail_m15_n: int = 2
    stop_buffer_pct: float = 0.1
    min_stop_distance_pct: float = 1.0
    tp_pct: float = 10.0
    cooldown_minutes: int = 60
    signal_validity_minutes: int = 15

    # Data / capacity.
    rest_concurrency: int = 3
    ws_top_n: int = 50
    volume_window: str = "24h"
    kline_window: int = 1000

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> TripleScreenSettings:
        """Validate the UI-supplied ``params`` into a settings object.

        Unknown keys are ignored so the command payload may carry shared keys
        (e.g. risk limits) without breaking the strategy.
        """
        known = {k: v for k, v in params.items() if k in cls.model_fields}
        return cls.model_validate(known)


__all__ = ["TripleScreenSettings"]
