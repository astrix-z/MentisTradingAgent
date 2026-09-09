"""Grid strategy: resting buy/sell limit ladder around a moving anchor.

Places ``levels`` sell limits above and ``levels`` buy limits below an anchor
price, spaced by ``spacing`` (fraction of the anchor). When the market drifts at
least one full grid cell from the anchor, the ladder is re-centred on the new
price.

This is a deliberately simple starting point for the strategy registry: it shows
the full decision -> risk -> execution flow (orders go through the risk gate and
the execution service) without pretending to be production-grade grid trading.
"""

from __future__ import annotations

import logging
from typing import Any

from agent.exchange.order_types import OrderResult, OrderSide
from agent.strategies.base import Strategy, StrategyContext, strategy

logger = logging.getLogger(__name__)

# Default grid parameters (override per start via ``config``).
_DEFAULT_SPACING = 0.01
_DEFAULT_LEVELS = 3
_DEFAULT_NOTIONAL_PER_LEVEL = 50.0
_TAG = "grid"


@strategy("grid")
class Grid(Strategy):
    """Re-centring limit-order grid around the current price."""

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self._orders: dict[str, list[int]] = {}
        self._anchors: dict[str, float] = {}

    # --- parameters ----------------------------------------------------------

    def _spacing(self) -> float:
        return float(self.config.get("spacing", _DEFAULT_SPACING) or _DEFAULT_SPACING)

    def _levels(self) -> int:
        levels = int(self.config.get("levels", _DEFAULT_LEVELS) or _DEFAULT_LEVELS)
        return max(1, levels)

    def _notional_per_level(self) -> float:
        return float(
            self.config.get("notional_per_level", _DEFAULT_NOTIONAL_PER_LEVEL)
            or _DEFAULT_NOTIONAL_PER_LEVEL
        )

    # --- lifecycle ------------------------------------------------------------

    async def on_start(self, ctx: StrategyContext) -> None:
        logger.info("grid strategy start symbols=%s", ctx.symbols)
        for symbol in ctx.symbols:
            if symbol not in self._orders:
                self._orders[symbol] = []

    async def on_tick(self, ctx: StrategyContext, symbol: str) -> None:
        if symbol not in self._orders:
            return
        price = ctx.current_price(symbol)
        if price is None:
            return
        anchor = self._anchors.get(symbol)
        if anchor is None or abs(price - anchor) >= anchor * self._spacing():
            await self._reanchor(ctx, symbol, price)

    async def on_fill(
        self,
        ctx: StrategyContext,
        symbol: str,
        side: OrderSide,
        quantity: float,
        price: float,
    ) -> None:
        # A filled grid order takes the position one step up/down the ladder.
        # Production logic would place the opposite-side exit; for now we log and
        # re-anchor so the grid keeps covering the current price.
        logger.info("grid fill %s %s qty=%s @%s", symbol, side, quantity, price)
        if symbol in self._orders:
            mid = ctx.current_price(symbol)
            if mid is not None:
                await self._reanchor(ctx, symbol, mid)

    async def on_stop(self, ctx: StrategyContext) -> None:
        for symbol, order_ids in list(self._orders.items()):
            for order_id in order_ids:
                await ctx.cancel(symbol=symbol, order_id=order_id)
            self._orders[symbol] = []
        logger.info("grid strategy stopped")

    # --- helpers ----------------------------------------------------------------

    async def _reanchor(self, ctx: StrategyContext, symbol: str, price: float) -> None:
        """Cancel the old ladder and place a fresh one centred on ``price``."""
        for order_id in self._orders.get(symbol, []):
            await ctx.cancel(symbol=symbol, order_id=order_id)
        self._orders[symbol] = []

        spacing = self._spacing()
        levels = self._levels()
        notional = self._notional_per_level()
        placed: list[int] = []

        # Buys below the anchor, then sells above it, closest to the anchor first.
        for side, sign in (("SELL", 1.0), ("BUY", -1.0)):
            for step in range(1, levels + 1):
                level_price = price * (1.0 + sign * spacing * step)
                quantity = notional / level_price
                try:
                    result = await self._buy_or_sell(ctx, symbol, side, quantity, level_price)
                except Exception as exc:  # noqa: BLE001 - per-level best effort
                    logger.warning("grid %s %s level %s skipped: %s", symbol, side, step, exc)
                    continue
                placed.append(result.order_id)

        self._orders[symbol] = placed
        self._anchors[symbol] = price
        logger.info("grid reanchored %s @%s placed=%s", symbol, price, len(placed))

    async def _buy_or_sell(
        self, ctx: StrategyContext, symbol: str, side: str, quantity: float, price: float
    ) -> OrderResult:
        if side == "SELL":
            return await ctx.sell_limit(
                symbol=symbol, quantity=quantity, price=price, tag=_TAG
            )
        return await ctx.buy_limit(
            symbol=symbol, quantity=quantity, price=price, tag=_TAG
        )
