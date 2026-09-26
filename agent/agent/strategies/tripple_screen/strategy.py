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

from agent.common.intervals import bar_open, interval_ms
from agent.strategies.base import (
    EXIT_POLICY_VERSION,
    ExitPolicy,
    PositionEvent,
    PositionsSnapshot,
    Signal,
    Strategy,
    StrategyServices,
    transport_value,
)
from agent.strategies.registry import strategy
from agent.strategies.tripple_screen.indicators import (
    Candle,
    ema_series,
    force_index_series,
    sma_of_optional,
    stochastic_k_series,
)
from agent.strategies.tripple_screen.schema import CONSTRAINTS, LOOKUPS
from agent.strategies.tripple_screen.settings import (
    LEGACY_KEYS,
    TripleScreenSettings,
)

logger = logging.getLogger(__name__)

# Grace period after a tide-interval boundary before re-screening, so the
# exchange's own bar (and its REST availability) has had time to close.
_RESCREEN_BUFFER_MS = 5_000


@strategy("tripple_screen")
class TripleScreenStrategy(Strategy):
    label = "Triple Screen"
    settings_model = TripleScreenSettings
    settings_constraints = CONSTRAINTS
    settings_lookups = LOOKUPS
    settings_legacy_keys = LEGACY_KEYS

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
        # Stage 1: the full symbol list handed to on_start, re-screened every
        # tide-interval boundary. Stage 2: the subset currently wired/bootstrapped.
        self._universe: list[str] = []
        self._active_symbols: set[str] = set()
        # Consecutive failed rescreen cycles, surfaced via health_warning.
        self._rescreen_failures = 0
        self._rescreen_failing_since: datetime | None = None
        self._rescreen_error = ""

    @property
    def settings(self) -> TripleScreenSettings:
        return self._settings

    @property
    def active_symbols(self) -> list[str] | None:
        """Symbols that passed stage-1 screening and are stage-2 WS-wired."""
        return sorted(self._active_symbols)

    @property
    def health_warning(self) -> str | None:
        since = self._rescreen_failing_since
        if not self._rescreen_failures or since is None:
            return None
        return (
            f"rescreen failing since {since:%Y-%m-%d %H:%M:%S} UTC "
            f"({self._rescreen_failures} cycles in a row): {self._rescreen_error}"
        )

    def indicator_snapshot(self) -> dict[str, Any] | None:
        s = self.settings
        tide_keys = ("ema_fast", "ema_mid", "ema_slow")
        groups = [
            {
                "key": "tide",
                "label": "Tide",
                "interval": s.tide_interval,
                "indicators": [
                    {"key": key, "label": f"EMA {period}"}
                    for key, period in zip(tide_keys, s.ema_tide_periods, strict=True)
                ],
            },
            {
                "key": "wave",
                "label": "Wave",
                "interval": s.wave_interval,
                "indicators": [
                    {"key": "stoch_k", "label": f"%K {s.stochastic_k_period}"},
                    {"key": "stoch_d", "label": f"%D {s.stochastic_d_period}"},
                    {
                        "key": "stoch_slow_d",
                        "label": f"Slow %D {s.stochastic_slow_period}",
                    },
                    {"key": "force_index", "label": f"FI {s.fi_ema_period}"},
                ],
            },
            {
                "key": "ripple",
                "label": "Ripple",
                "interval": s.ripple_interval,
                # Not indicators: the breakout trigger is close > previous high.
                "indicators": [
                    {"key": "close", "label": "Close"},
                    {"key": "high", "label": "High"},
                ],
            },
        ]
        rows = []
        for symbol in sorted(self._active_symbols):
            windows = self._candles.get(symbol, {})
            tide = windows.get(s.tide_interval, [])
            wave = windows.get(s.wave_interval, [])
            ripple = windows.get(s.ripple_interval, [])
            tide_values: dict[str, Any] = {}
            if len(tide) > max(s.ema_tide_periods):
                emas = self._tide(tide)["ema"]
                tide_values = {
                    key: emas[str(period)]
                    for key, period in zip(tide_keys, s.ema_tide_periods, strict=True)
                }
            wave_values: dict[str, Any] = {}
            if len(wave) >= 2:
                computed = self._wave(wave)
                wave_values = {
                    "stoch_k": computed["k"],
                    "stoch_d": computed["d"],
                    "stoch_slow_d": computed["slow_d"],
                    "force_index": computed["fi"],
                }
            ripple_values: dict[str, Any] = {}
            if len(ripple) >= 2:
                ripple_values = {
                    field: {
                        "previous": getattr(ripple[-2], field),
                        "current": getattr(ripple[-1], field),
                    }
                    for field in ("close", "high")
                }
            rows.append(
                {
                    "symbol": symbol,
                    "groups": {
                        "tide": _indicator_group(tide, tide_values),
                        "wave": _indicator_group(wave, wave_values),
                        "ripple": _indicator_group(ripple, ripple_values),
                    },
                }
            )
        return {"groups": groups, "rows": transport_value(rows)}

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
        self._universe = list(dict.fromkeys(services.symbols))
        try:
            # Stage 1: REST-only screen of the full universe, no streams yet.
            selected = await self._screen_universe()
            logger.info(
                "tripple_screen initial screening universe=%s selected=%s",
                len(self._universe),
                sorted(selected),
            )
            # Stage 2: only the screened subset gets wired/bootstrapped.
            services.streams.on_reconnect(self._on_streams_reconnect)
            await self._apply_selection(selected)
            self._spawn(self._run())
            self._spawn(self._rescreen_loop())
        except BaseException:
            await self.on_stop()
            raise

    async def _wire_streams(self, symbols: list[str]) -> None:
        subscriptions = [
            (symbol, interval, self._kline_callback(symbol, interval))
            for symbol in symbols
            for interval in self._intervals()
        ]
        await self.services.streams.subscribe_klines_batch(subscriptions)

    def _on_streams_reconnect(self) -> None:
        # Bars that closed while the WS was down were never delivered; reload
        # the closed windows so indicators do not run on a gapped series.
        if self._active and self._active_symbols:
            self._spawn(self._backfill(sorted(self._active_symbols)))

    async def _backfill(self, symbols: list[str]) -> None:
        await self._bootstrap(symbols)
        logger.info(
            "tripple_screen backfilled after reconnect symbols=%s", len(symbols)
        )

    async def _bootstrap(self, symbols: list[str]) -> None:
        cutoff = int(datetime.now(UTC).timestamp() * 1000) - 1
        semaphore = asyncio.Semaphore(self.settings.rest_concurrency)

        async def load_one(symbol: str, interval: str) -> None:
            async with semaphore:
                candles = await self._load_window(symbol, interval, cutoff)
            self._merge(symbol, interval, candles)

        await asyncio.gather(
            *(
                load_one(symbol, interval)
                for symbol in symbols
                for interval in self._intervals()
            )
        )

    async def _screen_universe(self) -> set[str]:
        """Stage 1: REST-only EMA13/EMA34 rising filter over the full universe.

        Ranks qualifiers by relative EMA-spread (trend strength) so the cap
        (``ws_top_n``) admits the strongest trends first; a symbol with an open
        position is always kept regardless of rank, per spec.
        """
        settings = self.settings
        _, mid_period, slow_period = settings.ema_tide_periods
        semaphore = asyncio.Semaphore(settings.rest_concurrency)
        cutoff = int(datetime.now(UTC).timestamp() * 1000) - 1

        async def screen_one(symbol: str) -> tuple[str, Decimal] | None:
            # A REST failure here propagates (via gather) exactly like a
            # bootstrap failure already did before screening existed: on_start
            # aborts rather than silently starting with zero active symbols.
            async with semaphore:
                candles = await self._load_window(
                    symbol, settings.tide_interval, cutoff
                )
            # Evaluation needs the same full closed tide window (_snapshot ->
            # _slice); a younger listing would take a slot yet never be
            # evaluable, re-fetching its window over REST on every trigger.
            window = self._slice(candles, settings.tide_interval, cutoff)
            if window is None:
                logger.debug(
                    "tripple_screen screen symbol=%s skipped: tide history %s < %s",
                    symbol,
                    len(candles),
                    settings.kline_window,
                )
                return None
            closes = [c.close for c in window]
            ema_mid = ema_series(closes, mid_period)
            ema_slow = ema_series(closes, slow_period)
            mid_prev, mid_now = ema_mid[-2], ema_mid[-1]
            slow_prev, slow_now = ema_slow[-2], ema_slow[-1]
            if (
                mid_prev is None
                or mid_now is None
                or slow_prev is None
                or slow_now is None
            ):
                return None
            rising = mid_now > mid_prev and slow_now > slow_prev
            logger.debug(
                "tripple_screen screen symbol=%s rising=%s ema_mid=%s ema_slow=%s",
                symbol,
                rising,
                {"previous": mid_prev, "current": mid_now},
                {"previous": slow_prev, "current": slow_now},
            )
            if not rising or slow_now == 0:
                return None
            return symbol, (mid_now - slow_now) / slow_now

        results = await asyncio.gather(
            *(screen_one(symbol) for symbol in self._universe)
        )
        qualifiers = sorted(
            (item for item in results if item is not None),
            key=lambda item: item[1],
            reverse=True,
        )
        # Positions stay streamed outside the cap — including at a cold start,
        # before anything is active, and for symbols outside the universe.
        keep_for_position = set(self._positions)
        budget = max(0, settings.ws_top_n - len(keep_for_position))
        admitted_new = [
            symbol for symbol, _ in qualifiers if symbol not in keep_for_position
        ][:budget]
        selected = keep_for_position | set(admitted_new)
        logger.debug(
            "tripple_screen screening cycle universe=%s qualifiers=%s "
            "kept_for_position=%s admitted=%s selected=%s",
            len(self._universe),
            len(qualifiers),
            sorted(keep_for_position),
            sorted(admitted_new),
            sorted(selected),
        )
        return selected

    async def _apply_selection(self, selected: set[str]) -> None:
        """Stage 2: wire newly-admitted symbols, unwire dropped ones."""
        to_add = sorted(selected - self._active_symbols)
        to_remove = sorted(self._active_symbols - selected)
        if to_add:
            # Subscribe first: callbacks buffer closes while REST is in flight.
            await self._wire_streams(to_add)
            await self._bootstrap(to_add)
        if to_remove:
            await self._unwire_symbols(to_remove)
        self._active_symbols = selected
        if to_add or to_remove:
            logger.info(
                "tripple_screen active symbols updated added=%s removed=%s total=%s",
                to_add,
                to_remove,
                len(selected),
            )

    async def _unwire_symbols(self, symbols: list[str]) -> None:
        # One batched UNSUBSCRIBE: per-stream messages exceed Binance's
        # 5 msg/s per-connection limit and get the whole connection dropped.
        try:
            await self.services.streams.unsubscribe_klines_batch(
                [
                    (symbol, interval)
                    for symbol in symbols
                    for interval in self._intervals()
                ]
            )
        except Exception:  # noqa: BLE001 - best-effort teardown
            logger.debug(
                "tripple_screen unsubscribe failed symbols=%s", symbols, exc_info=True
            )
        for symbol in symbols:
            self._forget_symbol(symbol)

    def _forget_symbol(self, symbol: str) -> None:
        self._candles.pop(symbol, None)
        self._last_price.pop(symbol, None)
        self._pending.pop(symbol, None)
        self._evaluated.pop(symbol, None)
        # Cooldown is not per-stream state: it belongs to the symbol and must
        # survive the symbol dropping out of the active set and coming back.
        prefix = f"{symbol}:"
        self._consumed_epochs.difference_update(
            {epoch for epoch in self._consumed_epochs if epoch.startswith(prefix)}
        )
        for epoch in [e for e in self._outbox if e.startswith(prefix)]:
            self._outbox.pop(epoch, None)

    def _next_rescreen_ms(self, now_ms: int) -> int:
        """First instant after ``now_ms`` at which the next tide bar has
        closed, plus a small buffer for the exchange's own bar to settle."""
        tide = self.settings.tide_interval
        return bar_open(now_ms, tide) + interval_ms(tide) + _RESCREEN_BUFFER_MS

    async def _rescreen_loop(self) -> None:
        """Re-run stage 1 over the full universe at every tide-interval close.

        A failed cycle (e.g. a transient WS-subscribe race) must not kill the
        loop permanently: without this, `_task_done` only logs the exception
        and the task dies, silently freezing `_active_symbols` on a stale set
        while the heartbeat keeps reporting state=running.
        """
        while True:
            now_ms = int(datetime.now(UTC).timestamp() * 1000)
            next_ms = self._next_rescreen_ms(now_ms)
            await asyncio.sleep(max(0.0, (next_ms - now_ms) / 1000))
            try:
                selected = await self._screen_universe()
                await self._apply_selection(selected)
            except Exception as exc:
                logger.exception("tripple_screen rescreen cycle failed")
                if not self._rescreen_failures:
                    self._rescreen_failing_since = datetime.now(UTC)
                self._rescreen_failures += 1
                self._rescreen_error = f"{type(exc).__name__}: {exc}"[:200]
            else:
                self._rescreen_failures = 0
                self._rescreen_failing_since = None

    async def _load_window(
        self, symbol: str, interval: str, cutoff: int
    ) -> list[Candle]:
        """Fetch exactly the configured closed UTC range, paginating SDK REST.

        end_time is inclusive; excluding the forming bar before pagination
        prevents it from taking a slot in the configured closed-bar window.
        """
        step = interval_ms(interval)
        # +1 keeps the bar that closes exactly at ``cutoff``.
        end = bar_open(cutoff + 1, interval) - 1
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
        self._universe = []
        self._active_symbols = set()
        # Streams belong to Engine; late callbacks are gated by _active.

    def mark_position_open(self, symbol: str) -> None:
        self._positions.add(symbol.upper())

    def mark_position_closed(
        self,
        symbol: str,
        at: datetime | None = None,
        *,
        cooldown_until: datetime | None = None,
    ) -> None:
        """Forget the position and start (or keep) the symbol's cooldown.

        ``cooldown_until`` is the absolute end computed by execution; without
        it the cooldown runs from ``at`` with the current settings. An end
        already known is never shortened nor extended by a repeated close.
        """
        symbol = symbol.upper()
        self._positions.discard(symbol)
        until = cooldown_until or (at or datetime.now(UTC)) + timedelta(
            minutes=self.settings.cooldown_minutes
        )
        known = self._cooldown_until.get(symbol)
        self._cooldown_until[symbol] = until if known is None else max(known, until)

    async def on_position_opened(self, event: PositionEvent) -> None:
        self.mark_position_open(event.symbol)

    async def on_position_closed(self, event: PositionEvent) -> None:
        self.mark_position_closed(
            event.symbol, event.at, cooldown_until=event.cooldown_until
        )

    async def restore_positions(self, snapshot: PositionsSnapshot) -> None:
        self._positions = {symbol.upper() for symbol in snapshot.open_symbols}
        self._cooldown_until = {
            symbol.upper(): until for symbol, until in snapshot.cooldown_until.items()
        }

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
        # +1 keeps the bar that closes exactly at ``cutoff``.
        end = bar_open(cutoff + 1, interval) - 1
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
        swing = min(c.low for c in wave_candles[-settings.swing_low_wave_n :]) * (
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
        exit_policy = None
        if reason is None:
            distance = settings.min_stop_distance_pct / 100
            exit_policy = ExitPolicy(
                version=EXIT_POLICY_VERSION,
                stop_ref=min(swing, trigger.close * (1 - distance)),
                swing_low_ref=swing,
                min_stop_distance_pct=settings.min_stop_distance_pct,
                stop_buffer_pct=settings.stop_buffer_pct,
                tp_pct=settings.tp_pct,
                trail_interval=settings.ripple_interval,
                trail_n=settings.trail_ripple_n,
                trail_cap_pct=settings.trail_cap_pct,
                max_hold_hours=settings.max_hold_hours,
                cooldown_minutes=settings.cooldown_minutes,
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
            exit_policy=exit_policy,
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


def _indicator_group(candles: list[Candle], values: dict[str, Any]) -> dict[str, Any]:
    """One group cell: values on the last closed bar, stamped with its open."""
    return {
        "bar_open_time": datetime.fromtimestamp(candles[-1].open_time / 1000, UTC)
        if values
        else None,
        "values": values,
    }
