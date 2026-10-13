"""The signal tracker: every ENTRY to its first stop/take crossing (869f6m7vn).

Pure crossing rules first, then the tracker on a throwaway PostgreSQL database
(``tests/pg.py``) with scripted 1m bars and a clock.
"""

from __future__ import annotations

import asyncio
import json
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


def test_a_crossing_is_reported_at_once_and_restored_by_the_snapshot() -> None:
    # 869f6m7vg: the engine's virtual feedback hears of the close when it is
    # recorded — Redis down or not — and a Start reads it back.
    async def scenario(state: World) -> None:
        tracker = state.tracker()
        closed: list[tuple[str, datetime | None]] = []

        async def on_closed(watch: SignalWatch) -> None:
            closed.append((watch.symbol, watch.exited_at))

        tracker.on_closed = on_closed
        await tracker.watch(entry("AAAUSDT"), "live")
        await tracker.watch(entry("BBBUSDT"), "live")
        await tracker.watch(entry("CCCUSDT"), "testnet")
        start = T0.replace(second=0) + timedelta(minutes=1)
        state.market.bars["AAAUSDT"] = [
            bar(start, "0.0225", "0.0226", "0.0210", "0.0211")
        ]
        state.now = start + timedelta(minutes=2)
        state.redis_down = True
        await tracker.check()
        assert closed == [("AAAUSDT", start)]
        open_symbols, exits = await tracker.snapshot("live", start - timedelta(hours=1))
        assert open_symbols == frozenset({"BBBUSDT"})
        assert [(w.symbol, w.exited_at) for w in exits] == [("AAAUSDT", start)]
        assert (await tracker.snapshot("live", start + timedelta(minutes=1)))[1] == []

    run(scenario)


# --- the watch is as durable as the ENTRY (review 2026-10-05 REPEAT, R2) -------------------


def _engine(state: World, *, durable: bool) -> tuple[Any, Any, Any]:
    from unittest.mock import AsyncMock, Mock

    from agent.engine import Engine
    from agent.execution.runtime import ExecutionContext, ExecutionRuntime

    redis = Mock(publish=AsyncMock(), set=AsyncMock())
    engine = Engine(redis)
    engine._network = "testnet"
    engine._strategy_name = "tripple_screen"
    engine._state = "running"
    strategy = Mock(on_position_opened=AsyncMock(), on_position_closed=AsyncMock())
    engine._strategy = strategy
    engine._virtual_feedback = True
    if durable:
        runtime = ExecutionRuntime(db=state.db, db_url=None, schema_ready=True)
        runtime.context = ExecutionContext("1001", "testnet")
        runtime.schema_ready = True  # set by boot after the migration check
        engine.execution = runtime
    tracker = state.tracker()
    engine.tracker = tracker
    tracker.on_opened = engine.virtual_opened
    return engine, redis, strategy


async def _inbox(state: World) -> list[tuple[str, str]]:
    from sqlalchemy import select

    from agent.execution.models import SignalInbox

    async with async_sessionmaker(state.db)() as session:
        rows = (
            await session.execute(select(SignalInbox.symbol, SignalInbox.type))
        ).all()
    return [(symbol, kind) for symbol, kind in rows]


async def _watched(state: World) -> list[str]:
    async with async_sessionmaker(state.db)() as session:
        return list(
            (
                await session.scalars(
                    SignalWatch.__table__.select().with_only_columns(SignalWatch.symbol)
                )
            ).all()
        )


def test_a_failed_watch_fails_the_emit_and_the_retry_completes_it() -> None:
    async def scenario(state: World) -> None:
        from sqlalchemy.exc import OperationalError

        engine, redis, strategy = _engine(state, durable=True)
        real = engine.tracker.watch_in

        async def broken(*args: Any, **kwargs: Any) -> None:
            raise OperationalError("insert", {}, Exception("connection lost"))

        engine.tracker.watch_in = broken
        signal = entry()
        try:
            await engine._on_signal(signal)
        except OperationalError:
            pass
        else:
            raise AssertionError("the emit should fail with its watch")
        # Nothing half done: no inbox record, nothing published, nothing held.
        assert await _inbox(state) == []
        redis.publish.assert_not_awaited()
        strategy.on_position_opened.assert_not_awaited()
        engine.tracker.watch_in = real
        await engine._on_signal(signal)  # the strategy retries the epoch
        assert await _inbox(state) == [("SENTUSDT", "ENTRY")]
        assert await _watched(state) == ["SENTUSDT"]
        assert redis.publish.await_count == 1
        strategy.on_position_opened.assert_awaited_once()

    run(scenario)


def test_a_replay_completes_an_entry_recorded_without_its_watch() -> None:
    # An inbox record from before this change (or any path that missed the
    # watch): the replay of its epoch must not stop at the dedup.
    async def scenario(state: World) -> None:
        from agent.execution.admission import record_signal
        from agent.execution.runtime import ExecutionContext

        engine, redis, strategy = _engine(state, durable=True)
        signal = entry().identified(
            account_uid="1001", network="testnet", strategy="tripple_screen"
        )
        async with engine.execution.session() as session:
            await record_signal(
                session,
                signal,
                context=ExecutionContext("1001", "testnet"),
                strategy="tripple_screen",
            )
        assert await _watched(state) == []
        await engine._on_signal(signal)
        assert await _watched(state) == ["SENTUSDT"]
        redis.publish.assert_not_awaited()  # not re-emitted
        strategy.on_position_opened.assert_awaited_once()

    run(scenario)


def test_the_tracker_repairs_unwatched_inbox_entries() -> None:
    async def scenario(state: World) -> None:
        from agent.execution.admission import record_signal
        from agent.execution.runtime import ExecutionContext

        engine, _redis, strategy = _engine(state, durable=True)
        signal = entry().identified(
            account_uid="1001", network="testnet", strategy="tripple_screen"
        )
        async with engine.execution.session() as session:
            await record_signal(
                session,
                signal,
                context=ExecutionContext("1001", "testnet"),
                strategy="tripple_screen",
            )
        state.now = T0 + timedelta(minutes=5)
        await engine.tracker.check()
        assert await _watched(state) == ["SENTUSDT"]
        strategy.on_position_opened.assert_awaited_once()
        await engine.tracker.check()  # once
        strategy.on_position_opened.assert_awaited_once()

    run(scenario)


def test_without_an_inbox_a_failed_publish_drops_the_watch() -> None:
    async def scenario(state: World) -> None:
        engine, redis, strategy = _engine(state, durable=False)
        redis.publish.side_effect = RedisError("down")
        try:
            await engine._on_signal(entry())
        except RedisError:
            pass
        else:
            raise AssertionError("a failed publish fails the emit without an inbox")
        assert await _watched(state) == []
        strategy.on_position_opened.assert_not_awaited()
        redis.publish.side_effect = None
        await engine._on_signal(entry())
        assert await _watched(state) == ["SENTUSDT"]
        strategy.on_position_opened.assert_awaited_once()

    run(scenario)


def test_a_tracker_exit_never_reaches_the_execution_inbox() -> None:
    async def scenario(state: World) -> None:
        engine, redis, _strategy = _engine(state, durable=True)
        engine.tracker._publish = engine.publish_exit
        await engine._on_signal(entry())
        start = T0.replace(second=0) + timedelta(minutes=1)
        state.market.bars["SENTUSDT"] = [
            bar(start, "0.0219", "0.0220", "0.0215", "0.0216")
        ]
        state.now = start + timedelta(minutes=2)
        assert await engine.tracker.check() == 1
        assert await _inbox(state) == [("SENTUSDT", "ENTRY")]
        kinds = [json.loads(c.args[1])["type"] for c in redis.publish.await_args_list]
        assert kinds == ["ENTRY", "EXIT"]

    run(scenario)
