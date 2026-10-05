"""The signal tracker: every ENTRY to its first stop/take crossing (869f6m7vn).

Pure crossing rules first, then the tracker on a throwaway PostgreSQL database
(``tests/pg.py``) with scripted 1m bars and a clock.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from pg import scratch_engine
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agent.core.migrate import migrate
from agent.models import SignalWatch
from agent.signal_tracker import (
    MINUTE_MS,
    STOP,
    TAKE,
    SignalTracker,
    first_crossing,
    take_level,
)
from agent.strategies.base import Signal

T0 = datetime(2026, 9, 23, 14, 30, 20, tzinfo=UTC)  # the ENTRY, mid-minute


def ms(at: datetime) -> int:
    return int(at.timestamp() * 1000)


def bar(at: datetime, o: str, h: str, low: str, c: str) -> list[Any]:
    return [ms(at), o, h, low, c, "1", ms(at) + MINUTE_MS - 1]


# --- the rules ------------------------------------------------------------------------


def test_take_level_follows_the_tp_rule_template() -> None:
    assert take_level(Decimal(100), "fill x 1.05") == Decimal("105.00")
    assert take_level(Decimal(100), "take 5%") is None


def test_traded_through_the_stop_closes_at_the_stop() -> None:
    at = T0.replace(second=0)
    crossing = first_crossing(
        [bar(at, "101", "102", "100.5", "101"), bar(at, "100.5", "101", "98", "99")],
        Decimal(99),
        Decimal(110),
    )
    assert crossing is not None and crossing.reason == STOP
    assert crossing.price == Decimal(99) and crossing.at == at


def test_a_gap_closes_at_the_open() -> None:
    at = T0.replace(second=0)
    down = first_crossing([bar(at, "97", "98", "96", "97")], Decimal(99), Decimal(110))
    up = first_crossing(
        [bar(at, "112", "113", "111", "112")], Decimal(99), Decimal(110)
    )
    assert down is not None and (down.reason, down.price) == (STOP, Decimal(97))
    assert up is not None and (up.reason, up.price) == (TAKE, Decimal(112))


def test_take_traded_through_and_a_bar_reaching_both_is_a_stop() -> None:
    at = T0.replace(second=0)
    take = first_crossing(
        [bar(at, "105", "111", "104", "110")], Decimal(99), Decimal(110)
    )
    both = first_crossing(
        [bar(at, "105", "111", "98", "105")], Decimal(99), Decimal(110)
    )
    assert take is not None and (take.reason, take.price) == (TAKE, Decimal(110))
    assert both is not None and (both.reason, both.price) == (STOP, Decimal(99))
    assert (
        first_crossing([bar(at, "105", "109", "100", "105")], Decimal(99), Decimal(110))
        is None
    )


# --- the tracker ----------------------------------------------------------------------


class Market:
    """1m bars by symbol; ``klines`` filters like the exchange and pages."""

    def __init__(self) -> None:
        self.bars: dict[str, list[list[Any]]] = {}
        self.calls = 0
        self.fail = False

    async def klines(
        self,
        *,
        symbol: str,
        interval: str,
        limit: int | None = None,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list[Any]:
        assert interval == "1m"
        self.calls += 1
        if self.fail:
            raise RuntimeError("exchange unreachable")
        rows = [
            b
            for b in self.bars.get(symbol, [])
            if (start_time is None or b[0] >= start_time)
            and (end_time is None or b[0] <= end_time)
        ]
        return rows[: limit or 500]


class World:
    def __init__(self, db: AsyncEngine) -> None:
        self.db = db
        self.market = Market()
        self.now = T0
        self.published: list[Signal] = []
        self.redis_down = False

    async def publish(self, signal: Signal) -> None:
        if self.redis_down:
            raise RedisError("down")
        self.published.append(signal)

    def tracker(self) -> SignalTracker:
        return SignalTracker(
            self.db, lambda network: self.market, self.publish, clock=lambda: self.now
        )

    def flat(self, symbol: str, start: datetime, minutes: int, price: str) -> None:
        self.market.bars.setdefault(symbol, []).extend(
            bar(start + timedelta(minutes=k), price, price, price, price)
            for k in range(minutes)
        )


def entry(symbol: str = "SENTUSDT", *, entry_ref: str = "0.02300") -> Signal:
    return Signal(
        type="ENTRY",
        symbol=symbol,
        emitted_at=T0,
        epoch_id=f"{symbol}:1",
        entry_ref=Decimal(entry_ref),
        swing_low_ref=Decimal("0.02198799"),
        tp_rule="fill x 1.1",
        signal_id=f"entry-{symbol}",
    )


def run(scenario: Callable[[World], Awaitable[None]]) -> None:
    async def main() -> None:
        async with scratch_engine() as db:
            await migrate(db)
            await scenario(World(db))

    asyncio.run(main())


def test_sentusdt_closes_at_the_stop_in_the_minute_it_crossed() -> None:
    # The production case: stop 0.02198799, traded through at 14:48 UTC; the
    # view at 15:14 used to record 0.02161.
    async def scenario(state: World) -> None:
        tracker = state.tracker()
        await tracker.watch(entry(), "live")
        start = T0.replace(second=0)
        state.market.bars["SENTUSDT"] = [
            # The ENTRY's own minute dipped below the stop before it: not read.
            bar(start, "0.02300", "0.02310", "0.02190", "0.02300"),
            *(
                bar(
                    start + timedelta(minutes=k), "0.0225", "0.0226", "0.0222", "0.0225"
                )
                for k in range(1, 18)
            ),
            bar(
                start + timedelta(minutes=18),
                "0.02209",
                "0.02210",
                "0.02194",
                "0.02198",
            ),
            bar(
                start + timedelta(minutes=40),
                "0.02163",
                "0.02171",
                "0.02161",
                "0.02161",
            ),
        ]
        state.now = datetime(2026, 9, 23, 15, 14, 52, tzinfo=UTC)
        assert await tracker.check() == 1
        [exit_] = state.published
        assert exit_.type == "EXIT" and exit_.exit_reason == STOP
        assert exit_.entry_signal_id == "entry-SENTUSDT"
        assert exit_.exit_price == Decimal("0.02198799")
        assert exit_.emitted_at == datetime(2026, 9, 23, 14, 48, tzinfo=UTC)
        assert exit_.transport()["exit_price"] == "0.02198799"
        # Published once; later rounds read nothing more for it.
        calls = state.market.calls
        assert await tracker.check() == 0
        assert state.market.calls == calls

    run(scenario)


def test_open_bars_are_not_read_and_the_watermark_follows() -> None:
    async def scenario(state: World) -> None:
        tracker = state.tracker()
        await tracker.watch(entry(), "live")
        await tracker.watch(entry(), "live")  # a repeated ENTRY is a no-op
        start = T0.replace(second=0) + timedelta(minutes=1)
        state.flat("SENTUSDT", start, 5, "0.0225")
        # 14:36:30 — the bar of 14:36 is still open: through 14:35 only.
        state.now = start + timedelta(minutes=5, seconds=30)
        state.market.bars["SENTUSDT"].append(
            bar(start + timedelta(minutes=5), "0.0225", "0.0225", "0.0210", "0.0212")
        )
        assert await tracker.check() == 0
        async with async_sessionmaker(state.db)() as session:
            row = await session.get(SignalWatch, "entry-SENTUSDT")
        assert row is not None and row.exit_reason is None
        assert row.checked_until == ms(start + timedelta(minutes=5))
        state.now += timedelta(minutes=1)  # now it is closed
        assert await tracker.check() == 1
        assert state.published[0].emitted_at == start + timedelta(minutes=5)

    run(scenario)


def test_a_crossing_during_downtime_is_found_after_a_restart() -> None:
    async def scenario(state: World) -> None:
        await state.tracker().watch(entry(), "live")
        start = T0.replace(second=0) + timedelta(minutes=1)
        state.flat("SENTUSDT", start, 2500, "0.0225")  # several pages of bars
        state.market.bars["SENTUSDT"][2300] = bar(
            start + timedelta(minutes=2300), "0.0230", "0.0254", "0.0229", "0.0250"
        )
        state.now = start + timedelta(minutes=2600)
        assert await state.tracker().check() == 1  # a new process
        [exit_] = state.published
        assert exit_.exit_reason == TAKE
        assert exit_.exit_price == Decimal("0.02300") * Decimal("1.1")
        assert exit_.emitted_at == start + timedelta(minutes=2300)

    run(scenario)


def test_an_exit_redis_did_not_take_is_published_again() -> None:
    async def scenario(state: World) -> None:
        tracker = state.tracker()
        await tracker.watch(entry(), "live")
        start = T0.replace(second=0) + timedelta(minutes=1)
        state.market.bars["SENTUSDT"] = [
            bar(start, "0.0219", "0.0220", "0.0215", "0.0216")
        ]
        state.now = start + timedelta(minutes=2)
        state.redis_down = True
        assert await tracker.check() == 0
        state.redis_down = False
        assert await tracker.check() == 1
        assert await tracker.check() == 0
        [exit_] = state.published
        assert exit_.exit_price == Decimal("0.0219")  # gapped below the stop

    run(scenario)


def test_unreadable_bars_of_one_symbol_do_not_stop_the_others() -> None:
    async def scenario(state: World) -> None:
        tracker = state.tracker()
        await tracker.watch(entry("AAAUSDT"), "live")
        await tracker.watch(entry("BBBUSDT"), "live")
        start = T0.replace(second=0) + timedelta(minutes=1)
        state.market.bars["BBBUSDT"] = [
            bar(start, "0.0225", "0.0226", "0.0210", "0.0211")
        ]
        original = state.market.klines

        async def flaky(**kwargs: Any) -> list[Any]:
            if kwargs["symbol"] == "AAAUSDT":
                raise RuntimeError("Invalid symbol.")
            return await original(**kwargs)

        state.market.klines = flaky  # type: ignore[method-assign]
        state.now = start + timedelta(minutes=2)
        assert await tracker.check() == 1
        assert [s.entry_signal_id for s in state.published] == ["entry-BBBUSDT"]

    run(scenario)


def test_without_a_database_nothing_is_followed() -> None:
    async def scenario() -> None:
        published: list[Signal] = []

        async def publish(signal: Signal) -> None:
            published.append(signal)

        tracker = SignalTracker(None, lambda network: Market(), publish)
        await tracker.watch(entry(), "live")
        assert await tracker.check() == 0

    asyncio.run(scenario())
