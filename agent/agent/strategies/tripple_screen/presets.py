"""Built-in setting presets of Triple Screen (presets plan §4.2, §5).

Each preset is a full snapshot: exactly the non-const parameters of
:class:`TripleScreenSettings`, in their JSON form (decimals as strings,
integers as numbers). ``volume_window`` is const and never part of a preset.
The catalog publishes them next to the settings schema; the backend stores
them as built-in presets. Changing a value here changes the preset users see.
"""

from __future__ import annotations

from typing import Any

#: The agent's own defaults; ``TripleScreenSettings()`` equals this preset.
DEFAULT: dict[str, Any] = {
    "quote_asset": "USDT",
    "tide_interval": "4h",
    "wave_interval": "1h",
    "ripple_interval": "15m",
    "ema_tide_periods": [5, 13, 34],
    "stochastic_k_period": 5,
    "stochastic_d_period": 3,
    "stochastic_slow_period": 3,
    "fi_ema_period": 2,
    "oversold_threshold": "20",
    "swing_low_wave_n": 2,
    "trail_ripple_n": 2,
    "stop_buffer_pct": "0.1",
    "min_stop_distance_pct": "1",
    "tp_pct": "10",
    "trail_cap_pct": "4",
    "max_hold_hours": "8",
    "cooldown_minutes": 60,
    "signal_validity_minutes": 15,
    "rest_concurrency": 3,
    "ws_top_n": 30,
    "kline_window": 1000,
}

#: Shorter timeframes and exits (T6): H1 / M15 / M5, TP 5 %, cap 1 %, 4 h hold.
FAST: dict[str, Any] = {
    **DEFAULT,
    "tide_interval": "1h",
    "wave_interval": "15m",
    "ripple_interval": "5m",
    "tp_pct": "5",
    "trail_cap_pct": "1",
    "max_hold_hours": "4",
    "signal_validity_minutes": 5,
}

#: Published in this order; names are unique ignoring case.
BUILTIN_PRESETS: dict[str, dict[str, Any]] = {"Default": DEFAULT, "Fast": FAST}

__all__ = ["BUILTIN_PRESETS", "DEFAULT", "FAST"]
