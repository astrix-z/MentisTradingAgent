"""Closed-candle Triple Screen core; execution/universe/protection live elsewhere."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from binance_sdk_spot.websocket_streams.models import KlineResponse

from agent.strategies.base import Signal, Strategy, StrategyServices, transport_value
from agent.strategies.registry import strategy
from agent.strategies.tripple_screen.indicators import (
    Candle,
    ema_series,
    force_index_series,
    sma_of_optional,
    stochastic_k_series,
)
from agent.strategies.tripple_screen.schema import CONSTRAINTS, LOOKUPS
from agent.strategies.tripple_screen.settings import TripleScreenSettings, interval_ms

logger = logging.getLogger(__name__)


@strategy("tripple_screen")
class TripleScreenStrategy(Strategy):
    label = "Triple Screen"
    settings_model = TripleScreenSettings
    settings_constraints = CONSTRAINTS
    settings_lookups = LOOKUPS

    def __init__(self, params: dict[str, Any]) -> None:
        super().__init__(params)
        self._settings = TripleScreenSettings.from_params(params)
        self._config_version = hashlib.sha256(
            self._settings.model_dump_json().encode()
        ).hexdigest()
        self._candles: dict[str, dict[str, list[Candle]]] = {}
        self._last_price: dict[str, Decimal] = {}
        self._positions: set[str] = set()
        self._cooldown_until: dict[str, datetime] = {}
        self._consumed_epochs: set[str] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._pending: dict[str, dict[int, Candle]] = {}
        self._outbox: dict[str, Signal] = {}
        self._evaluated: dict[str, int] = {}
        self._wake = asyncio.Event()
        self._active = False

    @property
    def settings(self) -> TripleScreenSettings:
        return self._settings

    def _intervals(self) -> tuple[str, str, str]:
        return (
            self.settings.tide_interval,
            self.settings.wave_interval,
            self.settings.ripple_interval,
        )

    async def on_start(self, services: StrategyServices) -> None:
        if self._active:
            raise RuntimeError("strategy is already started")
        self._services = services
        self._active = True
        try:
            # Subscribe first: callbacks buffer closes while REST is in flight.
            await self._wire_streams(services)
            await self._bootstrap(services)
            self._spawn(self._run())
        except BaseException:
            await self.on_stop()
            raise

    async def _wire_streams(self, services: StrategyServices) -> None:
        for symbol in services.symbols:
            for interval in self._intervals():
                await services.streams.subscribe_kline(
                    symbol=symbol,
                    interval=interval,
                    callback=self._kline_callback(symbol, interval),
                )

    async def _bootstrap(self, services: StrategyServices) -> None:
        cutoff = int(datetime.now(UTC).timestamp() * 1000) - 1
        for symbol in services.symbols:
            for interval in self._intervals():
                candles = await self._load_window(symbol, interval, cutoff)
                self._merge(symbol, interval, candles)

    async def _load_window(
        self, symbol: str, interval: str, cutoff: int
    ) -> list[Candle]:
        """Fetch exactly the configured closed UTC range, paginating SDK REST.

        end_time is inclusive; excluding the forming bar before pagination
        prevents it from taking a slot in the configured closed-bar window.
        """
        step = interval_ms(interval)
        end = (cutoff + 1) // step * step - 1
        start = end + 1 - self.settings.kline_window * step
        cursor = max(0, start)
        found: dict[int, Candle] = {}
        while cursor <= end:
            rows = await self.services.rest.klines(
                symbol=symbol,
                interval=interval,
                start_time=cursor,
                end_time=end,
                limit=min(1000, (end - cursor + 1) // step),
            )
            page = [Candle.from_rest_row(row) for row in rows]
            page = [c for c in page if cursor <= c.open_time and c.close_time <= end]
            if not page:
                break
            found.update((c.open_time, c) for c in page)
            cursor = max(c.open_time for c in page) + step
        return [found[key] for key in sorted(found) if key >= start]

    def _merge(self, symbol: str, interval: str, candles: list[Candle]) -> None:
        windows = self._candles.setdefault(symbol, {})
        # A closed bar is immutable: replay cannot revise an already used bar.
        merged = {c.open_time: c for c in candles if c.closed}
        merged.update((c.open_time, c) for c in windows.get(interval, []))
        windows[interval] = [merged[key] for key in sorted(merged)][
            -self.settings.kline_window :
        ]

    def _spawn(self, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error("tripple_screen background task failed", exc_info=error)

    def _kline_callback(
        self, symbol: str, interval: str
    ) -> Callable[[KlineResponse], None]:
        def callback(data: KlineResponse) -> None:
            if self._active:
                self._spawn(self._on_kline(symbol, interval, data))

        return callback

    async def on_stop(self) -> None:
        self._active = False
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._services = None
        self._candles.clear()
        self._last_price.clear()
        self._pending.clear()
        self._outbox.clear()
        self._wake.clear()
        # Streams belong to Engine; late callbacks are gated by _active.

    def mark_position_open(self, symbol: str) -> None:
        self._positions.add(symbol.upper())

    def mark_position_closed(self, symbol: str, at: datetime | None = None) -> None:
        symbol = symbol.upper()
        self._positions.discard(symbol)
        self._cooldown_until[symbol] = (at or datetime.now(UTC)) + timedelta(
            minutes=self.settings.cooldown_minutes
        )

    async def _on_kline(self, symbol: str, interval: str, data: KlineResponse) -> None:
        if data.k is None:
            return
        candle = Candle.from_ws(data.k)
        self._last_price[symbol] = candle.close
        if not candle.closed:
            return
        self._merge(symbol, interval, [candle])
        if (
            interval == self.settings.ripple_interval
            and candle.open_time > self._evaluated.get(symbol, -1)
        ):
            self._pending.setdefault(symbol, {}).setdefault(candle.open_time, candle)
        self._wake.set()

    async def _run(self) -> None:
        while self._active:
            self._wake.clear()
            for symbol in list(self._pending):
                await self._evaluate(symbol)
            if any(self._pending.values()):
                # Also retry absent closes and failed emits without another WS event.
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=1)
                except TimeoutError:
                    pass
            else:
                await self._wake.wait()

    def _slice(
        self, candles: list[Candle], interval: str, cutoff: int
    ) -> list[Candle] | None:
        step = interval_ms(interval)
        end = (cutoff + 1) // step * step - 1
        start = end + 1 - self.settings.kline_window * step
        selected = [
            c
            for c in candles
            if start <= c.open_time and c.close_time <= end and c.closed
        ]
        if len(selected) != self.settings.kline_window:
            return None
        if any(
            c.open_time != start + i * step or c.close_time != c.open_time + step - 1
            for i, c in enumerate(selected)
        ):
            return None
        return selected

    async def _snapshot(
        self, symbol: str, trigger: Candle
    ) -> dict[str, list[Candle]] | None:
        snapshot: dict[str, list[Candle]] = {}
        for interval in self._intervals():
            cutoff = (
                trigger.open_time
                if interval == self.settings.wave_interval
                else trigger.close_time
            )
            candles = self._candles.get(symbol, {}).get(interval, [])
            selected = self._slice(candles, interval, cutoff)
            if selected is None:
                loaded = await self._load_window(symbol, interval, cutoff)
                by_time = {c.open_time: c for c in loaded}
                by_time.update(
                    (c.open_time, c)
                    for c in self._candles.get(symbol, {}).get(interval, [])
                )
                candles = [by_time[key] for key in sorted(by_time)]
                self._merge(symbol, interval, loaded)
                selected = self._slice(candles, interval, cutoff)
            if selected is None:
                logger.warning(
                    "tripple_screen history not ready symbol=%s interval=%s trigger=%s",
                    symbol,
                    interval,
                    trigger.open_time,
                )
                return None
            snapshot[interval] = selected
        return snapshot

    def _trigger_deadline(self, trigger: Candle) -> datetime:
        """Exclusive freshness bound, independent of delivery/backfill time.

        Spec §3.4/§5 require the latest closed ripple, not a complete historical
        snapshot. Stop admitting it at the next ripple close, or earlier when
        configured validity is shorter. Signal.valid_until still follows §4.2
        (emitted_at + validity); neither deadline is renewed by retry.
        """
        return datetime.fromtimestamp(trigger.close_time / 1000, UTC) + min(
            timedelta(milliseconds=interval_ms(self.settings.ripple_interval)),
            timedelta(minutes=self.settings.signal_validity_minutes),
        )

    def _skip_expired(
        self, symbol: str, trigger: Candle, signal: Signal | None = None
    ) -> bool:
        now = datetime.now(UTC)
        reason = None
        if (
            signal is not None
            and signal.type == "ENTRY"
            and (signal.valid_until is None or now >= signal.valid_until)
        ):
            reason = "expired_entry"
        elif now >= self._trigger_deadline(trigger):
            reason = "stale_trigger"
        if reason is None:
            return False
        epoch = f"{symbol}:{trigger.open_time}"
        logger.warning(
            "tripple_screen skipped reason=%s symbol=%s epoch=%s signal_id=%s",
            reason,
            symbol,
            epoch,
            signal.signal_id if signal else None,
        )
        self._pending.get(symbol, {}).pop(trigger.open_time, None)
        self._outbox.pop(epoch, None)
        # Terminal evaluation, NOT a delivered ENTRY. Replays must not revive it.
        self._evaluated[symbol] = max(
            self._evaluated.get(symbol, -1), trigger.open_time
        )
        return True

    async def _evaluate(self, symbol: str) -> None:
        pending = self._pending.get(symbol, {})
        for open_time in sorted(pending):
            if open_time <= self._evaluated.get(symbol, -1):
                pending.pop(open_time)
                continue
            trigger = pending[open_time]
            epoch = f"{symbol}:{open_time}"
            try:
                signal = self._outbox.get(epoch)
                if self._skip_expired(symbol, trigger, signal):
                    continue
                if datetime.now(UTC).timestamp() * 1000 < trigger.close_time:
                    return
                if signal is None:
                    snapshot = await self._snapshot(symbol, trigger)
                    if self._skip_expired(symbol, trigger):
                        continue
                    if snapshot is None:
                        return
                    signal = self._decide(symbol, snapshot)
                    if signal is not None:
                        self._outbox[epoch] = signal
                if self._skip_expired(symbol, trigger, signal):
                    continue
                if signal is not None:
                    await self.services.emit(signal)
                    if signal.type == "ENTRY":
                        self._consumed_epochs.add(epoch)
                    self._outbox.pop(epoch, None)
                self._evaluated[symbol] = open_time
                pending.pop(open_time)
            except Exception:
                logger.exception(
                    "tripple_screen evaluation/emit failed symbol=%s epoch=%s",
                    symbol,
                    epoch,
                )
                return

    def _decide(
        self, symbol: str, windows: dict[str, list[Candle]] | None = None
    ) -> Signal | None:
        settings = self.settings
        windows = windows if windows is not None else self._candles.get(symbol, {})
        ripple = windows.get(settings.ripple_interval, [])
        if not ripple:
            return None
        trigger = ripple[-1]
        now = datetime.now(UTC)
        if now.timestamp() * 1000 < trigger.close_time or now >= self._trigger_deadline(
            trigger
        ):
            return None
        snapshot: dict[str, list[Candle]] = {}
        for interval in self._intervals():
            cutoff = (
                trigger.open_time
                if interval == settings.wave_interval
                else trigger.close_time
            )
            selected = self._slice(windows.get(interval, []), interval, cutoff)
            if selected is None:
                return None
            snapshot[interval] = selected
        tide = self._tide(snapshot[settings.tide_interval])
        logger.debug(
            "tripple_screen tide symbol=%s ripple_open_time=%s bullish=%s "
            "classification=%s conditions=%s ema=%s",
            symbol,
            trigger.open_time,
            tide["bullish_tide"],
            tide["classification"],
            tide["conditions"],
            tide["ema"],
        )
        if not tide["bullish_tide"]:
            return None
        wave_candles = snapshot[settings.wave_interval]
        wave = self._wave(wave_candles)
        previous = ripple[-2]
        entry_trigger = trigger.close > previous.high
        now = datetime.now(UTC)
        if now >= self._trigger_deadline(trigger):
            return None
        epoch = f"{symbol}:{trigger.open_time}"
        cooldown = self._cooldown_until.get(symbol)
        cooling = cooldown is not None and now < cooldown
        swing = min(c.low for c in wave_candles[-settings.swing_low_h1_n :]) * (
            1 - settings.stop_buffer_pct / 100
        )
        provenance = transport_value(
            {
                "tide": tide,
                "wave": wave,
                "ripple": {
                    "open_time": trigger.open_time,
                    "close_time": trigger.close_time,
                    "close": trigger.close,
                    "previous_high": previous.high,
                    "entry_trigger": entry_trigger,
                },
                "state": {
                    "position_open": symbol in self._positions,
                    "cooldown_until": cooldown,
                    "cooldown_active": cooling,
                    "cooldown_remaining_seconds": max(
                        0.0, (cooldown - now).total_seconds()
                    )
                    if cooldown
                    else 0.0,
                    "epoch_id": epoch,
                    "epoch_consumed": epoch in self._consumed_epochs,
                },
                "swing_low_ref": swing,
                "tp_pct": settings.tp_pct,
                "stop_buffer_pct": settings.stop_buffer_pct,
                "min_stop_distance_pct": settings.min_stop_distance_pct,
            }
        )
        reason = next(
            (
                reason
                for blocked, reason in (
                    (not wave["pullback_up"], "no_pullback"),
                    (not entry_trigger, "no_trigger"),
                    (symbol in self._positions, "position_open"),
                    (cooling, "cooldown"),
                    (epoch in self._consumed_epochs, "epoch_consumed"),
                )
                if blocked
            ),
            None,
        )
        versions = {
            interval: hashlib.sha256(repr(candles).encode()).hexdigest()
            for interval, candles in snapshot.items()
        }
        logger.debug(
            "tripple_screen decision symbol=%s epoch=%s type=%s reason=%s wave=%s "
            "ripple=%s state=%s",
            symbol,
            epoch,
            "HOLD" if reason else "ENTRY",
            reason or "entry",
            wave,
            provenance["ripple"],
            provenance["state"],
        )
        return Signal(
            type="HOLD" if reason else "ENTRY",
            symbol=symbol,
            emitted_at=now,
            reason=reason or "entry",
            epoch_id=epoch,
            entry_ref=None if reason else trigger.close,
            swing_low_ref=None if reason else swing,
            tp_rule=None if reason else f"fill x {1 + settings.tp_pct / 100}",
            valid_until=None
            if reason
            else now + timedelta(minutes=settings.signal_validity_minutes),
            provenance=provenance,
            config_version=self._config_version,
            market_window_versions=versions,
        )

    def _tide(self, candles: list[Candle]) -> dict[str, Any]:
        periods = self.settings.ema_tide_periods
        emas = [ema_series([c.close for c in candles], p) for p in periods]
        if len(candles) < max(periods) + 1:
            return {"bullish_tide": None}
        pairs: list[tuple[Decimal, Decimal]] = []
        for series in emas:
            previous, current = series[-2:]
            assert previous is not None and current is not None
            pairs.append((previous, current))
        comparisons = [(current, previous) for previous, current in pairs]
        comparisons += [(pairs[0][1], pairs[1][1]), (candles[-1].close, pairs[2][1])]
        flags = [left > right for left, right in comparisons]
        bearish = all(left < right for left, right in comparisons)
        return {
            "open_time": candles[-1].open_time,
            "close_time": candles[-1].close_time,
            "close": candles[-1].close,
            "ema": {
                str(p): {"previous": pair[0], "current": pair[1]}
                for p, pair in zip(periods, pairs, strict=True)
            },
            "conditions": dict(
                zip(
                    (
                        "fast_rising",
                        "mid_rising",
                        "slow_rising",
                        "fast_above_mid",
                        "close_above_slow",
                    ),
                    flags,
                    strict=True,
                )
            ),
            "bullish_tide": all(flags),
            "classification": "bullish"
            if all(flags)
            else "bearish"
            if bearish
            else "neutral",
        }

    def _bullish_tide(self, symbol: str) -> bool | None:
        value: bool | None = self._tide(
            self._candles.get(symbol, {}).get(self.settings.tide_interval, [])
        )["bullish_tide"]
        return value

    def _wave(self, candles: list[Candle]) -> dict[str, Any]:
        s = self.settings
        k = stochastic_k_series(candles, s.stochastic_k_period)
        d = sma_of_optional(k, s.stochastic_d_period)
        slow = sma_of_optional(d, s.stochastic_slow_period)
        fi = force_index_series(candles, s.fi_ema_period)
        kp, kt, dp, dt, fp, ft = k[-2], k[-1], d[-2], d[-1], fi[-2], fi[-1]
        flags = {
            "oversold_exit": kp is not None
            and kt is not None
            and kp < s.oversold_threshold <= kt,
            "stochastic_cross": kp is not None
            and kt is not None
            and dp is not None
            and dt is not None
            and kp <= dp
            and kt > dt,
            "force_cross": fp is not None and ft is not None and fp < 0 <= ft,
        }
        return {
            "open_time": candles[-1].open_time,
            "close_time": candles[-1].close_time,
            "k": {"previous": kp, "current": kt},
            "d": {"previous": dp, "current": dt},
            "slow_d": {"previous": slow[-2], "current": slow[-1]},
            "fi": {"previous": fp, "current": ft},
            "oversold_threshold": s.oversold_threshold,
            "conditions": flags,
            "pullback_up": all(flags.values()),
        }
