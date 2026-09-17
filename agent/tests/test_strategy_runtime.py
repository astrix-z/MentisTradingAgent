"""Offline lifecycle, SDK callback, snapshot and transport regression tests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from itertools import permutations
from typing import Any, cast

import pytest
from binance_sdk_spot.websocket_streams.models import KlineResponse, KlineResponseK
from pydantic import ValidationError

from agent.exchange.rest import ExchangeRest
from agent.exchange.streams import MarketDataStreams
from agent.strategies.base import Signal, StrategyServices
from agent.strategies.tripple_screen.indicators import Candle
from agent.strategies.tripple_screen.settings import TripleScreenSettings, interval_ms
from agent.strategies.tripple_screen.strategy import TripleScreenStrategy

SYMBOL = "BTCUSDT"
PARAMS: dict[str, Any] = {
    "kline_window": 12,
    "ema_tide_periods": [2, 3, 4],
    "stochastic_k_period": 2,
    "stochastic_d_period": 2,
    "stochastic_slow_period": 2,
}
TRIGGER = 1_800_000_000 // 14_400_000 * 14_400_000 + 900_000


@dataclass
class ManualClock:
    now_ms: int = TRIGGER + 900_000


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> ManualClock:
    clock = ManualClock()

    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Clock:
            return cls.fromtimestamp(clock.now_ms / 1000, UTC)

    monkeypatch.setattr("agent.strategies.tripple_screen.strategy.datetime", Clock)
    return clock


def bar(open_time: int, interval: str, close: str = "11.200000000000000001") -> Candle:
    value = Decimal(close)
    return Candle(
        open_time,
        open_time + interval_ms(interval) - 1,
        value,
        Decimal(19),
        Decimal(9),
        value,
        Decimal(1),
        True,
    )


def fixture(trigger_time: int = TRIGGER, count: int = 12) -> dict[str, list[Candle]]:
    result = {}
    for interval in ("4h", "1h", "15m"):
        step = interval_ms(interval)
        cutoff = trigger_time if interval == "1h" else trigger_time + 899_999
        end = (cutoff + 1) // step * step
        result[interval] = [
            bar(
                end - (count - i) * step,
                interval,
                str(100 + i) if interval == "4h" else "10",
            )
            for i in range(count)
        ]
    wave = result["1h"]
    wave[-2] = replace(wave[-2], close=Decimal(9))
    wave[-1] = replace(wave[-1], close=Decimal(11))
    ripple = result["15m"]
    ripple[-2] = replace(ripple[-2], high=Decimal(11))
    ripple[-1] = replace(ripple[-1], close=Decimal("11.200000000000000001"))
    return result


def message(c: Candle) -> KlineResponse:
    return KlineResponse(
        k=KlineResponseK(
            t=c.open_time,
            T=c.close_time,
            o=str(c.open),
            h=str(c.high),
            l=str(c.low),
            c=str(c.close),
            v=str(c.volume),
            x=c.closed,
        )
    )


class Rest:
    def __init__(self, windows: dict[str, list[Candle]], page_size: int = 1000) -> None:
        self.windows = windows
        self.page_size = page_size
        self.calls: list[dict[str, Any]] = []

    async def klines(self, **kwargs: Any) -> list[Any]:
        self.calls.append(kwargs)
        await asyncio.sleep(0)
        bars = [
            c
            for c in self.windows[kwargs["interval"]]
            if kwargs["start_time"] <= c.open_time <= kwargs["end_time"]
        ]
        return [
            [
                c.open_time,
                str(c.open),
                str(c.high),
                str(c.low),
                str(c.close),
                str(c.volume),
                c.close_time,
            ]
            for c in bars[: min(self.page_size, kwargs["limit"])]
        ]


class Streams:
    def __init__(self) -> None:
        self.callbacks: dict[str, Callable[[KlineResponse], None]] = {}

    async def subscribe_kline(
        self, *, symbol: str, interval: str, callback: Callable[[KlineResponse], None]
    ) -> None:
        self.callbacks[interval] = callback


class RedisSink:
    def __init__(self) -> None:
        self.messages: dict[str, str] = {}
        self.attempts: list[Signal] = []
        self.fail = False
        self.accept_before_failure = False

    async def emit(self, signal: Signal) -> None:
        self.attempts.append(signal)
        if not self.fail or self.accept_before_failure:
            await self.publish(
                "agent:events", json.dumps({"kind": "signal", **signal.transport()})
            )
        if self.fail:
            raise RuntimeError("sink unavailable")

    async def publish(self, channel: str, payload: str) -> int:
        assert channel == "agent:events"
        decoded = json.loads(payload)
        # Models an idempotent downstream consumer, not Redis Pub/Sub guarantees.
        self.messages.setdefault(decoded["signal_id"], payload)
        return 1


def services(rest: Rest, streams: Streams, sink: RedisSink) -> StrategyServices:
    return StrategyServices(
        "testnet",
        [SYMBOL],
        cast(ExchangeRest, rest),
        cast(MarketDataStreams, streams),
        sink.emit,
    )


async def drain(strategy: TripleScreenStrategy, sink: RedisSink, count: int) -> None:
    for _ in range(200):
        if len(sink.messages) >= count:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"no signal: pending={strategy._pending}")


def test_lifecycle_callback_transport_and_hold(clock: ManualClock) -> None:
    async def run() -> None:
        windows = fixture()
        clock.now_ms = TRIGGER
        rest, streams, sink = Rest(windows), Streams(), RedisSink()
        strategy = TripleScreenStrategy(PARAMS)
        await strategy.on_start(services(rest, streams, sink))
        assert strategy.services.rest is cast(ExchangeRest, rest)
        clock.now_ms = TRIGGER + 900_000
        streams.callbacks["15m"](message(windows["15m"][-1]))
        await drain(strategy, sink, 1)
        signal = sink.attempts[0]
        assert signal.type == "ENTRY"
        assert signal.entry_ref == Decimal("11.200000000000000001")
        decoded = json.loads(next(iter(sink.messages.values())))
        assert decoded["entry_ref"] == "11.200000000000000001"
        assert decoded["valid_until"] == signal.valid_until.isoformat()  # type: ignore[union-attr]
        assert set(decoded) == {"kind", *signal.__dataclass_fields__}
        assert decoded["provenance"]["wave"]["conditions"] == {
            "oversold_exit": True,
            "stochastic_cross": True,
            "force_cross": True,
        }
        assert all(decoded["provenance"]["tide"]["conditions"].values())
        assert signal.epoch_id in strategy._consumed_epochs
        streams.callbacks["15m"](message(windows["15m"][-1]))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(sink.attempts) == 1
        strategy.mark_position_open(SYMBOL)
        next_bar = replace(
            windows["15m"][-1],
            open_time=TRIGGER + 900_000,
            close_time=TRIGGER + 1_799_999,
            close=Decimal(20),
        )
        rest.windows["15m"].append(next_bar)
        clock.now_ms = next_bar.close_time + 1
        streams.callbacks["15m"](message(next_bar))
        await drain(strategy, sink, 2)
        hold = sink.attempts[-1]
        assert hold.type == "HOLD" and hold.reason == "position_open"
        assert hold.epoch_id and hold.provenance["state"]["position_open"]
        tasks = list(strategy._tasks)
        await strategy.on_stop()
        assert all(task.done() for task in tasks)
        streams.callbacks["15m"](message(next_bar))
        assert not strategy._tasks and not strategy._candles
        with pytest.raises(RuntimeError, match="not started"):
            _ = strategy.services

    asyncio.run(run())


@pytest.mark.parametrize("ambiguous", [False, True])
def test_emit_failure_keeps_same_signal_and_epoch(
    caplog: pytest.LogCaptureFixture, ambiguous: bool
) -> None:
    async def run() -> None:
        windows = fixture()
        strategy = TripleScreenStrategy(PARAMS)
        sink = RedisSink()
        sink.fail, sink.accept_before_failure = True, ambiguous
        strategy._services = services(Rest(windows), Streams(), sink)
        strategy._candles[SYMBOL] = windows
        await strategy._on_kline(SYMBOL, "15m", message(windows["15m"][-1]))
        await strategy._evaluate(SYMBOL)
        assert not strategy._consumed_epochs
        assert "sink unavailable" in caplog.text
        original = sink.attempts[0]
        sink.fail = False
        await strategy._evaluate(SYMBOL)
        assert sink.attempts[-1] is original
        assert (
            len(sink.messages) == 1 and original.epoch_id in strategy._consumed_epochs
        )
        await strategy._evaluate(SYMBOL)
        assert len(sink.attempts) == 2

    asyncio.run(run())


def test_permutations_missing_close_and_no_lookahead(clock: ManualClock) -> None:
    async def run(order: tuple[str, ...]) -> dict[str, Any]:
        # Common H4 close: tide must wait for this very bar, wave is pre-trigger.
        trigger_time = TRIGGER - 1_800_000
        clock.now_ms = trigger_time + 900_000
        full = fixture(trigger_time)
        strategy = TripleScreenStrategy(PARAMS)
        sink = RedisSink()
        missing = {i: bars[:-1] for i, bars in full.items()}
        rest = Rest({i: list(bars) for i, bars in missing.items()})
        strategy._services = services(rest, Streams(), sink)
        strategy._candles[SYMBOL] = missing
        for interval in order:
            await strategy._on_kline(SYMBOL, interval, message(full[interval][-1]))
            await strategy._evaluate(SYMBOL)
            if interval != order[-1]:
                assert not sink.attempts
        assert len(sink.attempts) == 1
        signal = sink.attempts[0]
        future = bar(full["4h"][-1].open_time + 14_400_000, "4h", "1")
        strategy._merge(SYMBOL, "4h", [future])
        snapshot = await strategy._snapshot(SYMBOL, full["15m"][-1])
        # Supply the dropped oldest bar through existing REST for the historical slice.
        rest.windows = full
        snapshot = await strategy._snapshot(SYMBOL, full["15m"][-1])
        assert snapshot is not None
        again = strategy._decide(SYMBOL, snapshot)
        assert again is not None
        assert again.provenance["tide"] == signal.provenance["tide"]
        return {
            "type": signal.type,
            "tide": signal.provenance["tide"],
            "wave": signal.provenance["wave"],
            "versions": signal.market_window_versions,
        }

    results = [asyncio.run(run(order)) for order in permutations(("4h", "1h", "15m"))]
    assert all(result == results[0] for result in results)


def test_gap_swing_dedup_and_backfill() -> None:
    async def run() -> None:
        full = fixture()
        for interval in full:
            strategy = TripleScreenStrategy({**PARAMS, "swing_low_h1_n": 12})
            sink = RedisSink()
            incomplete = {i: list(bars) for i, bars in full.items()}
            incomplete[interval].pop(-3)
            rest = Rest(incomplete)
            strategy._services = services(rest, Streams(), sink)
            strategy._candles[SYMBOL] = {
                i: list(bars) for i, bars in incomplete.items()
            }
            for _ in range(2):
                await strategy._on_kline(SYMBOL, "15m", message(full["15m"][-1]))
            await strategy._evaluate(SYMBOL)
            assert not sink.attempts and not strategy._consumed_epochs
            rest.windows = full
            await strategy._evaluate(SYMBOL)
            assert sink.attempts[0].type == "ENTRY"
            assert len(strategy._candles[SYMBOL][interval]) == 12
            before = list(strategy._candles[SYMBOL][interval])
            for c in reversed(full[interval]):
                await strategy._on_kline(SYMBOL, interval, message(c))
            await strategy._evaluate(SYMBOL)
            assert strategy._candles[SYMBOL][interval] == before
            assert len(sink.attempts) == 1

    asyncio.run(run())


def test_bootstrap_full_1000_closed_and_paginated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        trigger_time = 20_000 * 14_400_000

        class Clock(datetime):
            @classmethod
            def now(cls, tz: Any = None) -> Clock:
                return cls.fromtimestamp(trigger_time / 1000, UTC)

        monkeypatch.setattr("agent.strategies.tripple_screen.strategy.datetime", Clock)
        windows = fixture(trigger_time - 900_000, 1000)
        # For bootstrap all three ranges terminate at the same now boundary.
        windows["1h"] = [
            bar(trigger_time - (1000 - i) * 3_600_000, "1h") for i in range(1000)
        ]
        for interval in windows:
            windows[interval].append(bar(trigger_time, interval))
        rest, streams, sink = Rest(windows, page_size=499), Streams(), RedisSink()
        strategy = TripleScreenStrategy({})
        await strategy.on_start(services(rest, streams, sink))
        for interval in windows:
            bars = strategy._candles[SYMBOL][interval]
            assert len(bars) == 1000
            assert all(c.close_time < trigger_time for c in bars)
            assert strategy._slice(bars, interval, trigger_time - 1) == bars
        assert len(rest.calls) >= 9
        await strategy.on_stop()

    asyncio.run(run())


@pytest.mark.parametrize(
    "params",
    [
        {"signal_validity_minutes": -1},
        {"signal_validity_minutes": 0},
        {"ema_tide_periods": [5, 5, 34]},
        {"ema_tide_periods": [5, 13]},
        {"ema_tide_periods": [0, 13, 34]},
        {"kline_window": 10},
        {"stochastic_k_period": 0},
        {"fi_ema_period": -1},
        {"swing_low_h1_n": 0},
        {"wave_interval": "1M"},
        {"ripple_interval": "5m"},
        {"tp_pct": "NaN"},
        {"stop_buffer_pct": 100},
        {"rest_concurrency": 2},
        {"quote_asset": "BTC"},
        {"cooldown_minutes": -1},
    ],
)
def test_invalid_settings(params: dict[str, Any]) -> None:
    with pytest.raises((ValidationError, ValueError)):
        TripleScreenStrategy(params)


def test_settings_immutable_and_detached() -> None:
    params = {"ema_tide_periods": [5, 13, 34]}
    strategy = TripleScreenStrategy(params)
    params["ema_tide_periods"][0] = 1
    strategy.params["ema_tide_periods"][0] = 2
    assert strategy.settings.ema_tide_periods == (5, 13, 34)
    assert strategy.params["ema_tide_periods"] == [5, 13, 34]
    with pytest.raises(ValidationError):
        strategy.settings.signal_validity_minutes = 2
    assert isinstance(strategy.settings.ema_tide_periods, tuple)
    assert TripleScreenSettings(ema_tide_periods=(5, 13, 600)).kline_window == 1000


def test_bootstrap_buffers_callback_and_cancel_awaits_sink(
    clock: ManualClock,
) -> None:
    async def run() -> None:
        windows = fixture()
        streams, sink = Streams(), RedisSink()
        entered, finalized = asyncio.Event(), asyncio.Event()
        strategy = TripleScreenStrategy(PARAMS)

        clock.now_ms = TRIGGER

        class InterleavedRest(Rest):
            async def klines(self, **kwargs: Any) -> list[Any]:
                # Call 0 is stage-1 screening (no streams wired yet); call 1 is
                # stage-2 bootstrap's first fetch, by which point streams must
                # already be wired so a closed bar buffers instead of being lost.
                if len(self.calls) == 1:
                    assert set(streams.callbacks) == {"4h", "1h", "15m"}
                    clock.now_ms = TRIGGER + 900_000
                    streams.callbacks["15m"](message(windows["15m"][-1]))
                    await asyncio.sleep(0)
                return await super().klines(**kwargs)

        async def blocked_emit(signal: Signal) -> None:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                finalized.set()

        svc = services(InterleavedRest(windows), streams, sink)
        svc.emit = blocked_emit
        await strategy.on_start(svc)
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert len(strategy._candles[SYMBOL]["15m"]) == 12
        tasks = list(strategy._tasks)
        await strategy.on_stop()
        assert finalized.is_set() and all(task.done() for task in tasks)
        assert not strategy._consumed_epochs

    asyncio.run(run())


def test_start_failure_gates_late_callbacks() -> None:
    async def run() -> None:
        class BrokenRest(Rest):
            async def klines(self, **kwargs: Any) -> list[Any]:
                # Call 0 (stage-1 screening) succeeds so the symbol qualifies
                # and streams get wired; bootstrap's first call then fails.
                if self.calls:
                    raise RuntimeError("bootstrap failed")
                return await super().klines(**kwargs)

        streams = Streams()
        strategy = TripleScreenStrategy(PARAMS)
        with pytest.raises(RuntimeError, match="bootstrap failed"):
            await strategy.on_start(
                services(BrokenRest(fixture()), streams, RedisSink())
            )
        streams.callbacks["15m"](message(fixture()["15m"][-1]))
        assert not strategy._tasks and not strategy._active
        assert strategy._services is None

    asyncio.run(run())


def test_nonbullish_common_boundary_never_uses_previous_tide(
    clock: ManualClock,
) -> None:
    async def run(order: tuple[str, ...]) -> None:
        clock.now_ms = TRIGGER - 900_000
        full = fixture(TRIGGER - 1_800_000)
        full["4h"][-1] = replace(full["4h"][-1], close=Decimal(1))
        strategy = TripleScreenStrategy(PARAMS)
        sink = RedisSink()
        strategy._candles[SYMBOL] = {i: bars[:-1] for i, bars in full.items()}
        strategy._services = services(
            Rest({i: bars[:-1] for i, bars in full.items()}), Streams(), sink
        )
        for interval in order:
            await strategy._on_kline(SYMBOL, interval, message(full[interval][-1]))
            await strategy._evaluate(SYMBOL)
            assert not sink.attempts
        assert strategy._evaluated[SYMBOL] == full["15m"][-1].open_time

    for order in permutations(("4h", "1h", "15m")):
        asyncio.run(run(order))


def test_worker_retries_failed_emit_without_new_callback() -> None:
    async def run() -> None:
        windows = fixture()
        strategy = TripleScreenStrategy(PARAMS)
        sink, streams = RedisSink(), Streams()
        sink.fail = True
        await strategy.on_start(services(Rest(windows), streams, sink))
        try:
            streams.callbacks["15m"](message(windows["15m"][-1]))
            for _ in range(100):
                if sink.attempts:
                    break
                await asyncio.sleep(0)
            assert len(sink.attempts) == 1 and not strategy._consumed_epochs
            sink.fail = False
            await asyncio.sleep(1.1)
            assert len(sink.messages) == 1
            assert sink.attempts[0] is sink.attempts[1]
        finally:
            await strategy.on_stop()

    asyncio.run(run())


def test_cooldown_epoch_and_no_trigger_provenance() -> None:
    strategy = TripleScreenStrategy(PARAMS)
    windows = fixture()
    first = strategy._decide(SYMBOL, windows)
    assert first is not None and first.type == "ENTRY"
    assert not strategy._consumed_epochs  # Decision alone is not delivery.
    strategy.mark_position_closed(SYMBOL)
    cooling = strategy._decide(SYMBOL, windows)
    assert cooling is not None and cooling.reason == "cooldown"
    assert cooling.provenance["state"]["cooldown_active"]
    strategy._cooldown_until.clear()
    assert first.epoch_id is not None
    strategy._consumed_epochs.add(first.epoch_id)
    consumed = strategy._decide(SYMBOL, windows)
    assert consumed is not None and consumed.reason == "epoch_consumed"
    assert consumed.epoch_id == first.epoch_id
    windows["15m"][-1] = replace(windows["15m"][-1], close=Decimal(11))
    no_trigger = strategy._decide(SYMBOL, windows)
    assert no_trigger is not None and no_trigger.reason == "no_trigger"
    assert not no_trigger.provenance["ripple"]["entry_trigger"]
    windows["1h"][-1] = replace(windows["1h"][-1], close=Decimal(9))
    no_pullback = strategy._decide(SYMBOL, windows)
    assert no_pullback is not None and no_pullback.reason == "no_pullback"


@pytest.mark.parametrize("validity_minutes", [1, 15, 30])
def test_trigger_freshness_boundaries_and_signal_validity(
    clock: ManualClock, validity_minutes: int
) -> None:
    strategy = TripleScreenStrategy(
        {**PARAMS, "signal_validity_minutes": validity_minutes}
    )
    windows = fixture()
    close_time = windows["15m"][-1].close_time
    deadline = close_time + min(900_000, validity_minutes * 60_000)
    clock.now_ms = close_time - 1
    assert strategy._decide(SYMBOL, windows) is None
    for now_ms in (close_time, deadline - 1):
        clock.now_ms = now_ms
        signal = strategy._decide(SYMBOL, windows)
        assert signal is not None and signal.type == "ENTRY"
        assert signal.emitted_at == datetime.fromtimestamp(now_ms / 1000, UTC)
        assert signal.valid_until is not None
        assert (
            signal.valid_until - signal.emitted_at
        ).total_seconds() == validity_minutes * 60
    for now_ms in (deadline, deadline + 86_400_000):
        clock.now_ms = now_ms
        assert strategy._decide(SYMBOL, windows) is None
    assert not strategy._consumed_epochs


async def wait_evaluated(strategy: TripleScreenStrategy, open_time: int) -> None:
    for _ in range(200):
        if strategy._evaluated.get(SYMBOL, -1) >= open_time:
            return
        await asyncio.sleep(0)
    raise AssertionError("trigger was not finalized")


def test_runtime_delayed_callback_skips_without_backfill(
    clock: ManualClock, caplog: pytest.LogCaptureFixture
) -> None:
    async def run() -> None:
        windows = fixture()
        rest, streams, sink = Rest(windows), Streams(), RedisSink()
        strategy = TripleScreenStrategy(PARAMS)
        await strategy.on_start(services(rest, streams, sink))
        try:
            calls = len(rest.calls)
            clock.now_ms += 86_400_000
            trigger = windows["15m"][-1]
            streams.callbacks["15m"](message(trigger))
            await wait_evaluated(strategy, trigger.open_time)
            assert "reason=stale_trigger" in caplog.text
            assert len(rest.calls) == calls
            assert not sink.attempts and not strategy._consumed_epochs
            assert not strategy._pending[SYMBOL] and not strategy._outbox
            streams.callbacks["15m"](message(trigger))
            await asyncio.sleep(0)
            assert not strategy._pending[SYMBOL]
        finally:
            await strategy.on_stop()

    asyncio.run(run())


@pytest.mark.parametrize("complete", [False, True])
def test_runtime_backfill_crosses_freshness_deadline(
    clock: ManualClock, caplog: pytest.LogCaptureFixture, complete: bool
) -> None:
    async def run() -> None:
        windows = fixture()
        entered, release = asyncio.Event(), asyncio.Event()

        class DelayedRest(Rest):
            armed = False

            async def klines(self, **kwargs: Any) -> list[Any]:
                if self.armed:
                    entered.set()
                    await release.wait()
                return await super().klines(**kwargs)

        incomplete = {i: list(bars) for i, bars in windows.items()}
        incomplete["1h"].pop(-3)
        rest, streams, sink = DelayedRest(incomplete), Streams(), RedisSink()
        strategy = TripleScreenStrategy(PARAMS)
        await strategy.on_start(services(rest, streams, sink))
        try:
            rest.armed = True
            if complete:
                rest.windows = windows
            trigger = windows["15m"][-1]
            streams.callbacks["15m"](message(trigger))
            await asyncio.wait_for(entered.wait(), timeout=2)
            clock.now_ms = trigger.close_time + 900_000
            release.set()
            await wait_evaluated(strategy, trigger.open_time)
            assert "reason=stale_trigger" in caplog.text
            assert not sink.attempts and not strategy._consumed_epochs
            assert not strategy._outbox and not strategy._pending[SYMBOL]

            # Dropping an old trigger must not block the next fresh epoch.
            next_bar = bar(TRIGGER + 900_000, "15m", "20")
            rest.windows = windows
            rest.windows["15m"].append(next_bar)
            clock.now_ms = next_bar.close_time + 1
            streams.callbacks["15m"](message(next_bar))
            await drain(strategy, sink, 1)
            assert sink.attempts[0].type == "ENTRY"
            assert strategy._consumed_epochs == {f"{SYMBOL}:{next_bar.open_time}"}
        finally:
            await strategy.on_stop()

    asyncio.run(run())


@pytest.mark.parametrize(
    "validity_minutes,expiry", [(1, "valid_until"), (30, "next_close")]
)
@pytest.mark.parametrize("ambiguous", [False, True])
def test_runtime_expired_retry_is_dropped_without_renewal(
    clock: ManualClock,
    caplog: pytest.LogCaptureFixture,
    validity_minutes: int,
    expiry: str,
    ambiguous: bool,
) -> None:
    async def run() -> None:
        windows = fixture()
        strategy = TripleScreenStrategy(
            {**PARAMS, "signal_validity_minutes": validity_minutes}
        )
        sink, streams = RedisSink(), Streams()
        sink.fail, sink.accept_before_failure = True, ambiguous
        await strategy.on_start(services(Rest(windows), streams, sink))
        try:
            trigger = windows["15m"][-1]
            # Emit exactly at close so the short validity and freshness bounds coincide.
            clock.now_ms = trigger.close_time
            streams.callbacks["15m"](message(trigger))
            for _ in range(200):
                if strategy._outbox:
                    break
                await asyncio.sleep(0)
            assert len(sink.attempts) == 1
            original = sink.attempts[0]
            original_payload = original.transport()
            assert original.valid_until is not None
            if expiry == "valid_until":
                clock.now_ms = trigger.close_time + validity_minutes * 60_000
                assert (
                    datetime.fromtimestamp(clock.now_ms / 1000, UTC)
                    == original.valid_until
                )
            else:
                clock.now_ms = trigger.close_time + 900_000
                assert (
                    datetime.fromtimestamp(clock.now_ms / 1000, UTC)
                    < original.valid_until
                )
            sink.fail = False
            # Wake the actual worker's retry path, without a new market bar.
            strategy._wake.set()
            await wait_evaluated(strategy, trigger.open_time)
            assert len(sink.attempts) == 1
            assert original.transport() == original_payload
            assert not strategy._consumed_epochs and not strategy._outbox
            assert not strategy._pending[SYMBOL]
            assert len(sink.messages) == int(ambiguous)
            reason = "expired_entry" if expiry == "valid_until" else "stale_trigger"
            assert f"reason={reason}" in caplog.text
            streams.callbacks["15m"](message(trigger))
            await asyncio.sleep(0)
            assert not strategy._pending[SYMBOL]
        finally:
            await strategy.on_stop()

    asyncio.run(run())


def test_runtime_rechecks_freshness_immediately_before_emit(
    clock: ManualClock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def run() -> None:
        windows = fixture()
        trigger = windows["15m"][-1]
        strategy = TripleScreenStrategy(PARAMS)
        streams, sink = Streams(), RedisSink()
        decide = strategy._decide

        def slow_decide(
            symbol: str, snapshot: dict[str, list[Candle]] | None = None
        ) -> Signal | None:
            signal = decide(symbol, snapshot)
            assert signal is not None and signal.type == "ENTRY"
            clock.now_ms = trigger.close_time + 900_000
            return signal

        monkeypatch.setattr(strategy, "_decide", slow_decide)
        await strategy.on_start(services(Rest(windows), streams, sink))
        try:
            streams.callbacks["15m"](message(trigger))
            await wait_evaluated(strategy, trigger.open_time)
            assert "reason=stale_trigger" in caplog.text
            assert not sink.attempts and not strategy._consumed_epochs
            assert not strategy._outbox and not strategy._pending[SYMBOL]
        finally:
            await strategy.on_stop()

    asyncio.run(run())


def test_transport_all_types_nested_precision() -> None:
    now = datetime.now(UTC)
    for kind in ("ENTRY", "EXIT", "HOLD"):
        signal = Signal(
            type=cast(Any, kind),
            symbol=SYMBOL,
            emitted_at=now,
            provenance={"nested": [Decimal("0.0000000000000000001"), now]},
        )
        payload = json.loads(json.dumps(signal.transport()))
        assert payload["provenance"]["nested"] == ["1E-19", now.isoformat()]
        assert payload["entry_ref"] is None and payload["side"] == "LONG"
        assert set(payload) == set(signal.__dataclass_fields__)


# --- Stage-1 screening / stage-2 selection --------------------------------

SCREEN_PARAMS: dict[str, Any] = {**PARAMS, "ws_top_n": 30}


def sloped_4h(
    windows: dict[str, list[Candle]], step_size: int
) -> dict[str, list[Candle]]:
    """Same shape as ``fixture()`` but with a 4h close series that changes by
    ``step_size`` per bar (positive: rising, negative: falling), so EMA13/
    EMA34 (periods 3/4 under ``PARAMS``) rise or fall accordingly, and a
    steeper slope yields a larger EMA13-over-EMA34 spread."""
    windows["4h"] = [
        replace(c, close=Decimal(100 + step_size * i))
        for i, c in enumerate(windows["4h"])
    ]
    return windows


def falling_4h(windows: dict[str, list[Candle]]) -> dict[str, list[Candle]]:
    return sloped_4h(windows, -1)


class MultiRest:
    """Per-symbol REST fake for multi-symbol screening/rescreen tests."""

    def __init__(self, windows: dict[str, dict[str, list[Candle]]]) -> None:
        self.windows = windows
        self.calls: list[dict[str, Any]] = []

    async def klines(self, **kwargs: Any) -> list[Any]:
        self.calls.append(kwargs)
        await asyncio.sleep(0)
        bars = [
            c
            for c in self.windows[kwargs["symbol"]][kwargs["interval"]]
            if kwargs["start_time"] <= c.open_time <= kwargs["end_time"]
        ]
        return [
            [
                c.open_time,
                str(c.open),
                str(c.high),
                str(c.low),
                str(c.close),
                str(c.volume),
                c.close_time,
            ]
            for c in bars[: kwargs["limit"]]
        ]


class MultiStreams:
    def __init__(self) -> None:
        self.subscribed: set[tuple[str, str]] = set()
        self.unsubscribed: list[tuple[str, str]] = []

    async def subscribe_kline(
        self, *, symbol: str, interval: str, callback: Callable[[KlineResponse], None]
    ) -> None:
        self.subscribed.add((symbol, interval))

    async def unsubscribe_kline(self, *, symbol: str, interval: str) -> None:
        self.subscribed.discard((symbol, interval))
        self.unsubscribed.append((symbol, interval))


def multi_services(
    rest: MultiRest, streams: MultiStreams, sink: RedisSink, symbols: list[str]
) -> StrategyServices:
    return StrategyServices(
        "testnet",
        symbols,
        cast(ExchangeRest, rest),
        cast(MarketDataStreams, streams),
        sink.emit,
    )


def test_screening_admits_by_cap_and_trend_strength() -> None:
    async def run() -> None:
        # STRONG and WEAK both qualify (rising EMAs); WEAK's slope (and thus
        # EMA13-over-EMA34 spread) is much shallower. DOWN never qualifies.
        windows = {
            "STRONG": sloped_4h(fixture(), 5),
            "WEAK": sloped_4h(fixture(), 1),
            "DOWN": falling_4h(fixture()),
        }
        rest = MultiRest(windows)
        streams, sink = MultiStreams(), RedisSink()
        strategy = TripleScreenStrategy({**SCREEN_PARAMS, "ws_top_n": 1})
        await strategy.on_start(
            multi_services(rest, streams, sink, ["STRONG", "WEAK", "DOWN"])
        )
        try:
            assert strategy._active_symbols == {"STRONG"}
            assert ("STRONG", "4h") in streams.subscribed
            assert not any(sym == "WEAK" for sym, _ in streams.subscribed)
            assert not any(sym == "DOWN" for sym, _ in streams.subscribed)
        finally:
            await strategy.on_stop()

    asyncio.run(run())


def test_open_position_retained_past_failed_rescreen() -> None:
    async def run() -> None:
        windows = {"BTCUSDT": fixture(), "ETHUSDT": falling_4h(fixture())}
        rest = MultiRest(windows)
        streams, sink = MultiStreams(), RedisSink()
        strategy = TripleScreenStrategy(SCREEN_PARAMS)
        await strategy.on_start(
            multi_services(rest, streams, sink, ["BTCUSDT", "ETHUSDT"])
        )
        try:
            assert strategy._active_symbols == {"BTCUSDT"}
            strategy.mark_position_open("BTCUSDT")
            # BTCUSDT now fails the screen; it must still be kept.
            rest.windows["BTCUSDT"] = falling_4h(fixture())
            selected = await strategy._screen_universe()
            assert selected == {"BTCUSDT"}
            await strategy._apply_selection(selected)
            assert strategy._active_symbols == {"BTCUSDT"}
            assert ("BTCUSDT", "4h") not in streams.unsubscribed
        finally:
            await strategy.on_stop()

    asyncio.run(run())


def test_rescreen_adds_and_removes_symbols() -> None:
    async def run() -> None:
        windows = {"BTCUSDT": fixture(), "ETHUSDT": falling_4h(fixture())}
        rest = MultiRest(windows)
        streams, sink = MultiStreams(), RedisSink()
        strategy = TripleScreenStrategy(SCREEN_PARAMS)
        await strategy.on_start(
            multi_services(rest, streams, sink, ["BTCUSDT", "ETHUSDT"])
        )
        try:
            assert strategy._active_symbols == {"BTCUSDT"}
            assert strategy._candles["BTCUSDT"]
            # Flip: BTCUSDT now falls, ETHUSDT now rises.
            rest.windows["BTCUSDT"] = falling_4h(fixture())
            rest.windows["ETHUSDT"] = fixture()
            selected = await strategy._screen_universe()
            assert selected == {"ETHUSDT"}
            await strategy._apply_selection(selected)
            assert strategy._active_symbols == {"ETHUSDT"}
            assert "BTCUSDT" not in strategy._candles
            assert ("BTCUSDT", "4h") in streams.unsubscribed
            assert ("ETHUSDT", "4h") in streams.subscribed
        finally:
            await strategy.on_stop()

    asyncio.run(run())


def test_next_rescreen_ms_is_tide_boundary_plus_buffer() -> None:
    strategy = TripleScreenStrategy(PARAMS)
    step = interval_ms("4h")
    now_ms = step * 10 + 1_000
    assert strategy._next_rescreen_ms(now_ms) == step * 11 + 5_000
    # Exactly on a boundary still waits for the *next* one, not this instant.
    assert strategy._next_rescreen_ms(step * 10) == step * 11 + 5_000
