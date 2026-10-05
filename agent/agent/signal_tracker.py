"""Follow each ENTRY signal to its first stop or take crossing (ClickUp 869f6m7vn).

The strategy places no order of its own (D-SIGNAL-ONLY), and the backend shows
"what would have happened" for every ENTRY. It used to close such a position
only when someone viewed it, at the price of that moment: SENTUSDT crossed its
stop at 14:48 UTC on 2026-09-23 and was recorded closed at 15:14, 1.7 % below
the stop. The backend cannot read the exchange; the agent can.

For every ENTRY the agent publishes it records a :class:`SignalWatch` with the
levels the backend shows (stop = ``swing_low_ref``, take = ``entry_ref`` x the
``tp_rule`` factor). Once a minute it reads the closed 1m bars since the
watch's watermark and takes the first bar that reached a level:

* the bar opened beyond the level (a gap) — the exit is at its open;
* otherwise the level itself, traded through within the bar;
* a bar that reached both levels is a stop: the path inside it is unknown,
  and the conservative outcome is the loss.

The exit is dated by the bar's open (the minute of the crossing) and published
as an EXIT naming its ENTRY, with that price. Being durable, the watermark
lets a restarted agent find a crossing that happened while it was down; an
exit is published again until Redis has taken it. These EXITs only describe
signals: they go to the backend's channel, never to execution's inbox.

Bars are read from the first full minute after the ENTRY was emitted: the
ENTRY's own minute also holds trades from before it.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agent.models import SignalWatch
from agent.strategies.base import Signal

logger = logging.getLogger(__name__)

MINUTE_MS = 60_000
CHECK_SECONDS = 60.0
#: Most bars one klines request returns.
PAGE = 1000
STOP = "stop"
TAKE = "tp"  # what the backend reads as hit_take
#: EXIT ids derive from their ENTRY's: a republished exit is the same signal.
_EXIT_NAMESPACE = uuid.UUID("5d0c1e3a-8f0b-4c55-9d6e-3f3b9b1a6c21")
#: The template the strategy formats ``tp_rule`` with (and the backend parses).
_TP_RULE = re.compile(r"fill x ([\d.]+)")


class Klines(Protocol):
    async def klines(
        self,
        *,
        symbol: str,
        interval: str,
        limit: int | None = None,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list[Any]: ...


def take_level(entry_ref: Decimal, tp_rule: str) -> Decimal | None:
    """``"fill x 1.05"`` against ``entry_ref``; ``None`` for another format."""
    match = _TP_RULE.fullmatch(tp_rule)
    if match is None:
        return None
    try:
        return entry_ref * Decimal(match.group(1))
    except InvalidOperation:
        return None


@dataclass(frozen=True)
class Crossing:
    reason: str
    price: Decimal
    at: datetime


def first_crossing(
    bars: list[list[Any]], stop: Decimal, take: Decimal
) -> Crossing | None:
    """The first of ``bars`` (raw 1m klines, oldest first) to reach a level."""
    for bar in bars:
        opened = Decimal(str(bar[1]))
        high, low = Decimal(str(bar[2])), Decimal(str(bar[3]))
        at = datetime.fromtimestamp(int(bar[0]) / 1000, UTC)
        if opened <= stop:
            return Crossing(STOP, opened, at)
        if opened >= take:
            return Crossing(TAKE, opened, at)
        if low <= stop:
            return Crossing(STOP, stop, at)
        if high >= take:
            return Crossing(TAKE, take, at)
    return None


def exit_signal(watch: SignalWatch) -> Signal:
    assert watch.exit_reason is not None and watch.exited_at is not None
    return Signal(
        type="EXIT",
        symbol=watch.symbol,
        emitted_at=watch.exited_at,
        reason="signal tracker",
        exit_reason=watch.exit_reason,
        entry_signal_id=watch.signal_id,
        exit_price=watch.exit_price,
        signal_id=str(uuid.uuid5(_EXIT_NAMESPACE, watch.signal_id)),
    )


class SignalTracker:
    def __init__(
        self,
        db: AsyncEngine | None,
        market: Callable[[str], Klines],
        publish: Callable[[Signal], Awaitable[None]],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._sessions = (
            async_sessionmaker(db, expire_on_commit=False) if db is not None else None
        )
        self._market = market
        self._publish = publish
        self._clock = clock or (lambda: datetime.now(UTC))
        #: Told of each crossing as soon as it is recorded (before the EXIT is
        #: published): the engine's virtual position feedback (869f6m7vg).
        self.on_closed: Callable[[SignalWatch], Awaitable[None]] | None = None

    async def snapshot(
        self, network: str, since: datetime
    ) -> tuple[frozenset[str], list[SignalWatch]]:
        """Symbols followed on ``network`` now, and its exits since ``since``."""
        if self._sessions is None:
            return frozenset(), []
        async with self._sessions() as session:
            open_symbols = (
                await session.scalars(
                    select(SignalWatch.symbol).where(
                        SignalWatch.network == network,
                        SignalWatch.exit_reason.is_(None),
                    )
                )
            ).all()
            exits = (
                await session.scalars(
                    select(SignalWatch)
                    .where(
                        SignalWatch.network == network,
                        SignalWatch.exited_at >= since,
                    )
                    .order_by(SignalWatch.exited_at)
                )
            ).all()
        return frozenset(open_symbols), list(exits)

    async def watch(self, signal: Signal, network: str) -> None:
        """Start following a published ENTRY; a repeated one is a no-op."""
        if (
            self._sessions is None
            or signal.type != "ENTRY"
            or signal.entry_ref is None
            or signal.swing_low_ref is None
        ):
            return
        take = take_level(signal.entry_ref, signal.tp_rule or "")
        if take is None:
            logger.warning(
                "signal %s: tp_rule %r has no take level; not followed",
                signal.signal_id,
                signal.tp_rule,
            )
            return
        emitted_ms = int(signal.emitted_at.timestamp() * 1000)
        first = -(-emitted_ms // MINUTE_MS) * MINUTE_MS  # next full minute
        async with self._sessions() as session:
            await session.execute(
                insert(SignalWatch)
                .values(
                    signal_id=signal.signal_id,
                    network=network,
                    symbol=signal.symbol,
                    entry_ref=signal.entry_ref,
                    stop=signal.swing_low_ref,
                    take=take,
                    emitted_at=signal.emitted_at,
                    checked_until=first,
                )
                .on_conflict_do_nothing(index_elements=["signal_id"])
            )
            await session.commit()

    async def check(self) -> int:
        """One round: find new crossings, publish every unpublished exit.

        Returns how many exits were published.
        """
        if self._sessions is None:
            return 0
        now_ms = int(self._clock().timestamp() * 1000)
        # The newest closed bar opened a full minute before the current one.
        last_closed = (now_ms // MINUTE_MS - 1) * MINUTE_MS
        async with self._sessions() as session:
            watching = (
                await session.scalars(
                    select(SignalWatch)
                    .where(SignalWatch.exit_reason.is_(None))
                    .order_by(SignalWatch.emitted_at)
                )
            ).all()
        for watch in watching:
            try:
                await self._scan(watch, last_closed)
            except Exception as exc:  # noqa: BLE001 - one symbol must not stop the rest
                logger.warning(
                    "signal %s %s: bars unavailable, retried next round: %s",
                    watch.symbol,
                    watch.signal_id,
                    exc,
                )
        return await self._publish_exits()

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.check()
            except Exception:  # noqa: BLE001 - the loop outlives any one round
                logger.exception("signal tracker round failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=CHECK_SECONDS)
            except TimeoutError:
                pass

    async def _scan(self, watch: SignalWatch, last_closed: int) -> None:
        assert self._sessions is not None
        cursor = watch.checked_until
        crossing: Crossing | None = None
        while cursor <= last_closed and crossing is None:
            bars = await self._market(watch.network).klines(
                symbol=watch.symbol,
                interval="1m",
                start_time=cursor,
                end_time=last_closed,
                limit=PAGE,
            )
            bars = [b for b in bars if cursor <= int(b[0]) <= last_closed]
            if not bars:  # no trades since the cursor: nothing to cross
                cursor = last_closed + MINUTE_MS
                break
            crossing = first_crossing(bars, watch.stop, watch.take)
            cursor = (
                int(crossing.at.timestamp() * 1000) + MINUTE_MS
                if crossing is not None
                else int(bars[-1][0]) + MINUTE_MS
            )
        if cursor == watch.checked_until and crossing is None:
            return
        async with self._sessions() as session:
            row = await session.get(SignalWatch, watch.signal_id, with_for_update=True)
            assert row is not None
            row.checked_until = cursor
            if crossing is not None:
                row.exit_reason = crossing.reason
                row.exit_price = crossing.price
                row.exited_at = crossing.at
                logger.info(
                    "signal %s %s crossed %s at %s, price %s",
                    row.symbol,
                    row.signal_id,
                    crossing.reason,
                    crossing.at.isoformat(),
                    crossing.price,
                )
            await session.commit()
        if crossing is not None and self.on_closed is not None:
            try:
                await self.on_closed(row)
            except Exception:  # noqa: BLE001 - the crossing is recorded
                logger.exception("close of %s not delivered", row.signal_id)

    async def _publish_exits(self) -> int:
        assert self._sessions is not None
        async with self._sessions() as session:
            pending = (
                await session.scalars(
                    select(SignalWatch)
                    .where(
                        SignalWatch.exit_reason.is_not(None),
                        SignalWatch.published_at.is_(None),
                    )
                    .order_by(SignalWatch.exited_at)
                )
            ).all()
        published = 0
        for watch in pending:
            try:
                await self._publish(exit_signal(watch))
            except Exception as exc:  # noqa: BLE001 - published again next round
                logger.warning("EXIT of %s not published yet: %s", watch.signal_id, exc)
                break
            async with self._sessions() as session:
                row = await session.get(SignalWatch, watch.signal_id)
                assert row is not None
                row.published_at = self._clock()
                await session.commit()
            published += 1
        return published
