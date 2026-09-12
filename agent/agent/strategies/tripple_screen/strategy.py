"""Triple Screen strategy — the agent's decision core (spec ``STRATEGY_TRIPPLE_SCREEN``).

Screens:

* Tide (H4)   — three EMA alignment + price above the slow EMA -> ``BullishTide``;
* Wave (H1)   — Stochastic ``%K/%D`` oversold cross up + Force Index turning
  positive -> ``PullbackUp``;
* Ripple (M15) — close above the previous bar's high -> entry trigger.

Evaluation runs on every closed ripple (M15) bar, only by closed candles, and
emits an ENTRY or HOLD :class:`~agent.strategies.base.Signal`. Sizing, order
type and protection are out of scope here (``D-SIGNAL-ONLY``).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from binance_sdk_spot.websocket_streams.models import KlineResponse

from agent.strategies.base import Signal, Strategy, StrategyServices
from agent.strategies.registry import strategy
from agent.strategies.tripple_screen.indicators import (
    Candle,
    ema_series,
    force_index_series,
    sma_of_optional,
    stochastic_k_series,
)
from agent.strategies.tripple_screen.settings import TripleScreenSettings

logger = logging.getLogger(__name__)

# How much history to bootstrap via REST per interval (bounded by kline_window).
_BOOTSTRAP_CAP = 500


@strategy("tripple_screen")
class TripleScreenStrategy(Strategy):
    """Long-only Triple Screen strategy (Binance Spot)."""

    def __init__(self, params: dict[str, Any]) -> None:
        super().__init__(params)
        self._settings = TripleScreenSettings.from_params(params)
        # symbol -> interval -> closed candles (oldest first).
        self._candles: dict[str, dict[str, list[Candle]]] = {}
        self._last_price: dict[str, Decimal] = {}
        # Position / re-entry state (driven by the engine's execution lane).
        self._positions: set[str] = set()
        self._cooldown_until: dict[str, datetime] = {}
        self._consumed_epochs: set[str] = set()
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def settings(self) -> TripleScreenSettings:
        return self._settings

    # --- lifecycle ---------------------------------------------------------

    async def on_start(self, services: StrategyServices) -> None:
        await self._bootstrap(services)
        await self._wire_streams(services)

    async def _bootstrap(self, services: StrategyServices) -> None:
        """Seed closed-candle windows from REST before subscribing to the WS."""
        limit = min(self._settings.kline_window, _BOOTSTRAP_CAP)
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        for symbol in services.symbols:
            for interval in self._intervals():
                rows = await services.rest.klines(
                    symbol=symbol, interval=interval, limit=limit
                )
                # REST includes the still-forming last candle; keep only closed ones.
                candles = [
                    candle
                    for row in rows
                    if (candle := Candle.from_rest_row(row)).close_time <= now_ms
                ]
                self._candles.setdefault(symbol, {})[interval] = candles

    async def _wire_streams(self, services: StrategyServices) -> None:
        for symbol in services.symbols:
            for interval in self._intervals():
                await services.streams.subscribe_kline(
                    symbol=symbol,
                    interval=interval,
                    callback=self._kline_callback(symbol, interval),
                )

    def _intervals(self) -> tuple[str, str, str]:
        return (
            self._settings.tide_interval,
            self._settings.wave_interval,
            self._settings.ripple_interval,
        )

    def _kline_callback(self, symbol: str, interval: str) -> Any:
        """Wrap the async handler so it can be used as a sync WS callback."""

        def _callback(data: KlineResponse) -> None:
            task = asyncio.create_task(self._on_kline(symbol, interval, data))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

        return _callback

    async def on_stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        self._candles.clear()
        self._last_price.clear()

    # --- position state (called by the engine's execution/portfolio lane) --

    def mark_position_open(self, symbol: str) -> None:
        self._positions.add(symbol.upper())

    def mark_position_closed(self, symbol: str, at: datetime | None = None) -> None:
        symbol = symbol.upper()
        self._positions.discard(symbol)
        closed_at = at or datetime.now(UTC)
        self._cooldown_until[symbol] = closed_at + timedelta(
            minutes=self._settings.cooldown_minutes
        )

    # --- evaluation --------------------------------------------------------

    async def _on_kline(self, symbol: str, interval: str, data: KlineResponse) -> None:
        k = data.k
        if k is None:
            return
        candle = Candle.from_ws(k)
        self._last_price[symbol] = candle.close
        if not candle.closed:
            return

        window = self._candles.setdefault(symbol, {}).setdefault(interval, [])
        window.append(candle)
        limit = self._settings.kline_window
        if len(window) > limit:
            del window[: len(window) - limit]

        if interval == self._settings.ripple_interval:
            await self._evaluate(symbol)

    async def _evaluate(self, symbol: str) -> None:
        try:
            signal = self._decide(symbol)
        except Exception:  # noqa: BLE001 - one symbol must not kill the run
            logger.exception("tripple_screen evaluation failed for %s", symbol)
            return
        if signal is not None:
            await self.services.emit(signal)

    def _decide(self, symbol: str) -> Signal | None:
        windows = self._candles.get(symbol, {})
        ripple = windows.get(self._settings.ripple_interval, [])
        if len(ripple) < 2:
            return None
        trigger = ripple[-1]
        previous = ripple[-2]

        tide = self._bullish_tide(symbol)
        if tide is not True:
            # Non-candidates do not emit HOLD (spec §4).
            return None

        pullback = self._pullback_up(symbol, before_time=trigger.open_time)
        entry_trigger = trigger.close > previous.high

        provenance: dict[str, Any] = {
            "bullish_tide": True,
            "pullback_up": pullback is True,
            "entry_trigger": entry_trigger,
            "ripple_open_time": trigger.open_time,
            "ripple_prev_high": str(previous.high),
            "ripple_close": str(trigger.close),
        }

        if not pullback:
            return self._hold(symbol, "no_pullback", provenance)
        if not entry_trigger:
            return self._hold(symbol, "no_trigger", provenance)

        if symbol in self._positions:
            return self._hold(symbol, "position_open", provenance)

        now = datetime.now(UTC)
        cooldown = self._cooldown_until.get(symbol)
        if cooldown is not None and now < cooldown:
            provenance["cooldown_until"] = cooldown.isoformat()
            return self._hold(symbol, "cooldown", provenance)

        epoch_id = f"{symbol}:{trigger.open_time}"
        if epoch_id in self._consumed_epochs:
            return self._hold(symbol, "epoch_consumed", provenance)
        self._consumed_epochs.add(epoch_id)

        swing_low = self._swing_low(
            symbol, self._settings.swing_low_h1_n, before_time=trigger.open_time
        )
        provenance["epoch_id"] = epoch_id
        provenance["swing_low_ref"] = str(swing_low) if swing_low is not None else None
        return Signal(
            type="ENTRY",
            symbol=symbol,
            emitted_at=now,
            reason="entry",
            epoch_id=epoch_id,
            entry_ref=float(trigger.close),
            swing_low_ref=float(swing_low) if swing_low is not None else None,
            tp_rule=f"fill x {1 + self._settings.tp_pct / 100}",
            valid_until=now + timedelta(minutes=self._settings.signal_validity_minutes),
            provenance=provenance,
        )

    def _hold(self, symbol: str, reason: str, provenance: dict[str, Any]) -> Signal:
        return Signal(
            type="HOLD",
            symbol=symbol,
            emitted_at=datetime.now(UTC),
            reason=reason,
            provenance=provenance,
        )

    # --- screen helpers ----------------------------------------------------

    def _closed_series(self, symbol: str, interval: str) -> list[Candle]:
        return self._candles.get(symbol, {}).get(interval, [])

    def _bullish_tide(self, symbol: str) -> bool | None:
        """``BullishTide`` (spec §3.1); ``None`` when there is not enough history."""
        candles = self._closed_series(symbol, self._settings.tide_interval)
        periods = self._settings.ema_tide_periods
        if len(periods) < 3 or len(candles) < max(periods) + 1:
            return None
        p_fast, p_mid, p_slow = periods[0], periods[1], periods[2]
        closes = [c.close for c in candles]
        emas = {p: ema_series(closes, p) for p in (p_fast, p_mid, p_slow)}

        last = len(closes) - 1
        prev = last - 1
        fast, mid, slow = emas[p_fast], emas[p_mid], emas[p_slow]
        if (
            fast[last] is None
            or fast[prev] is None
            or mid[last] is None
            or mid[prev] is None
            or slow[last] is None
            or slow[prev] is None
        ):
            return None
        return (
            fast[last] > fast[prev]  # type: ignore[operator]
            and mid[last] > mid[prev]  # type: ignore[operator]
            and slow[last] > slow[prev]  # type: ignore[operator]
            and fast[last] > mid[last]  # type: ignore[operator]
            and closes[last] > slow[last]  # type: ignore[operator]
        )

    def _pullback_up(self, symbol: str, *, before_time: int) -> bool:
        """``PullbackUp`` on the last wave bar not overlapping the trigger bar."""
        all_candles = self._closed_series(symbol, self._settings.wave_interval)
        candles = [c for c in all_candles if c.close_time <= before_time]
        settings = self._settings
        needed = max(
            settings.stochastic_k_period + settings.stochastic_d_period,
            settings.fi_ema_period + 1,
        )
        if len(candles) < needed + 1:
            return False

        k = stochastic_k_series(candles, settings.stochastic_k_period)
        d = sma_of_optional(k, settings.stochastic_d_period)
        fi = force_index_series(candles, settings.fi_ema_period)

        t = len(candles) - 1
        p = t - 1
        k_t, k_p = k[t], k[p]
        d_t, d_p = d[t], d[p]
        fi_t, fi_p = fi[t], fi[p]
        if None in (k_t, k_p, d_t, d_p, fi_t, fi_p):
            return False

        ov = Decimal(str(settings.oversold_threshold))
        return (
            k_p < ov  # type: ignore[operator]
            and k_t >= ov  # type: ignore[operator]
            and k_p <= d_p  # type: ignore[operator]
            and k_t > d_t  # type: ignore[operator]
            and fi_p < 0  # type: ignore[operator]
            and fi_t >= 0  # type: ignore[operator]
        )

    def _swing_low(self, symbol: str, n: int, *, before_time: int) -> Decimal | None:
        """``min(low, last n wave bars) * (1 - buffer)`` for the initial stop."""
        candles = [
            c
            for c in self._closed_series(symbol, self._settings.wave_interval)
            if c.close_time <= before_time
        ]
        if len(candles) < n:
            return None
        window = candles[-n:]
        buffer = Decimal(1) - Decimal(str(self._settings.stop_buffer_pct)) / Decimal(
            100
        )
        return min(c.low for c in window) * buffer
