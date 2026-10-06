"""Kline interval durations and bar boundaries (UTC).

One place for everything the agent derives from an interval string, so the
strategy, the position monitor and the published catalog cannot disagree about
which intervals exist or where a bar starts.

Supported are the fixed-length SDK intervals whose bars Binance opens on a
fixed grid: seconds/minutes/hours and ``1d`` on the Unix-epoch grid, ``1w`` on
the grid of Mondays (Binance weekly bars open Monday 00:00 UTC; the epoch itself
is a Thursday). ``1M`` (calendar month) has no fixed length, and ``3d`` bars are
not on the epoch grid (they open one day off it), so both are rejected rather
than silently misaligned.
"""

from __future__ import annotations

from binance_sdk_spot.rest_api.models import KlinesIntervalEnum
from binance_sdk_spot.websocket_streams.models import KlineIntervalEnum

_SECOND = 1_000
_MINUTE = 60 * _SECOND
_HOUR = 60 * _MINUTE
_DAY = 24 * _HOUR
_WEEK = 7 * _DAY

_UNITS = {"s": _SECOND, "m": _MINUTE, "h": _HOUR}
#: Day/week intervals with a known grid; other multiples (``3d``) are not.
_CALENDAR = {"1d": _DAY, "1w": _WEEK}

#: 1970-01-05T00:00Z, the first Monday after the epoch: anchor of weekly bars.
WEEK_ANCHOR_MS = 4 * _DAY


def interval_ms(interval: str) -> int:
    """Length of one bar of ``interval`` in milliseconds.

    Raises:
        ValueError: the SDK does not know the interval, or it has no fixed
            length on a fixed grid (``1M``, ``3d``).
    """
    KlinesIntervalEnum(interval)
    KlineIntervalEnum(interval)
    if interval in _CALENDAR:
        return _CALENDAR[interval]
    unit = _UNITS.get(interval[-1])
    if unit is None:
        raise ValueError(f"interval {interval!r} is not supported")
    return int(interval[:-1]) * unit


def bar_open(ts_ms: int, interval: str) -> int:
    """Open time of the ``interval`` bar that contains ``ts_ms``."""
    step = interval_ms(interval)
    anchor = WEEK_ANCHOR_MS if interval == "1w" else 0
    return (ts_ms - anchor) // step * step + anchor


__all__ = ["WEEK_ANCHOR_MS", "bar_open", "interval_ms"]
