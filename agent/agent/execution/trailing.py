"""Trailing stop on closed bars, up to its ceiling (plan §6.4, spec v1.2 §7.2).

For each closed bar of the position's trailing interval after its watermark::

    candidate = min(low[1..trail_n]) × (1 − stop_buffer_pct/100)
    new_stop  = min(max(exchange_stop, candidate), fill_vwap × (1 + trail_cap_pct/100))

rounded down to the tick. The ratchet never moves down, never passes the
ceiling, and moves only by at least one tick.

The decision and the watermark are committed **together**: ``desired_stop``
(what the exchange stop should become) and the bar that produced it. A crash
before that commit re-processes the bar; a crash after it leaves
``desired_stop`` above ``exchange_stop``, which :meth:`Trailer.catch_up`
finishes. The exchange is changed only afterwards, through the position
worker's protocol — or, when the last trade is already at or below the new
stop, by a market exit (``trail`` / ``market_trail``).
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal

from sqlalchemy import select

from agent.execution.filters import round_down
from agent.execution.ledger import lock_position, record_event
from agent.execution.models import MonitorWatermark, Position, PositionStatus
from agent.execution.monitor import MonitorFeed, commit_watermark
from agent.execution.outcome import ExitMechanism, ExitReason
from agent.execution.protection import Step, StepResult
from agent.execution.runtime import ExecutionRuntime
from agent.execution.worker import PositionWorker
from agent.strategies.base import ExitPolicy

logger = logging.getLogger(__name__)


class Trailer:
    def __init__(self, worker: PositionWorker, feed: MonitorFeed) -> None:
        self.worker = worker
        self.feed = feed

    @property
    def runtime(self) -> ExecutionRuntime:
        return self.worker.orders.runtime

    async def on_bars(self, position_id: uuid.UUID) -> StepResult | None:
        """Process every new closed bar of one position, in order."""
        position = await self._position(position_id)
        if position.status != PositionStatus.PROTECTED or position.fill_vwap is None:
            return None
        policy = ExitPolicy.from_transport(position.exit_policy)
        filters = await self.worker.orders.filters.get(
            position.network, position.symbol
        )
        watermark = await self._watermark(position_id, policy.trail_interval)
        moved = False
        opened_ms = (
            int(position.opened_at.timestamp() * 1000) if position.opened_at else 0
        )
        bars = self.feed.bars_after(position_id, watermark)
        for index, bar in enumerate(bars):
            if bar.close_time <= opened_ms:
                continue  # context from before the fill decides nothing
            window = self.feed.window(
                position.symbol, policy.trail_interval, bar.open_time, policy.trail_n
            )
            if window is None:
                if index < policy.trail_n - 1:
                    continue  # the first bars of the data: context only
                logger.warning("%s trailing window has a gap; waiting", position.symbol)
                break
            candidate = policy.trail_candidate([b.low for b in window])
            moved = (
                await self._decide(
                    position_id,
                    policy,
                    bar.open_time,
                    candidate,
                    filters.tick,
                )
                or moved
            )
        return await self.catch_up(position_id) if moved else None

    async def _decide(
        self,
        position_id: uuid.UUID,
        policy: ExitPolicy,
        bar_open_time: int,
        candidate: Decimal,
        tick: Decimal,
    ) -> bool:
        """Store the ratchet's answer to one bar together with the watermark."""
        async with self.runtime.session() as session, session.begin():
            position = await lock_position(session, position_id)
            assert position.fill_vwap is not None
            current = position.desired_stop or position.exchange_stop or Decimal(0)
            ceiling = policy.trail_ceiling(position.fill_vwap)
            new = round_down(min(max(current, candidate), ceiling), tick)
            moved = new - current >= tick
            if moved:
                position.desired_stop = new
                await record_event(
                    session,
                    account=position.account,
                    network=position.network,
                    kind="trail_raised",
                    payload={"stop": new, "bar": bar_open_time, "ceiling": ceiling},
                    position=position,
                )
            await commit_watermark(
                session, position_id, policy.trail_interval, bar_open_time
            )
        return moved

    async def catch_up(self, position_id: uuid.UUID) -> StepResult | None:
        """Bring the exchange stop to the stored ``desired_stop``."""
        position = await self._position(position_id)
        if (
            position.status != PositionStatus.PROTECTED
            or position.desired_stop is None
            or position.exchange_stop is None
            or position.desired_stop <= position.exchange_stop
        ):
            return None
        last = self.feed.last_trade(position.symbol)
        if last is not None and last.price <= position.desired_stop:
            return await self.worker.close(
                position_id, ExitReason.TRAIL, ExitMechanism.MARKET_TRAIL
            )
        result = await self.worker.reprice(position_id, position.desired_stop)
        if result.step is Step.PROTECTED:
            logger.info("%s stop trailed to %s", position.symbol, position.desired_stop)
        return result

    async def _watermark(self, position_id: uuid.UUID, interval: str) -> int | None:
        async with self.runtime.session() as session:
            at = await session.scalar(
                select(MonitorWatermark.bar_open_time).where(
                    MonitorWatermark.position_id == position_id,
                    MonitorWatermark.interval == interval,
                )
            )
        return int(at.timestamp() * 1000) if at is not None else None

    async def _position(self, position_id: uuid.UUID) -> Position:
        async with self.runtime.session() as session:
            position = await session.get(Position, position_id)
            assert position is not None
            return position


__all__ = ["Trailer"]
