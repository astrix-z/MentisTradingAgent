"""EXEC-07: strategy feedback — positions and an absolute, persistent cooldown."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from pg import scratch_database
from sqlalchemy.ext.asyncio import create_async_engine
from test_engine_lifecycle import setup_engine, start

# The autouse clock and the bar fixtures of the runtime suite.
from test_strategy_runtime import (  # noqa: F401
    PARAMS,
    SYMBOL,
    ManualClock,
    RedisSink,
    Rest,
    Streams,
    clock,
    fixture,
    services,
)

from agent.core.migrate import migrate
from agent.execution.models import Position, SymbolCooldown
from agent.execution.runtime import ExecutionContext, ExecutionRuntime
from agent.models import SignalWatch
from agent.strategies.base import PositionEvent, PositionsSnapshot, Signal
from agent.strategies.tripple_screen.strategy import TripleScreenStrategy


def now(manual: ManualClock) -> datetime:
    return datetime.fromtimestamp(manual.now_ms / 1000, UTC)


def event(symbol: str = SYMBOL, **values: Any) -> PositionEvent:
    return PositionEvent(
        event_id=values.pop("event_id", "e1"),
        symbol=symbol,
        position_id="p1",
        at=values.pop("at"),
        **values,
    )


def test_open_position_holds_entries_and_close_starts_the_given_cooldown(
    clock: ManualClock,  # noqa: F811 - the imported fixture
) -> None:
    async def scenario() -> None:
        strategy = TripleScreenStrategy(PARAMS)
        await strategy.on_position_opened(event(at=now(clock)))
        held = strategy._decide(SYMBOL, fixture())
        assert held is not None and held.reason == "position_open"
        until = now(clock) + timedelta(minutes=60)
        await strategy.on_position_closed(event(at=now(clock), cooldown_until=until))
        cooling = strategy._decide(SYMBOL, fixture())
        assert cooling is not None and cooling.reason == "cooldown"
        remaining = cooling.provenance["state"]["cooldown_remaining_seconds"]
        assert remaining == pytest.approx(3600)

    asyncio.run(scenario())


def test_cooldown_survives_demotion_and_promotion(
    clock: ManualClock,  # noqa: F811 - the imported fixture
) -> None:
    async def scenario() -> None:
        strategy = TripleScreenStrategy(PARAMS)
        until = now(clock) + timedelta(minutes=60)
        await strategy.on_position_closed(event(at=now(clock), cooldown_until=until))
        strategy._forget_symbol(SYMBOL)  # dropped from the active set
        cooling = strategy._decide(SYMBOL, fixture())  # promoted back
        assert cooling is not None and cooling.reason == "cooldown"

    asyncio.run(scenario())


def test_repeated_close_does_not_extend_and_new_settings_do_not_rewrite(
    clock: ManualClock,  # noqa: F811 - the imported fixture
) -> None:
    async def scenario() -> None:
        until = now(clock) + timedelta(minutes=10)
        strategy = TripleScreenStrategy({**PARAMS, "cooldown_minutes": 600})
        await strategy.restore_positions(
            PositionsSnapshot(cooldown_until={SYMBOL: until})
        )
        await strategy.on_position_closed(
            event(at=now(clock) + timedelta(minutes=5), cooldown_until=until)
        )
        assert strategy._cooldown_until[SYMBOL] == until  # not now + 600 min
        clock.now_ms += 11 * 60_000
        # Past the absolute end nothing holds the symbol any more.
        assert strategy._cooldown_until[SYMBOL] < now(clock)

    asyncio.run(scenario())


def test_position_outside_universe_and_cap_is_streamed_from_a_cold_start(
    clock: ManualClock,  # noqa: F811 - the imported fixture
) -> None:
    async def scenario() -> None:
        strategy = TripleScreenStrategy({**PARAMS, "ws_top_n": 1})
        await strategy.restore_positions(
            PositionsSnapshot(open_symbols=frozenset({"ETHUSDT"}))
        )
        rest, streams, sink = Rest(fixture()), Streams(), RedisSink()
        strategy._services = services(rest, streams, sink)  # universe: BTCUSDT
        strategy._universe = [SYMBOL]
        selected = await strategy._screen_universe()
        assert selected == {"ETHUSDT"}  # kept although not screened and cap is 1

    asyncio.run(scenario())


def test_engine_restores_positions_before_the_strategy_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        engine, _redis, strategy, _streams = setup_engine(monkeypatch)
        order: list[str] = []
        snapshot = PositionsSnapshot(open_symbols=frozenset({"BTCUSDT"}))
        strategy.restore_positions = AsyncMock(
            side_effect=lambda s: order.append("restore")
        )
        strategy.on_start = AsyncMock(side_effect=lambda s: order.append("start"))
        runtime = AsyncMock()
        runtime.positions_snapshot = AsyncMock(return_value=snapshot)
        runtime.open_admission = lambda name: None
        runtime.close_admission = lambda: None
        runtime.heartbeat = lambda: {}
        runtime.message = lambda: None
        engine.execution = runtime
        await start(engine)
        assert order == ["restore", "start"]
        strategy.restore_positions.assert_awaited_once_with(snapshot)
        opened = event(at=datetime.now(UTC))
        strategy.on_position_opened = AsyncMock()
        await engine.position_feedback(opened, False)
        strategy.on_position_opened.assert_awaited_once_with(opened)
        await engine.stop()
        strategy.on_position_opened.reset_mock()
        await engine.position_feedback(opened, False)  # no run, nothing to tell
        strategy.on_position_opened.assert_not_awaited()
        await engine.shutdown()

    asyncio.run(scenario())


def test_runtime_snapshot_reads_open_positions_and_running_cooldowns() -> None:
    async def scenario() -> None:
        async with scratch_database() as url:
            db = create_async_engine(url)
            await migrate(db)
            moment = datetime(2026, 9, 24, 12, tzinfo=UTC)
            runtime = ExecutionRuntime(
                db=db, db_url=url, schema_ready=True, clock=lambda: moment
            )
            runtime.context = ExecutionContext("1001", "live")
            async with runtime.session() as session:
                for symbol, status in (("BTCUSDT", "protected"), ("ETHUSDT", "closed")):
                    session.add(
                        Position(
                            id=uuid.uuid4(),
                            account="1001",
                            network="live",
                            symbol=symbol,
                            strategy="tripple_screen",
                            signal_id=uuid.uuid4(),
                            status=status,
                            exit_policy={},
                            quantity=Decimal(0),
                        )
                    )
                for symbol, minutes in (("ETHUSDT", 30), ("SOLUSDT", -1)):
                    session.add(
                        SymbolCooldown(
                            account="1001",
                            network="live",
                            symbol=symbol,
                            cooldown_until=moment + timedelta(minutes=minutes),
                            position_id=uuid.uuid4(),
                        )
                    )
                await session.commit()
            snapshot = await runtime.positions_snapshot()
            assert snapshot.open_symbols == {"BTCUSDT"}
            assert snapshot.cooldown_until == {
                "ETHUSDT": moment + timedelta(minutes=30)
            }
            await db.dispose()

    asyncio.run(scenario())


def test_lost_feedback_never_breaks_execution() -> None:
    async def scenario() -> None:
        runtime = ExecutionRuntime(db=None, db_url=None, schema_ready=False)
        runtime.feedback = AsyncMock(side_effect=RuntimeError("strategy crashed"))
        await runtime.notify(event(at=datetime.now(UTC)), closed=True)  # no raise

    asyncio.run(scenario())


def test_changes_during_startup_are_replayed_after_on_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Review 2026-09-30 (EXEC-07): BTC closed through the worker/finalizer
    # while on_start was running; the feedback was dropped and the strategy
    # kept holding BTC without its cooldown.
    async def scenario() -> None:
        engine, _redis, strategy, _streams = setup_engine(monkeypatch)
        at = datetime.now(UTC)
        until = at + timedelta(minutes=60)
        held: set[str] = set()
        cooldown: dict[str, datetime] = {}
        order: list[str] = []

        async def restore(snapshot: PositionsSnapshot) -> None:
            held.clear()
            held.update(snapshot.open_symbols)
            order.append("restore")

        async def opened(e: PositionEvent) -> None:
            held.add(e.symbol)
            order.append(f"opened {e.symbol}")

        async def closed(e: PositionEvent) -> None:
            held.discard(e.symbol)
            if e.cooldown_until is not None:
                cooldown[e.symbol] = e.cooldown_until
            order.append(f"closed {e.symbol}")

        async def on_start(services: Any) -> None:
            order.append("on_start")
            # The finalizer commits BTC's close while the strategy starts.
            await engine.position_feedback(
                event("BTCUSDT", at=at, cooldown_until=until), True
            )

        strategy.restore_positions = AsyncMock(side_effect=restore)
        strategy.on_position_opened = AsyncMock(side_effect=opened)
        strategy.on_position_closed = AsyncMock(side_effect=closed)
        strategy.on_start = AsyncMock(side_effect=on_start)

        async def snapshot() -> PositionsSnapshot:
            # ETH opens right after the snapshot was read.
            await engine.position_feedback(event("ETHUSDT", at=at), False)
            return PositionsSnapshot(open_symbols=frozenset({"BTCUSDT"}))

        runtime = AsyncMock()
        runtime.positions_snapshot = AsyncMock(side_effect=snapshot)
        runtime.open_admission = lambda name: None
        runtime.close_admission = lambda: None
        runtime.heartbeat = lambda: {}
        runtime.message = lambda: None
        engine.execution = runtime
        await start(engine)
        assert order == ["restore", "on_start", "opened ETHUSDT", "closed BTCUSDT"]
        assert held == {"ETHUSDT"}
        assert cooldown == {"BTCUSDT": until}
        # Running: delivered directly, nothing buffered any more.
        await engine.position_feedback(event("ETHUSDT", at=at), True)
        assert held == set()
        assert engine._startup_feedback is None
        await engine.shutdown()

    asyncio.run(scenario())


def test_a_failed_start_drops_its_buffer(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        engine, _redis, strategy, _streams = setup_engine(monkeypatch)

        async def on_start(services: Any) -> None:
            await engine.position_feedback(event(at=datetime.now(UTC)), True)
            raise RuntimeError("no market data")

        strategy.on_start = AsyncMock(side_effect=on_start)
        strategy.on_position_closed = AsyncMock()
        with pytest.raises(RuntimeError):
            await start(engine)
        assert engine._startup_feedback is None
        strategy.on_position_closed.assert_not_awaited()
        await engine.shutdown()

    asyncio.run(scenario())


def test_a_refused_start_delivers_its_buffer_to_the_run_still_going(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Review 2026-10-03: BTC closed during prepare_start of a new Start that
    # was then refused (credentials, account); the old strategy kept running
    # but its close was buffered and dropped.
    async def scenario() -> None:
        engine, _redis, strategy, _streams = setup_engine(monkeypatch)
        closed: list[str] = []

        async def on_closed(e: PositionEvent) -> None:
            closed.append(e.symbol)

        strategy.on_position_closed = AsyncMock(side_effect=on_closed)
        runtime = AsyncMock()
        runtime.positions_snapshot = AsyncMock(return_value=PositionsSnapshot())
        runtime.open_admission = lambda name: None
        runtime.close_admission = lambda: None
        runtime.heartbeat = lambda: {}
        runtime.message = lambda: None
        engine.execution = runtime
        await start(engine)
        assert engine._state == "running"
        at = datetime.now(UTC)

        async def refused(**kwargs: Any) -> None:
            await engine.position_feedback(
                event("BTCUSDT", at=at, cooldown_until=at + timedelta(hours=1)), True
            )
            raise ValueError("credentials belong to another account")

        runtime.prepare_start = AsyncMock(side_effect=refused)
        with pytest.raises(ValueError):
            await start(engine)
        assert engine._state == "running" and engine._strategy is strategy
        assert closed == ["BTCUSDT"]  # delivered once, to the run still going
        assert engine._startup_feedback is None
        await engine.position_feedback(event("ETHUSDT", at=at), True)
        assert closed == ["BTCUSDT", "ETHUSDT"]  # and directly afterwards
        await engine.shutdown()

    asyncio.run(scenario())


# --- signal-only runs: virtual positions from the signal tracker (869f6m7vg) -----------


def _watch(symbol: str, signal_id: str, exited_at: datetime | None) -> SignalWatch:
    return SignalWatch(
        signal_id=signal_id,
        network="testnet",
        symbol=symbol,
        entry_ref=Decimal(100),
        stop=Decimal(90),
        take=Decimal(110),
        emitted_at=datetime.now(UTC) - timedelta(hours=2),
        checked_until=0,
        exit_reason=None if exited_at is None else "stop",
        exited_at=exited_at,
    )


def _recording(strategy: Any) -> list[str]:
    seen: list[str] = []

    async def restore(snapshot: PositionsSnapshot) -> None:
        seen.append(f"restore {sorted(snapshot.open_symbols)}")

    async def opened(e: PositionEvent) -> None:
        seen.append(f"opened {e.symbol}")

    async def closed(e: PositionEvent) -> None:
        seen.append(f"closed {e.symbol} at {e.at:%H:%M}")

    strategy.restore_positions = AsyncMock(side_effect=restore)
    strategy.on_position_opened = AsyncMock(side_effect=opened)
    strategy.on_position_closed = AsyncMock(side_effect=closed)
    return seen


def test_a_signal_only_run_hears_of_its_virtual_positions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # SENTUSDT emitted three ENTRYs while its first was still open: nothing
    # told the strategy. Without real execution the tracker's ENTRY/EXIT are
    # its positions — restored on Start, opened by an ENTRY, closed (and the
    # cooldown started) at the crossing.
    async def scenario() -> None:
        engine, _redis, strategy, _streams = setup_engine(monkeypatch)
        seen = _recording(strategy)
        crossed = datetime.now(UTC).replace(second=0, microsecond=0)
        tracker = Mock(
            watch=AsyncMock(),
            is_open=AsyncMock(return_value=True),
            snapshot=AsyncMock(
                return_value=(
                    frozenset({"ETHUSDT"}),
                    [
                        _watch("SOLUSDT", "sol", crossed - timedelta(minutes=30)),
                        # Followed again since: still open, not closed over.
                        _watch("ETHUSDT", "eth-old", crossed - timedelta(hours=1)),
                    ],
                )
            ),
        )
        engine.tracker = tracker
        await start(engine)
        assert seen == [
            "restore ['ETHUSDT']",
            f"closed SOLUSDT at {crossed - timedelta(minutes=30):%H:%M}",
        ]
        assert tracker.snapshot.await_args.args[0] == "testnet"

        entry = Signal(
            type="ENTRY",
            symbol="SENTUSDT",
            emitted_at=crossed,
            epoch_id="SENTUSDT:1",
            entry_ref=Decimal("0.023"),
            swing_low_ref=Decimal("0.02198799"),
            tp_rule="fill x 1.1",
        )
        await engine._on_signal(entry)
        assert seen[-1] == "opened SENTUSDT"
        await engine.virtual_closed(_watch("SENTUSDT", entry.signal_id, crossed))
        assert seen[-1] == f"closed SENTUSDT at {crossed:%H:%M}"
        await engine.shutdown()

    asyncio.run(scenario())


def test_with_real_execution_virtual_positions_are_not_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Real positions are the truth there (EXEC-07); an ENTRY admission refused
    # must not hold the symbol as a virtual one.
    async def scenario() -> None:
        engine, _redis, strategy, _streams = setup_engine(monkeypatch)
        seen = _recording(strategy)
        tracker = Mock(watch=AsyncMock(), snapshot=AsyncMock())
        engine.tracker = tracker
        runtime = AsyncMock()
        runtime.positions_snapshot = AsyncMock(return_value=PositionsSnapshot())
        runtime.open_admission = lambda name: None
        runtime.close_admission = lambda: None
        runtime.heartbeat = lambda: {}
        runtime.message = lambda: None
        runtime.context = None
        engine.execution = runtime
        await start(engine, execution={"enabled": True})
        tracker.snapshot.assert_not_awaited()
        entry = Signal(
            type="ENTRY",
            symbol="SENTUSDT",
            emitted_at=datetime.now(UTC),
            entry_ref=Decimal("0.023"),
            swing_low_ref=Decimal("0.02198799"),
            tp_rule="fill x 1.1",
        )
        await engine._on_signal(entry)
        tracker.watch.assert_awaited_once()  # still followed for the backend
        await engine.virtual_closed(
            _watch("SENTUSDT", entry.signal_id, datetime.now(UTC))
        )
        assert seen == ["restore []"]
        await engine.shutdown()

    asyncio.run(scenario())
