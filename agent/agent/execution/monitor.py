"""MonitorFeed: market data of open positions, independent of the strategy (§9.3).

For every open position the feed keeps, through the process's
:class:`~agent.exchange.hub.StreamHub`:

* closed bars of the position's trailing interval (from its exit policy), with
  enough history before the position's watermark to evaluate the first bar
  after it (``trail_n`` bars of context);
* the last trade and its time, from the ``trade`` stream — the guard's price
  source (plan §6.6).

Positions are followed whether or not the strategy runs, and whether or not
their symbol is in the strategy's universe. The feed only **delivers**: the
position worker decides on each bar and commits the watermark together with
that decision (:func:`commit_watermark`), so a crash can never lose a bar that
moved the stop.

On a cold start and after every reconnect the feed reloads bars over REST from
the watermark back ``trail_n`` bars; until that backfill has closed the gap the
symbol is *not ready* and no bar is handed out, while bars arriving on the
websocket meanwhile are kept. Duplicates and out-of-order bars collapse on
their open time; a bar still forming is never used. A symbol whose trades have
been silent longer than ``stale_after`` is reported degraded.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from agent.common.intervals import interval_ms
from agent.exchange.hub import StreamHub, StreamView
from agent.execution.models import MonitorWatermark
from agent.execution.quotes import LastTrade

logger = logging.getLogger(__name__)

STALE_AFTER = timedelta(seconds=60)
_KEEP_BARS = 200


@dataclass(frozen=True)
class Bar:
    """A closed bar of a monitored symbol (our value, derived from the SDK's)."""

    open_time: int
    close_time: int
    low: Decimal
    high: Decimal
    close: Decimal


@dataclass(frozen=True)
class Followed:
    position_id: uuid.UUID
    symbol: str
    interval: str
    trail_n: int
    watermark: int | None  # open time of the last bar whose result is stored


KlineLoader = Callable[[str, str, int | None, int], Awaitable[list[list[Any]]]]


async def commit_watermark(
    session: AsyncSession, position_id: uuid.UUID, interval: str, bar_open_time: int
) -> None:
    """Advance a position's watermark in the caller's transaction.

    Called by the position worker in the same transaction that stores what the
    bar changed (new desired stop, or a planned operation), never by the feed.
    """
    at = datetime.fromtimestamp(bar_open_time / 1000, UTC)
    await session.execute(
        insert(MonitorWatermark)
        .values(position_id=position_id, interval=interval, bar_open_time=at)
        .on_conflict_do_update(
            index_elements=["position_id", "interval"],
            set_={"bar_open_time": at, "updated_at": datetime.now(UTC)},
            where=MonitorWatermark.bar_open_time < at,
        )
    )


class MonitorFeed:
    def __init__(
        self,
        hub: StreamHub,
        load_klines: KlineLoader,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        stale_after: timedelta = STALE_AFTER,
    ) -> None:
        self._hub = hub
        self._load = load_klines
        self._clock = clock
        self._stale_after = stale_after
        self._view: StreamView | None = None
        self._followed: dict[uuid.UUID, Followed] = {}
        self._bars: dict[tuple[str, str], dict[int, Bar]] = {}
        self._ready: set[tuple[str, str]] = set()
        self._trades: dict[str, LastTrade] = {}
        self._backfills: dict[tuple[str, str], asyncio.Task[None]] = {}
        #: Called after new closed bars became available for a symbol.
        self.on_bars: Callable[[str, str], None] | None = None

    # --- following positions ----------------------------------------------------------

    async def follow(self, network: str, positions: list[Followed]) -> None:
        """Make the feed follow exactly ``positions`` (open positions only)."""
        if self._view is None and positions:
            self._view = await self._hub.attach(network, "monitor")  # type: ignore[arg-type]
            await self._view.start()
            self._view.on_reconnect(self._reconnected)
        wanted = {p.position_id: p for p in positions}
        before = {(p.symbol, p.interval) for p in self._followed.values()}
        after = {(p.symbol, p.interval) for p in wanted.values()}
        before_symbols = {p.symbol for p in self._followed.values()}
        after_symbols = {p.symbol for p in wanted.values()}
        self._followed = wanted
        if self._view is None:
            return
        added = sorted(after - before)
        removed = sorted(before - after)
        if added:
            await self._view.subscribe_klines_batch(
                [(s, i, self._kline_callback(s, i)) for s, i in added]
            )
        new_symbols = sorted(after_symbols - before_symbols)
        if new_symbols:
            await self._view.subscribe_trades_batch(
                [(s, self._trade_callback(s)) for s in new_symbols]
            )
        if removed:
            await self._view.unsubscribe_klines_batch(removed)
            for key in removed:
                self._bars.pop(key, None)
                self._ready.discard(key)
        gone = sorted(before_symbols - after_symbols)
        if gone:
            await self._view.unsubscribe_trades_batch(gone)
            for symbol in gone:
                self._trades.pop(symbol, None)
        for key in added:
            self._schedule_backfill(key)
        if not wanted:
            await self.close()

    async def close(self) -> None:
        for task in self._backfills.values():
            task.cancel()
        self._backfills.clear()
        view, self._view = self._view, None
        if view is not None:
            await view.stop()

    # --- reading ---------------------------------------------------------------------------

    def ready(self, symbol: str, interval: str) -> bool:
        return (symbol, interval) in self._ready

    def bars_after(self, position_id: uuid.UUID, watermark: int | None) -> list[Bar]:
        """Closed bars after ``watermark``, oldest first; [] until ready."""
        followed = self._followed.get(position_id)
        if followed is None or not self.ready(followed.symbol, followed.interval):
            return []
        bars = self._bars.get((followed.symbol, followed.interval), {})
        return [bars[t] for t in sorted(bars) if watermark is None or t > watermark]

    def window(self, symbol: str, interval: str, upto: int, n: int) -> list[Bar] | None:
        """The ``n`` closed bars ending with the one opened at ``upto``.

        ``None`` when any of them is missing: the ratchet must not run on a
        gapped window.
        """
        bars = self._bars.get((symbol, interval), {})
        step = interval_ms(interval)
        wanted = [upto - step * k for k in range(n - 1, -1, -1)]
        if not all(t in bars for t in wanted):
            return None
        return [bars[t] for t in wanted]

    def last_trade(self, symbol: str) -> LastTrade | None:
        return self._trades.get(symbol)

    def degraded(self) -> dict[str, str]:
        """Symbols whose data is stale or still being backfilled."""
        now = self._clock()
        problems: dict[str, str] = {}
        for followed in self._followed.values():
            trade = self._trades.get(followed.symbol)
            if trade is None or now - trade.time > self._stale_after:
                problems[followed.symbol] = "no recent trades"
            if not self.ready(followed.symbol, followed.interval):
                problems[followed.symbol] = "bars not backfilled"
        return problems

    # --- websocket ----------------------------------------------------------------------------

    def _kline_callback(self, symbol: str, interval: str) -> Callable[[Any], None]:
        def on_message(message: Any) -> None:
            k = getattr(message, "k", None)
            if k is None or not k.x:  # a forming bar is never used
                return
            self._add(
                symbol,
                interval,
                [
                    Bar(
                        open_time=int(k.t),
                        close_time=int(k.T),
                        low=Decimal(k.l),
                        high=Decimal(k.h),
                        close=Decimal(k.c),
                    )
                ],
            )

        return on_message

    def _trade_callback(self, symbol: str) -> Callable[[Any], None]:
        def on_message(message: Any) -> None:
            if message.p is None or message.T is None:
                return
            when = datetime.fromtimestamp(int(message.T) / 1000, UTC)
            current = self._trades.get(symbol)
            if current is None or when >= current.time:
                self._trades[symbol] = LastTrade(Decimal(message.p), when)

        return on_message

    def _reconnected(self) -> None:
        # Bars that closed while disconnected were never delivered.
        for key in {(p.symbol, p.interval) for p in self._followed.values()}:
            self._ready.discard(key)
            self._schedule_backfill(key)

    def _add(self, symbol: str, interval: str, bars: list[Bar]) -> None:
        store = self._bars.setdefault((symbol, interval), {})
        for bar in bars:
            store[bar.open_time] = bar  # a duplicate replaces, order is by key
        for stale in sorted(store)[:-_KEEP_BARS]:
            del store[stale]
        if bars and self.ready(symbol, interval) and self.on_bars is not None:
            self.on_bars(symbol, interval)

    # --- backfill ----------------------------------------------------------------------------

    def _schedule_backfill(self, key: tuple[str, str]) -> None:
        task = self._backfills.get(key)
        if task is None or task.done():
            self._backfills[key] = asyncio.create_task(self.backfill(*key))

    async def backfill(self, symbol: str, interval: str) -> None:
        """Reload closed bars from ``trail_n`` before the oldest watermark."""
        followers = [
            p
            for p in self._followed.values()
            if (p.symbol, p.interval) == (symbol, interval)
        ]
        if not followers:
            return
        step = interval_ms(interval)
        context = max(p.trail_n for p in followers)
        marks = [p.watermark for p in followers if p.watermark is not None]
        now_ms = int(self._clock().timestamp() * 1000)
        start = min(marks) - step * context if marks else now_ms - step * (context + 2)
        try:
            rows = await self._load(symbol, interval, start, 1000)
        except Exception as exc:  # noqa: BLE001 - retried on the next round
            logger.warning("backfill %s %s failed: %s", symbol, interval, exc)
            return
        closed = [
            Bar(
                open_time=int(r[0]),
                close_time=int(r[6]),
                low=Decimal(str(r[3])),
                high=Decimal(str(r[2])),
                close=Decimal(str(r[4])),
            )
            for r in rows
            if int(r[6]) < now_ms  # the forming bar stays out
        ]
        self._add(symbol, interval, closed)
        self._ready.add((symbol, interval))
        logger.info("monitor backfilled %s %s bars=%s", symbol, interval, len(closed))
        if self.on_bars is not None:
            self.on_bars(symbol, interval)


__all__ = [
    "Bar",
    "Followed",
    "MonitorFeed",
    "commit_watermark",
]
