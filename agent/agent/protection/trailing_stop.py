"""Trailing stop-loss protector.

For every open position it keeps a SELL stop-limit that trails the best price
seen since the position appeared. As the market rises the stop is ratcheted up
(never down); it is only replaced when it can be tightened or the position size
changes. When the position is closed the protective order is cancelled.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from agent.protection.base import ProtectionContext, Protector, protector

logger = logging.getLogger(__name__)

_DEFAULT_TRAIL_PCT = 0.02
_DEFAULT_LIMIT_OFFSET_PCT = 0.001
_TAG = "protect"
_EPSILON = 1e-12


@dataclass
class _StopState:
    order_id: int | None
    stop: float
    high: float
    quantity: float


@protector("trailing_stop")
class TrailingStop(Protector):
    """Ratchet a trailing stop behind each open position."""

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self._stops: dict[str, _StopState] = {}

    def _trail_pct(self) -> float:
        return float(
            self.config.get("trail_pct", _DEFAULT_TRAIL_PCT) or _DEFAULT_TRAIL_PCT
        )

    def _limit_offset_pct(self) -> float:
        return float(
            self.config.get("limit_offset_pct", _DEFAULT_LIMIT_OFFSET_PCT)
            or _DEFAULT_LIMIT_OFFSET_PCT
        )

    async def on_start(self, ctx: ProtectionContext) -> None:
        logger.info("trailing_stop protector start")

    async def on_tick(self, ctx: ProtectionContext, symbol: str) -> None:
        position = ctx.position(symbol)
        price = ctx.current_price(symbol)
        if position is None or position.quantity <= _EPSILON:
            await self._clear(ctx, symbol)
            return
        if price is None:
            return

        state = self._stops.get(symbol)
        if state is None:
            state = _StopState(order_id=None, stop=0.0, high=price, quantity=position.quantity)
            self._stops[symbol] = state

        # Ratchet the peak price; only ever tighten (raise) the stop.
        peak = max(state.high, price)
        stop = peak * (1.0 - self._trail_pct())
        qty_changed = abs(position.quantity - state.quantity) > _EPSILON

        needs_new = state.order_id is None
        tighter = state.order_id is not None and stop > state.stop + _EPSILON
        if needs_new or tighter or qty_changed:
            await self._replace(ctx, symbol, position.quantity, peak, stop)
            return

        # Track the peak even when we keep the existing stop.
        if peak > state.high:
            state.high = peak
            state.stop = stop

    async def _replace(
        self,
        ctx: ProtectionContext,
        symbol: str,
        quantity: float,
        peak: float,
        stop: float,
    ) -> None:
        state = self._stops.get(symbol)
        if state is not None and state.order_id is not None:
            await ctx.cancel(symbol=symbol, order_id=state.order_id)

        limit_price = stop * (1.0 - self._limit_offset_pct())
        try:
            result = await ctx.place_stop_sell(
                symbol=symbol,
                quantity=quantity,
                stop_price=stop,
                limit_price=limit_price,
                tag=_TAG,
            )
        except Exception as exc:  # noqa: BLE001 - placement is best effort
            logger.warning("trailing_stop placement failed for %s: %s", symbol, exc)
            self._stops[symbol] = _StopState(None, stop, peak, quantity)
            return
        self._stops[symbol] = _StopState(result.order_id, stop, peak, quantity)
        logger.info(
            "trailing stop %s @%.4g qty=%s order=%s", symbol, stop, quantity, result.order_id
        )

    async def _clear(self, ctx: ProtectionContext, symbol: str) -> None:
        state = self._stops.pop(symbol, None)
        if state is not None and state.order_id is not None:
            await ctx.cancel(symbol=symbol, order_id=state.order_id)

    async def on_fill(
        self,
        ctx: ProtectionContext,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
    ) -> None:
        # A protective fill (SELL) closed/reduced the position; let on_tick
        # reconcile the state on the next quote.
        self._stops.pop(symbol, None)

    async def on_stop(self, ctx: ProtectionContext) -> None:
        for symbol in list(self._stops):
            await self._clear(ctx, symbol)
        logger.info("trailing_stop protector stopped")
