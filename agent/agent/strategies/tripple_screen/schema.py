"""What JSON Schema cannot express about ``TripleScreenSettings``.

The published catalog (see :mod:`agent.protocol`) carries the model's JSON
Schema for field-level rules — names, types, bounds, enums, fixed-length
arrays, defaults. The rules below are cross-field, so they travel as data
instead: the backend enforces them before accepting a Start and the UI shows
the same message before sending one, both without reimplementing them.

These mirror :meth:`TripleScreenSettings.coherent` exactly; the publish-time
checks in :mod:`agent.protocol` fail fast if a referenced field is renamed or
an index goes out of range, and ``agent/tests/test_strategy_catalog.py``
asserts that the published rules reject the same payloads the model does.
"""

from __future__ import annotations

from typing import Any

from binance_sdk_spot.rest_api.models import KlinesIntervalEnum

from agent.strategies.tripple_screen.settings import TRAIL_CAP_MESSAGE, interval_ms

EMA_MESSAGE = "EMA tide periods must be three strictly increasing positive periods"
RATIO_MESSAGE = "timeframes must have tide:wave:ripple ratio 16:4:1"


def _intervals_ms() -> dict[str, int]:
    """Intervals the agent can actually serve, mapped to milliseconds.

    Derived from the SDK enums through the same :func:`interval_ms` the
    strategy uses, so the published list cannot drift from what a run accepts:
    values the SDK knows but ``interval_ms`` rejects (calendar/week alignment)
    are simply absent.
    """
    allowed: dict[str, int] = {}
    for member in KlinesIntervalEnum:
        value = str(getattr(member, "value", member))
        try:
            allowed[value] = interval_ms(value)
        except ValueError:
            continue
    return dict(sorted(allowed.items(), key=lambda item: item[1]))


#: Value tables referenced by CONSTRAINTS via ``{"lookup": ..., "field": ...}``.
#: ``intervals_ms`` also backs every field marked with the ``interval`` widget.
LOOKUPS: dict[str, dict[str, int]] = {"intervals_ms": _intervals_ms()}

#: Cross-field rules, in the constraint grammar of :mod:`agent.protocol`.
CONSTRAINTS: list[dict[str, Any]] = [
    {
        # stop ≤ fill × (1 + cap) < TP must hold for every fill (spec §7.4).
        "cmp": "lt",
        "lhs": {"field": "trail_cap_pct"},
        "rhs": {"field": "tp_pct"},
        "message": TRAIL_CAP_MESSAGE,
    },
    {
        "cmp": "lt",
        "lhs": {"field": ["ema_tide_periods", 0]},
        "rhs": {"field": ["ema_tide_periods", 1]},
        "message": EMA_MESSAGE,
    },
    {
        "cmp": "lt",
        "lhs": {"field": ["ema_tide_periods", 1]},
        "rhs": {"field": ["ema_tide_periods", 2]},
        "message": EMA_MESSAGE,
    },
    {
        "cmp": "eq",
        "lhs": {"lookup": "intervals_ms", "field": "tide_interval"},
        "rhs": {
            "op": "mul",
            "args": [
                {"const": 4},
                {"lookup": "intervals_ms", "field": "wave_interval"},
            ],
        },
        "message": RATIO_MESSAGE,
    },
    {
        "cmp": "eq",
        "lhs": {"lookup": "intervals_ms", "field": "wave_interval"},
        "rhs": {
            "op": "mul",
            "args": [
                {"const": 4},
                {"lookup": "intervals_ms", "field": "ripple_interval"},
            ],
        },
        "message": RATIO_MESSAGE,
    },
    {
        # max(slow + 1, k + d, k + d + slow - 2, fi + 2, swing_low, trail, 2)
        "cmp": "gte",
        "lhs": {"field": "kline_window"},
        "rhs": {
            "op": "max",
            "args": [
                {
                    "op": "add",
                    "args": [{"field": ["ema_tide_periods", 2]}, {"const": 1}],
                },
                {
                    "op": "add",
                    "args": [
                        {"field": "stochastic_k_period"},
                        {"field": "stochastic_d_period"},
                    ],
                },
                {
                    "op": "sub",
                    "args": [
                        {
                            "op": "add",
                            "args": [
                                {"field": "stochastic_k_period"},
                                {"field": "stochastic_d_period"},
                                {"field": "stochastic_slow_period"},
                            ],
                        },
                        {"const": 2},
                    ],
                },
                {
                    "op": "add",
                    "args": [{"field": "fi_ema_period"}, {"const": 2}],
                },
                {"field": "swing_low_h1_n"},
                {"field": "trail_m15_n"},
                {"const": 2},
            ],
        },
        "message": "kline_window must be at least {rhs}",
    },
]

__all__ = ["CONSTRAINTS", "LOOKUPS"]
