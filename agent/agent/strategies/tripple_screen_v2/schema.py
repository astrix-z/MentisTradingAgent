"""What JSON Schema cannot express about ``TripleScreenV2Settings``.

Mirrors :meth:`TripleScreenV2Settings.coherent` exactly, in the constraint
grammar of :mod:`agent.protocol` (see the v1 module for why these travel as
data). ``agent/tests/test_tripple_screen_v2.py`` asserts that the published
rules reject the same payloads the model does.
"""

from __future__ import annotations

from typing import Any

from agent.strategies.tripple_screen.schema import LOOKUPS
from agent.strategies.tripple_screen.settings import (
    TIMEFRAME_MESSAGE,
    TRAIL_CAP_MESSAGE,
)
from agent.strategies.tripple_screen_v2.settings import (
    RIPPLE_EMA_MESSAGE,
    TIDE_EMA_MESSAGE,
    WAVE_EMA_MESSAGE,
)


def _increasing(field: str, length: int, message: str) -> list[dict[str, Any]]:
    return [
        {
            "cmp": "lt",
            "lhs": {"field": [field, index]},
            "rhs": {"field": [field, index + 1]},
            "message": message,
        }
        for index in range(length - 1)
    ]


#: Cross-field rules, in the constraint grammar of :mod:`agent.protocol`.
CONSTRAINTS: list[dict[str, Any]] = [
    {
        # stop ≤ fill × (1 + cap) < TP must hold for every fill (spec §7.4).
        "cmp": "lt",
        "lhs": {"field": "trail_cap_pct"},
        "rhs": {"field": "tp_pct"},
        "message": TRAIL_CAP_MESSAGE,
    },
    *_increasing("ema_tide_periods", 3, TIDE_EMA_MESSAGE),
    *_increasing("ema_wave_periods", 3, WAVE_EMA_MESSAGE),
    *_increasing("ema_ripple_periods", 2, RIPPLE_EMA_MESSAGE),
    {
        "cmp": "gt",
        "lhs": {"lookup": "intervals_ms", "field": "tide_interval"},
        "rhs": {"lookup": "intervals_ms", "field": "wave_interval"},
        "message": TIMEFRAME_MESSAGE,
    },
    {
        "cmp": "gt",
        "lhs": {"lookup": "intervals_ms", "field": "wave_interval"},
        "rhs": {"lookup": "intervals_ms", "field": "ripple_interval"},
        "message": TIMEFRAME_MESSAGE,
    },
    {
        # max(tide slow + 1, wave slow, ripple mid + 1, swing_low, trail, 2)
        "cmp": "gte",
        "lhs": {"field": "kline_window"},
        "rhs": {
            "op": "max",
            "args": [
                {
                    "op": "add",
                    "args": [{"field": ["ema_tide_periods", 2]}, {"const": 1}],
                },
                {"field": ["ema_wave_periods", 2]},
                {
                    "op": "add",
                    "args": [{"field": ["ema_ripple_periods", 1]}, {"const": 1}],
                },
                {"field": "swing_low_wave_n"},
                {"field": "trail_ripple_n"},
                {"const": 2},
            ],
        },
        "message": "kline_window must be at least {rhs}",
    },
]

__all__ = ["CONSTRAINTS", "LOOKUPS"]
