"""Triple Screen v2: the v1 tide and universe screen, new wave and ripple screens.

* Stage 1 and the tide screen are v1's, unchanged; a symbol without a
  percentile table for the ripple interval is ruled out before the tide screen.
* Wave (H1 by default): EMA fast > EMA mid > EMA slow on the last closed bar.
* Ripple (M15 by default): the one-bar % change of each ripple EMA (5 and 13
  by default) on the trigger bar is above that EMA's percentile row of
  ``prcntl_<symbol>_<ripple interval>`` in the Binance DB. It replaces the v1
  breakout (close above the previous high).

Execution, exits, cooldown and the epoch machinery are v1's.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

from agent.strategies.base import (
    EXIT_POLICY_VERSION,
    ExitPolicy,
    Signal,
    StrategyServices,
    transport_value,
)
from agent.strategies.registry import strategy
from agent.strategies.tripple_screen.indicators import Candle, ema_series
from agent.strategies.tripple_screen.strategy import (
    TripleScreenStrategy,
    _indicator_group,
)
from agent.strategies.tripple_screen_v2.percentiles import (
    DbPercentileSource,
    PercentileSource,
)
from agent.strategies.tripple_screen_v2.presets import BUILTIN_PRESETS
from agent.strategies.tripple_screen_v2.schema import CONSTRAINTS, LOOKUPS
from agent.strategies.tripple_screen_v2.settings import TripleScreenV2Settings

logger = logging.getLogger(__name__)

_HUNDRED = Decimal(100)


@strategy("tripple_screen_v2")
class TripleScreenV2Strategy(TripleScreenStrategy):
    """Triple Screen v2: wave EMA alignment, ripple EMA change above its percentile."""

    label = "Triple Screen v2"
    # v1's machinery reads only the fields both models share.
    settings_model = TripleScreenV2Settings  # type: ignore[assignment]
    settings_constraints = CONSTRAINTS
    settings_lookups = LOOKUPS
    settings_legacy_keys = {}
    settings_presets = BUILTIN_PRESETS

    def __init__(self, params: dict[str, Any]) -> None:
        super().__init__(params)
        #: Built from ``services.market_db`` on start unless already set.
        self._percentile_source: PercentileSource | None = None
        #: Per selected symbol, ripple EMA period -> percentile threshold (%).
        self._thresholds: dict[str, dict[int, Decimal]] = {}

    def _parse_settings(self, params: dict[str, Any]) -> TripleScreenV2Settings:  # type: ignore[override]
        return TripleScreenV2Settings.from_params(params)

    @property
    def settings(self) -> TripleScreenV2Settings:  # type: ignore[override]
        return cast(TripleScreenV2Settings, self._settings)

    @property
    def _percentiles(self) -> PercentileSource:
        if self._percentile_source is None:
            raise RuntimeError("strategy is not started")
        return self._percentile_source

    async def on_start(self, services: StrategyServices) -> None:
        if self._percentile_source is None:
            if services.market_db is None:
                raise RuntimeError(
                    "Triple Screen v2 reads percentiles from the Binance DB: "
                    "BINANCE_DB_URL is not configured"
                )
            self._percentile_source = DbPercentileSource(services.market_db)
        await super().on_start(services)

    async def on_stop(self) -> None:
        await super().on_stop()
        self._thresholds.clear()

    async def _screen_exclusions(self) -> dict[str, str]:
        interval = self.settings.ripple_interval
        available = await self._percentiles.available(self._universe, interval)
        return {
            symbol: (
                f"no {interval} percentile table (prcntl_{symbol.lower()}_{interval})"
            )
            for symbol in self._universe
            if symbol not in available
        }

    async def _screen_universe(self) -> set[str]:
        """v1 stage 1, then the selection's thresholds, re-read every cycle."""
        selected = await super()._screen_universe()
        settings = self.settings
        self._thresholds = await self._percentiles.thresholds(
            sorted(selected),
            settings.ripple_interval,
            settings.ema_ripple_periods,
            settings.ripple_percentile,
        )
        missing = sorted(selected - self._thresholds.keys())
        if missing:
            logger.warning(
                "tripple_screen_v2 no %s%% percentile row for %s",
                settings.ripple_percentile,
                missing,
            )
        return selected

    def indicator_snapshot(self) -> dict[str, Any] | None:
        s = self.settings
        tide_keys = ("ema_fast", "ema_mid", "ema_slow")
        ripple_keys = ("fast", "mid")
        percentile = f"p{s.ripple_percentile}"
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
                    {"key": key, "label": f"EMA {period}"}
                    for key, period in zip(tide_keys, s.ema_wave_periods, strict=True)
                ],
            },
            {
                "key": "ripple",
                "label": "Ripple",
                "interval": s.ripple_interval,
                "indicators": [
                    indicator
                    for key, period in zip(
                        ripple_keys, s.ema_ripple_periods, strict=True
                    )
                    for indicator in (
                        {"key": f"delta_{key}", "label": f"ΔEMA {period} %"},
                        {
                            "key": f"threshold_{key}",
                            "label": f"{percentile} ΔEMA {period} %",
                        },
                    )
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
            if len(wave) > max(s.ema_wave_periods):
                emas = self._wave(wave)["ema"]
                wave_values = {
                    key: emas[str(period)]
                    for key, period in zip(tide_keys, s.ema_wave_periods, strict=True)
                }
            ripple_values: dict[str, Any] = {}
            if len(ripple) > max(s.ema_ripple_periods):
                thresholds = self._thresholds.get(symbol, {})
                closes = [c.close for c in ripple]
                for key, period in zip(ripple_keys, s.ema_ripple_periods, strict=True):
                    series = ema_series(closes, period)
                    older = series[-3] if len(series) >= 3 else None
                    ripple_values[f"delta_{key}"] = {
                        "previous": _delta_pct(older, series[-2]),
                        "current": _delta_pct(series[-2], series[-1]),
                    }
                    ripple_values[f"threshold_{key}"] = {
                        "previous": None,
                        "current": thresholds.get(period),
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
        snapshot: dict[str, Any] = {"groups": groups, "rows": transport_value(rows)}
        if self._screening is not None:
            snapshot["screening"] = {
                **self._screening,
                "active": len(self._active_symbols),
                "error": self._rescreen_error if self._rescreen_failures else None,
            }
        return snapshot

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
            # The wave bar must have closed before the trigger opened.
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
            "tripple_screen_v2 tide symbol=%s ripple_open_time=%s bullish=%s "
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
        ripple_screen = self._ripple(symbol, snapshot[settings.ripple_interval])
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
                "ripple": ripple_screen,
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
                    (not wave["ema_aligned"], "no_wave_alignment"),
                    (not ripple_screen["thresholds_loaded"], "no_percentiles"),
                    (not ripple_screen["momentum"], "no_momentum"),
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
            "tripple_screen_v2 decision symbol=%s epoch=%s type=%s reason=%s "
            "wave=%s ripple=%s state=%s",
            symbol,
            epoch,
            "HOLD" if reason else "ENTRY",
            reason or "entry",
            wave,
            ripple_screen,
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

    def _wave(self, candles: list[Candle]) -> dict[str, Any]:
        """EMA fast > EMA mid > EMA slow on the last closed wave bar."""
        periods = self.settings.ema_wave_periods
        closes = [c.close for c in candles]
        pairs = [ema_series(closes, period)[-2:] for period in periods]
        current = [pair[-1] for pair in pairs]
        flags = {
            name: left is not None and right is not None and left > right
            for name, left, right in (
                ("fast_above_mid", current[0], current[1]),
                ("mid_above_slow", current[1], current[2]),
            )
        }
        return {
            "open_time": candles[-1].open_time,
            "close_time": candles[-1].close_time,
            "close": candles[-1].close,
            "ema": {
                str(period): {
                    "previous": pair[0] if len(pair) == 2 else None,
                    "current": pair[-1],
                }
                for period, pair in zip(periods, pairs, strict=True)
            },
            "conditions": flags,
            "ema_aligned": all(flags.values()),
        }

    def _ripple(self, symbol: str, candles: list[Candle]) -> dict[str, Any]:
        """Each ripple EMA's one-bar % change above its percentile threshold."""
        settings = self.settings
        thresholds = self._thresholds.get(symbol)
        closes = [c.close for c in candles]
        ema: dict[str, Any] = {}
        conditions: dict[str, bool] = {}
        for period in settings.ema_ripple_periods:
            previous, current = ema_series(closes, period)[-2:]
            delta = _delta_pct(previous, current)
            threshold = thresholds.get(period) if thresholds else None
            ema[str(period)] = {
                "previous": previous,
                "current": current,
                "delta_pct": delta,
                "threshold_pct": threshold,
            }
            conditions[f"ema_{period}_above_p{settings.ripple_percentile}"] = (
                delta is not None and threshold is not None and delta > threshold
            )
        return {
            "open_time": candles[-1].open_time,
            "close_time": candles[-1].close_time,
            "close": candles[-1].close,
            "percentile": settings.ripple_percentile,
            "ema": ema,
            "thresholds_loaded": thresholds is not None,
            "conditions": conditions,
            "momentum": all(conditions.values()),
        }


def _delta_pct(previous: Decimal | None, current: Decimal | None) -> Decimal | None:
    """``(current − previous) / previous × 100``, as in ``klines_*_ema``."""
    if previous is None or current is None or previous == 0:
        return None
    return (current - previous) / previous * _HUNDRED
