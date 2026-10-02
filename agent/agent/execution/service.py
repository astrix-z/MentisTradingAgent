"""ExecutionService: everything execution runs, wired together (EXEC-11b).

Built once at boot around the :class:`ExecutionRuntime`:

* **startup** — the runtime takes the execution lock and, *before* anything
  may be mutated, runs the leadership hook: the reconciler books what
  happened on the exchange while the agent was away (trades, UNKNOWN entries,
  lists that finished). Admission stays closed until that hook is done.
* **entries** — admission reserves, the order manager buys, a fill goes
  straight to protection;
* **the loop** — every few seconds: entries left reserved or UNKNOWN,
  positions without confirmed protection (deadline and protection),
  operations to resume, time exits, the guard, trailing catch-up, the
  monitor's subscriptions and, every ``reconcile_interval``, a full reconcile;
* **bars** — the monitor feed wakes the trailer for the positions of a symbol.

A restored entry (BUY filled, agent died before recording it) is found by the
startup reconcile, its position opened with the protection deadline counted
from that discovery, and protected by the next round. Nothing is ever sent
twice: every step goes through the attempt journal.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from agent.exchange.hub import StreamHub
from agent.exchange.rest import ExchangeRest
from agent.execution.admission import Admission
from agent.execution.exits import Exits
from agent.execution.filters import FilterCache
from agent.execution.ledger import (
    CommissionConverter,
    Finalizer,
    PositionApplier,
    lock_position,
)
from agent.execution.models import (
    ExecutionIntent,
    Fill,
    IntentState,
    MonitorWatermark,
    Position,
    PositionStatus,
)
from agent.execution.monitor import Followed, MonitorFeed
from agent.execution.orders import OrderManager
from agent.execution.protection import Protector
from agent.execution.quotes import QuoteProvider
from agent.execution.reconcile import Reconciler
from agent.execution.runtime import ExecutionContext, ExecutionRuntime
from agent.execution.trailing import Trailer
from agent.execution.worker import PositionWorker
from agent.strategies.base import ExitPolicy

logger = logging.getLogger(__name__)

TICK_SECONDS = 5.0
UNSAFE = (
    PositionStatus.PROTECTING,
    PositionStatus.UNPROTECTED,
    PositionStatus.PROTECTION_OVERDUE,
)


class ExecutionService:
    def __init__(
        self,
        runtime: ExecutionRuntime,
        *,
        admission: Admission,
        market: Callable[[str], ExchangeRest],
        hub: StreamHub | None,
        filters: FilterCache,
        clock: Callable[[], datetime],
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        commission_price: Callable[[str, datetime], Awaitable[Decimal | None]]
        | None = None,
    ) -> None:
        self.runtime = runtime
        self.admission = admission
        self._clock = clock
        self._market = market

        async def no_price(asset: str, at: datetime) -> Decimal | None:
            return None

        price = commission_price or no_price

        def base_of(symbol: str) -> str:
            return symbol.removesuffix("USDT")

        self.applier = PositionApplier(
            CommissionConverter("USDT", base_of, price), base_of
        )
        self.finalizer = Finalizer(fallback_price=price)
        self.orders = OrderManager(
            runtime,
            applier=self.applier,
            filters=filters,
            quotes=lambda network: QuoteProvider(market(network), clock=clock),
            clock=clock,
            sleep=sleep,
        )
        self.protector = Protector(self.orders, self.finalizer)
        self.worker = PositionWorker(self.protector)

        async def last_price(symbol: str) -> Decimal | None:
            context = runtime.context
            if context is None:
                return None
            ticker = await market(context.network).ticker_price(symbol)
            value = getattr(getattr(ticker, "actual_instance", ticker), "price", None)
            return Decimal(value) if value else None

        self.reconciler = Reconciler(self.worker, prices=last_price)
        self.feed: MonitorFeed | None = None
        if hub is not None:

            async def load(
                symbol: str, interval: str, start: int | None, limit: int
            ) -> list[list[Any]]:
                context = runtime.context
                assert context is not None
                return await market(context.network).klines(
                    symbol=symbol, interval=interval, start_time=start, limit=limit
                )

            self.feed = MonitorFeed(hub, load, clock=clock)
            self.feed.on_bars = self._on_bars
        self.trailer = Trailer(self.worker, self.feed) if self.feed else None
        self.exits = Exits(self.worker, self.feed, clock=clock)
        self._task: asyncio.Task[None] | None = None
        self._busy: set[asyncio.Task[Any]] = set()
        self._last_reconcile: datetime | None = None

        runtime.on_leadership(self._startup_reconcile)
        runtime.close_position = self.exits.close_symbol
        runtime.release_entry = self.orders.release_entry
        admission.on_reserved = self._on_reserved
        runtime.on_watch(self._check_deadlines)
        self.orders.on_filled = self._on_filled

    # --- lifecycle -----------------------------------------------------------------------

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        tasks = [t for t in (self._task, *self._busy) if t is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._task = None
        self._busy.clear()
        if self.feed is not None:
            await self.feed.close()

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 - the loop must survive anything
                logger.exception("execution round failed")
            await asyncio.sleep(TICK_SECONDS)

    # --- hooks -----------------------------------------------------------------------------

    async def _startup_reconcile(self, context: ExecutionContext) -> None:
        """Before any mutation after the lock is taken: learn what happened."""
        logger.info(
            "reconciling %s/%s before execution", context.account, context.network
        )
        report = await self.reconciler.run()
        self._last_reconcile = self._clock()
        logger.info(
            "startup reconcile: fills=%s closed=%s unprotected=%s quarantined=%s divergence=%s",
            report.fills,
            report.closed,
            report.unprotected,
            report.quarantined,
            report.divergence,
        )

    async def _check_deadlines(self, context: ExecutionContext) -> None:
        """The 30 s protection deadline, from the runtime watchdog.

        The execution round may be stuck on an OCO answer for the very
        position that is running out of time, so the deadline is not left to
        it. This only marks the position overdue and alerts; it never sells
        through an OCO whose outcome is not known.
        """
        for position in await self._open():
            if position.status in UNSAFE:
                await self.protector.check_deadline(position.id)

    async def _on_reserved(self, signal_id: uuid.UUID) -> None:
        await self.orders.execute_entry(signal_id)

    async def _on_filled(self, position_id: uuid.UUID) -> None:
        await self.protector.protect(position_id)

    def _on_bars(self, symbol: str, interval: str) -> None:
        if self.trailer is None:
            return
        task = asyncio.ensure_future(self._trail_symbol(symbol))
        self._busy.add(task)
        task.add_done_callback(self._busy.discard)

    async def _trail_symbol(self, symbol: str) -> None:
        assert self.trailer is not None
        for position in await self._open():
            if (
                position.symbol == symbol
                and position.status == PositionStatus.PROTECTED
            ):
                await self.trailer.on_bars(position.id)

    # --- one round -------------------------------------------------------------------------

    async def tick(self) -> None:
        runtime = self.runtime
        await runtime.tick()
        if runtime.context is None or not runtime.may_mutate():
            return
        now = self._clock()
        interval = timedelta(seconds=runtime.settings.reconcile_interval_s)
        if self._last_reconcile is None or now - self._last_reconcile >= interval:
            await self.reconciler.run()
            await self._revalue_commissions()
            self._last_reconcile = now
        await self.orders.resolve_entries()
        for signal_id in await self._reserved():
            await self.orders.execute_entry(signal_id)
        positions = await self._open()
        await self._follow(positions)
        for position in positions:
            await self._tend(position)
        if self.feed is not None:
            for symbol, problem in self.feed.degraded().items():
                runtime.alerts[f"feed:{symbol}"] = problem

    async def _tend(self, position: Position) -> None:
        if position.operation is not None:
            await self.worker.resume(position.id)
            return
        if position.status in UNSAFE:
            await self.protector.check_deadline(position.id)
            await self.protector.protect(position.id)
            return
        if position.status != PositionStatus.PROTECTED:
            return
        if await self.exits.check_time(position.id) is not None:
            return
        if await self.exits.check_guard(position.id) is not None:
            return
        if self.trailer is not None:
            await self.trailer.catch_up(position.id)

    async def _follow(self, positions: list[Position]) -> None:
        if self.feed is None or self.runtime.context is None:
            return
        followed = []
        async with self.runtime.session() as session:
            marks = {
                (m.position_id, m.interval): int(m.bar_open_time.timestamp() * 1000)
                for m in (await session.scalars(select(MonitorWatermark))).all()
            }
        for position in positions:
            policy = ExitPolicy.from_transport(position.exit_policy)
            followed.append(
                Followed(
                    position.id,
                    position.symbol,
                    policy.trail_interval,
                    policy.trail_n,
                    marks.get((position.id, policy.trail_interval)),
                )
            )
        await self.feed.follow(self.runtime.context.network, followed)

    # --- reads ---------------------------------------------------------------------------------

    async def _revalue_commissions(self) -> None:
        """Retry the fees closed positions could not value when they closed."""
        context = self.runtime.context
        if context is None:
            return
        async with self.runtime.session() as session:
            ids = (
                await session.scalars(
                    select(Fill.position_id)
                    .join(Position, Position.id == Fill.position_id)
                    .where(
                        Position.account == context.account,
                        Position.network == context.network,
                        Position.status == PositionStatus.CLOSED,
                        Fill.commission_quote.is_(None),
                    )
                    .distinct()
                )
            ).all()
        for position_id in ids:
            if position_id is None:
                continue
            async with self.runtime.session() as session, session.begin():
                position = await lock_position(session, position_id)
                await self.finalizer.revalue(session, position)

    async def _open(self) -> list[Position]:
        context = self.runtime.context
        if context is None:
            return []
        async with self.runtime.session() as session:
            return list(
                (
                    await session.scalars(
                        select(Position).where(
                            Position.account == context.account,
                            Position.network == context.network,
                            Position.status != PositionStatus.CLOSED,
                        )
                    )
                ).all()
            )

    async def _reserved(self) -> list[uuid.UUID]:
        context = self.runtime.context
        if context is None:
            return []
        async with self.runtime.session() as session:
            return list(
                (
                    await session.scalars(
                        select(ExecutionIntent.signal_id).where(
                            ExecutionIntent.account == context.account,
                            ExecutionIntent.network == context.network,
                            ExecutionIntent.state == IntentState.RESERVED,
                        )
                    )
                ).all()
            )


def build_execution(
    runtime: ExecutionRuntime, hub: StreamHub | None
) -> ExecutionService:
    """The production wiring: public market-data clients, one per network."""
    clients: dict[str, ExchangeRest] = {}

    def market(network: str) -> ExchangeRest:
        if network not in clients:
            if network not in ("live", "testnet"):
                raise ValueError(f"unknown network: {network!r}")
            clients[network] = ExchangeRest.connect_public(
                "live" if network == "live" else "testnet"
            )
        return clients[network]

    async def fetch(network: str, symbol: str) -> Any:
        return await market(network).exchange_info(symbol)

    async def price(network: str, symbol: str) -> Decimal | None:
        ticker = await market(network).ticker_price(symbol)
        value = getattr(getattr(ticker, "actual_instance", ticker), "price", None)
        return Decimal(value) if value else None

    async def commission_price(asset: str, at: datetime) -> Decimal | None:
        """The asset's USDT price in the minute of the fill (Q19)."""
        context = runtime.context
        if context is None:
            return None
        start = int(at.timestamp() * 1000) // 60_000 * 60_000
        try:
            rows = await market(context.network).klines(
                symbol=f"{asset}USDT", interval="1m", start_time=start, limit=1
            )
        except Exception:  # noqa: BLE001 - no price: estimated at close
            return None
        return Decimal(str(rows[0][4])) if rows else None

    def clock() -> datetime:
        return datetime.now(UTC)

    filters = FilterCache(fetch)
    admission = Admission(
        runtime,
        quotes=lambda network: QuoteProvider(market(network)),
        filters=filters,
        prices=price,
    )
    runtime.set_admission_wake(admission.wake)
    return ExecutionService(
        runtime,
        admission=admission,
        market=market,
        hub=hub,
        filters=filters,
        clock=clock,
        commission_price=commission_price,
    )


__all__ = ["ExecutionService", "build_execution"]
