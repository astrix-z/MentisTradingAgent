"""A whole execution stack on a throwaway database and the fake exchange.

Shared by the order-manager, position-worker, reconciler and crash-matrix
tests. Everything above the fake exchange is production code; restarting the
"process" means building a new :class:`Stack` over the same database and the
same exchange (see :meth:`World.stack`).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from fake_exchange import FakeExchange, SymbolRules
from pg import scratch_database
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agent.core.migrate import migrate
from agent.exchange.budget import RequestBudget
from agent.exchange.rest import ExchangeRest
from agent.exchange.trade import TradeClient
from agent.execution.admission import Admission, record_signal
from agent.execution.filters import FilterCache
from agent.execution.ledger import CommissionConverter, Finalizer, PositionApplier
from agent.execution.models import (
    ExecutionIntent,
    OrderAttempt,
    Position,
    SignalInbox,
)
from agent.execution.orders import OrderManager
from agent.execution.quotes import QuoteProvider
from agent.execution.runtime import ExecutionContext, ExecutionRuntime
from agent.execution.settings import ExecutionSettings
from agent.strategies.base import EXIT_POLICY_VERSION, ExitPolicy, Signal

CONTEXT = ExecutionContext("1001", "live")
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")


class Clock:
    """Wall-clock based, but movable: ``advance`` shifts every reader."""

    def __init__(self) -> None:
        self.offset = timedelta(0)

    def now(self) -> datetime:
        return datetime.now(UTC) + self.offset

    def ms(self) -> int:
        return int(time.time() * 1000 + self.offset.total_seconds() * 1000)

    def advance(self, seconds: float) -> None:
        self.offset += timedelta(seconds=seconds)

    async def sleep(self, seconds: float) -> None:
        """Test sleep: moves the clock instead of waiting, then yields."""
        self.advance(seconds)
        await asyncio.sleep(0)


def base_of(symbol: str) -> str:
    return symbol.removesuffix("USDT")


@dataclass
class Stack:
    """One "process": runtime, admission, applier and order manager."""

    runtime: ExecutionRuntime
    admission: Admission
    applier: PositionApplier
    finalizer: Finalizer
    orders: OrderManager
    rest: ExchangeRest


@dataclass
class World:
    db: AsyncEngine
    url: str
    fake: FakeExchange
    clock: Clock
    settings: ExecutionSettings
    stacks: list[Stack] = field(default_factory=list)

    async def stack(self, *, protect: bool = True) -> Stack:
        """Build a process over the shared database and exchange."""
        fake, clock = self.fake, self.clock
        runtime = ExecutionRuntime(
            db=self.db,
            db_url=self.url,
            schema_ready=True,
            trade_factory=lambda network: TradeClient(
                "live", fake.spot(), budget=RequestBudget()
            ),
            clock=clock.now,
        )
        rest = ExchangeRest("live", fake.spot(), RequestBudget())

        async def fetch(network: str, symbol: str) -> Any:
            return await rest.exchange_info(symbol)

        async def price(network: str, symbol: str) -> Decimal | None:
            return fake.last.get(symbol)

        async def asset_price(asset: str, at: datetime) -> Decimal | None:
            return fake.bnb_price if asset == "BNB" else None

        filters = FilterCache(fetch)
        quotes = lambda network: QuoteProvider(rest, clock=clock.now)  # noqa: E731
        admission = Admission(
            runtime, quotes=quotes, filters=filters, prices=price, clock=clock.now
        )
        applier = PositionApplier(
            CommissionConverter("USDT", base_of, asset_price), base_of
        )
        finalizer = Finalizer(fallback_price=asset_price)
        orders = OrderManager(
            runtime,
            applier=applier,
            filters=filters,
            quotes=quotes,
            clock=clock.now,
            sleep=clock.sleep,
        )
        await runtime.prepare_start(
            network="live", settings=self.settings, params={"quote_asset": "USDT"}
        )
        runtime.open_admission("tripple_screen")
        stack = Stack(runtime, admission, applier, finalizer, orders, rest)
        self.stacks.append(stack)
        return stack

    async def kill(self, stack: Stack) -> None:
        """The process dies: its lock session goes, nothing else is cleaned."""
        await stack.runtime.shutdown()

    # --- data helpers ----------------------------------------------------------------

    def entry(
        self,
        symbol: str = "BTCUSDT",
        *,
        stop_ref: str = "98",
        valid_for: timedelta = timedelta(minutes=15),
    ) -> Signal:
        now = self.clock.now()
        policy = ExitPolicy(
            version=EXIT_POLICY_VERSION,
            stop_ref=Decimal(stop_ref),
            swing_low_ref=Decimal(stop_ref),
            min_stop_distance_pct=Decimal(1),
            stop_buffer_pct=Decimal("0.1"),
            tp_pct=Decimal(10),
            trail_interval="15m",
            trail_n=2,
            trail_cap_pct=Decimal(4),
            max_hold_hours=Decimal(8),
            cooldown_minutes=60,
        )
        signal = Signal(
            type="ENTRY",
            symbol=symbol,
            emitted_at=now,
            reason="entry",
            epoch_id=f"{symbol}:{uuid.uuid4().hex[:8]}",
            entry_ref=self.fake.last[symbol],
            swing_low_ref=Decimal(stop_ref),
            valid_until=now + valid_for,
            exit_policy=policy,
        )
        return signal.identified(
            account_uid="1001", network="live", strategy="tripple_screen"
        )

    async def reserve(
        self, stack: Stack, symbol: str = "BTCUSDT", **options: Any
    ) -> uuid.UUID:
        signal = self.entry(symbol, **options)
        async with stack.runtime.session() as session:
            await record_signal(
                session, signal, context=CONTEXT, strategy="tripple_screen"
            )
        decisions = await stack.admission.drain()
        [decision] = [d for d in decisions if str(d.signal_id) == signal.signal_id]
        assert decision.reason is None, decision.reason
        return decision.signal_id

    async def intent(self, stack: Stack, signal_id: uuid.UUID) -> ExecutionIntent:
        async with stack.runtime.session() as session:
            row = await session.get(ExecutionIntent, signal_id)
            assert row is not None
            return row

    async def position_of(self, stack: Stack, signal_id: uuid.UUID) -> Position | None:
        async with stack.runtime.session() as session:
            return (
                await session.scalars(
                    select(Position).where(Position.signal_id == signal_id)
                )
            ).first()

    async def attempts(self, stack: Stack, signal_id: uuid.UUID) -> list[OrderAttempt]:
        async with stack.runtime.session() as session:
            return list(
                (
                    await session.scalars(
                        select(OrderAttempt)
                        .where(OrderAttempt.signal_id == signal_id)
                        .order_by(OrderAttempt.created_at)
                    )
                ).all()
            )

    async def inbox_count(self, stack: Stack) -> int:
        async with stack.runtime.session() as session:
            return len((await session.scalars(select(SignalInbox))).all())


@asynccontextmanager
async def world(
    *, usdt: str = "1000", commission_asset: str = "BNB", **settings: Any
) -> AsyncIterator[World]:
    async with scratch_database() as url:
        db = create_async_engine(url)
        await migrate(db)
        clock = Clock()
        fake = FakeExchange(
            uid=1001,
            balances={"USDT": Decimal(usdt), "BNB": Decimal(1)},
            commission_asset=commission_asset,
            clock=clock.ms,
        )
        for symbol in SYMBOLS:
            fake.list_symbol(
                SymbolRules(symbol=symbol, base=base_of(symbol)), Decimal(100)
            )
        state = World(
            db=db,
            url=url,
            fake=fake,
            clock=clock,
            settings=ExecutionSettings(enabled=True, **settings),
        )
        try:
            yield state
        finally:
            for stack in state.stacks:
                await stack.runtime.shutdown()
            await db.dispose()


def run(scenario: Callable[[World], Awaitable[None]], **options: Any) -> None:
    async def wrapper() -> None:
        async with world(**options) as state:
            await scenario(state)

    asyncio.run(wrapper())
