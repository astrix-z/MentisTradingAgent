"""Built-in setting presets of Triple Screen v2.

Full snapshots of the non-const parameters of :class:`TripleScreenV2Settings`
in JSON form, like the v1 presets (``volume_window`` is const and never part
of a preset).
"""

from __future__ import annotations

from typing import Any

#: The agent's own defaults; ``TripleScreenV2Settings()`` equals this preset.
DEFAULT: dict[str, Any] = {
    "quote_asset": "USDT",
    "tide_interval": "4h",
    "wave_interval": "1h",
    "ripple_interval": "15m",
    "ema_tide_periods": [5, 13, 34],
    "ema_wave_periods": [5, 13, 34],
    "ema_ripple_periods": [5, 13],
    "ripple_percentile": "95",
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

#: Published in this order; names are unique ignoring case.
BUILTIN_PRESETS: dict[str, dict[str, Any]] = {"Default": DEFAULT}

__all__ = ["BUILTIN_PRESETS", "DEFAULT"]
