"""Interval lengths and bar boundaries (``agent.common.intervals``).

The weekly and daily boundaries are checked against real Binance kline rows
saved in ``tests/fixtures/binance_klines.json`` (source in the file), so CI
needs no network.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from agent.common.intervals import WEEK_ANCHOR_MS, bar_open, interval_ms
from agent.strategies.tripple_screen.schema import LOOKUPS

KLINES: dict[str, Any] = json.loads(
    (Path(__file__).parent / "fixtures" / "binance_klines.json").read_text()
)

DAY = 86_400_000
WEEK = 7 * DAY


def ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).replace(tzinfo=UTC).timestamp() * 1000)


@pytest.mark.parametrize(
    ("interval", "expected"),
    [
        ("1s", 1_000),
        ("1m", 60_000),
        ("15m", 900_000),
        ("4h", 14_400_000),
        ("12h", 43_200_000),
        ("1d", DAY),
        ("1w", WEEK),
    ],
)
def test_interval_ms(interval: str, expected: int) -> None:
    assert interval_ms(interval) == expected


@pytest.mark.parametrize("interval", ["1M", "3d", "M1", "1h ", "2d", ""])
def test_unsupported_intervals_are_rejected(interval: str) -> None:
    """``1M`` has no fixed length and ``3d`` is off the epoch grid; the rest
    are not SDK intervals. None may fall back to a guessed length."""
    with pytest.raises(ValueError):
        interval_ms(interval)


def test_week_anchor_is_the_first_monday_after_the_epoch() -> None:
    anchor = datetime.fromtimestamp(WEEK_ANCHOR_MS / 1000, UTC)
    assert anchor.isoformat() == "1970-01-05T00:00:00+00:00"
    assert anchor.weekday() == 0


def test_weekly_bar_opens_on_monday_not_on_the_epoch_grid() -> None:
    monday = ms("2026-09-21T00:00:00")
    assert bar_open(ms("2026-09-25T12:34:56"), "1w") == monday
    # Plain epoch flooring would land on Thursday 2026-09-24.
    assert ms("2026-09-25T12:34:56") // WEEK * WEEK == ms("2026-09-24T00:00:00")


def test_weekly_boundary_around_monday_midnight() -> None:
    monday = ms("2026-09-21T00:00:00")
    previous = monday - WEEK
    assert bar_open(monday - 2, "1w") == previous
    assert bar_open(monday - 1, "1w") == previous
    assert bar_open(monday, "1w") == monday


def test_daily_bar_opens_at_utc_midnight() -> None:
    midnight = ms("2026-09-25T00:00:00")
    assert bar_open(midnight - 1, "1d") == midnight - DAY
    assert bar_open(midnight, "1d") == midnight
    assert bar_open(midnight + DAY - 1, "1d") == midnight


def test_intraday_bars_keep_the_epoch_grid() -> None:
    t = ms("2026-09-25T13:47:12")
    assert bar_open(t, "4h") == ms("2026-09-25T12:00:00")
    assert bar_open(t, "15m") == ms("2026-09-25T13:45:00")


@pytest.mark.parametrize("interval", ["1w", "1d"])
def test_real_binance_rows_match_bar_open(interval: str) -> None:
    for row in KLINES[interval]:
        open_time, close_time = row[0], row[6]
        assert bar_open(open_time, interval) == open_time
        assert bar_open(close_time, interval) == open_time
        assert close_time == open_time + interval_ms(interval) - 1


def test_real_three_day_rows_are_off_the_epoch_grid() -> None:
    """Why ``3d`` is rejected: its bars do not start on multiples of 3 days."""
    for row in KLINES["3d"]:
        assert row[0] % (3 * DAY) != 0


def test_published_lookup_adds_day_and_week_only() -> None:
    lookup = LOOKUPS["intervals_ms"]
    assert lookup["1d"] == DAY
    assert lookup["1w"] == WEEK
    assert "1M" not in lookup
    assert "3d" not in lookup
    assert list(lookup) == sorted(lookup, key=lookup.__getitem__)
