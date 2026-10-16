"""EXEC-06: durable inbox and admission — epochs, valid_until, reserves, limits.

PostgreSQL for the inbox and reservations, the fake exchange for account,
quotes and rules, the production trade client in between.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from fake_exchange import FakeExchange, SymbolRules
from pg import scratch_database
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agent.core.migrate import migrate
from agent.engine import Engine
from agent.exchange.budget import RequestBudget
from agent.exchange.rest import ExchangeRest
from agent.exchange.trade import TradeClient
from agent.execution.admission import Admission, record_signal, release_intent
from agent.execution.filters import FilterCache
from agent.execution.models import (
    ExecutionIntent,
    IntentState,
    Position,
    PositionStatus,
    RiskState,
    SignalInbox,
    SymbolCooldown,
)
from agent.execution.quotes import QuoteProvider
from agent.execution.runtime import ExecutionContext, ExecutionRuntime
from agent.execution.settings import ExecutionSettings
from agent.strategies.base import EXIT_POLICY_VERSION, ExitPolicy, Signal

SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
CONTEXT = ExecutionContext("1001", "live")


def wall_clock() -> int:
    return int(time.time() * 1000)


@dataclass
class World:
    db: AsyncEngine
    url: str
    fake: FakeExchange
    runtime: ExecutionRuntime

    def admission(self) -> Admission:
        rest = ExchangeRest("live", self.fake.spot(), RequestBudget())

        async def fetch(network: str, symbol: str) -> Any:
            return await rest.exchange_info(symbol)

        async def price(network: str, symbol: str) -> Decimal | None:
            return self.fake.last.get(symbol)

        return Admission(
            self.runtime,
            quotes=lambda network: QuoteProvider(rest),
            filters=FilterCache(fetch),
            prices=price,
        )


@asynccontextmanager
async def world(usdt: str = "100", **settings: Any) -> AsyncIterator[World]:
    async with scratch_database() as url:
        db = create_async_engine(url)
        await migrate(db)
        fake = FakeExchange(
            uid=1001,
            balances={"USDT": Decimal(usdt), "BNB": Decimal(1)},
            clock=wall_clock,
        )
        for symbol in SYMBOLS:
            fake.list_symbol(
                SymbolRules(symbol=symbol, base=symbol.removesuffix("USDT")),
                Decimal(100),
            )
        runtime = ExecutionRuntime(
            db=db,
            db_url=url,
            schema_ready=True,
            trade_factory=lambda network: TradeClient(
                "live", fake.spot(), budget=RequestBudget()
            ),
        )
        runtime.settings = ExecutionSettings(enabled=True, **settings)
        await runtime.prepare_start(network="live", params={"quote_asset": "USDT"})
        runtime.open_admission("tripple_screen")
        try:
            yield World(db, url, fake, runtime)
        finally:
            await runtime.shutdown()
            await db.dispose()


def run(scenario: Callable[[World], Awaitable[None]], **options: Any) -> None:
    async def wrapper() -> None:
        async with world(**options) as state:
            await scenario(state)

    asyncio.run(wrapper())


def entry(
    symbol: str = "BTCUSDT",
    epoch: str | None = None,
    *,
    valid_for: timedelta = timedelta(minutes=15),
    stop_ref: str = "98",
) -> Signal:
    now = datetime.now(UTC)
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
        epoch_id=epoch or f"{symbol}:{uuid.uuid4().hex[:8]}",
        entry_ref=Decimal(100),
        swing_low_ref=Decimal(stop_ref),
        valid_until=now + valid_for,
        exit_policy=policy,
    )
    return signal.identified(
        account_uid="1001", network="live", strategy="tripple_screen"
    )


async def inbox(state: World, *signals: Signal) -> list[bool]:
    results = []
    for signal in signals:
        async with state.runtime.session() as session:
            results.append(
                await record_signal(
                    session, signal, context=CONTEXT, strategy="tripple_screen"
                )
            )
    return results


async def intents(state: World) -> dict[str, tuple[str, str | None, Decimal]]:
    async with state.runtime.session() as session:
        rows = (await session.scalars(select(ExecutionIntent))).all()
    return {
        r.symbol + ":" + str(r.signal_id)[:4]: (r.state, r.reason, r.quote_reserved)
        for r in rows
    }


async def states(state: World) -> list[tuple[str, str, str | None]]:
    async with state.runtime.session() as session:
        rows = (
            await session.scalars(
                select(ExecutionIntent).order_by(ExecutionIntent.created_at)
            )
        ).all()
    return [(r.symbol, r.state, r.reason) for r in rows]


async def add(state: World, *rows: Any) -> None:
    async with state.runtime.session() as session:
        session.add_all(rows)
        await session.commit()


def position(symbol: str, qty: str, status: str = PositionStatus.PROTECTED) -> Position:
    return Position(
        id=uuid.uuid4(),
        account="1001",
        network="live",
        symbol=symbol,
        strategy="tripple_screen",
        signal_id=uuid.uuid4(),
        status=status,
        exit_policy={},
        quantity=Decimal(qty),
    )


# --- the happy path ------------------------------------------------------------------


def test_entry_is_reserved_with_its_final_quote_cost() -> None:
    async def scenario(state: World) -> None:
        await inbox(state, entry())
        [decision] = await state.admission().drain()
        assert decision.state == IntentState.RESERVED and decision.plan is not None
        async with state.runtime.session() as session:
            intent = (await session.scalars(select(ExecutionIntent))).one()
            signal = (await session.scalars(select(SignalInbox))).one()
        assert intent.quote_reserved == decision.plan.quote_cost
        assert intent.quantity == decision.plan.quantity
        assert signal.processed_at is not None
        assert await state.admission().drain() == []  # decided once

    run(scenario)


# --- slots and money --------------------------------------------------------------------


def test_two_processes_competing_for_the_last_slot_admit_one() -> None:
    async def scenario(state: World) -> None:
        # Two slots (max_symbols=2), one taken by a position; money for both.
        await add(state, position("BTCUSDT", "0.1"))
        await inbox(state, entry("ETHUSDT"), entry("SOLUSDT"))
        first, second = state.admission(), state.admission()
        signals = await _pending(state)
        results = await asyncio.gather(
            first.decide(signals[0], CONTEXT), second.decide(signals[1], CONTEXT)
        )
        assert sorted(r.state for r in results) == [
            IntentState.REJECTED,
            IntentState.RESERVED,
        ]
        rejected = next(r for r in results if r.state == IntentState.REJECTED)
        assert rejected.reason == "slots_full"

    run(scenario, usdt="200", max_symbols=2)


def test_slots_free_but_money_for_only_one_final_size() -> None:
    async def scenario(state: World) -> None:
        # Equity 14 + 20 in a position = 34 → 3 slots of 10.2; free USDT is 14.
        state.fake.free["USDT"] = Decimal(14)
        await add(state, position("BTCUSDT", "0.2"))
        await inbox(state, entry("ETHUSDT"), entry("SOLUSDT"))
        await state.admission().drain()
        result = await states(state)
        assert [s for _, s, _ in result] == [IntentState.RESERVED, IntentState.REJECTED]
        assert result[1][2] == "insufficient_balance"

    run(scenario)


# --- one symbol, one entry ----------------------------------------------------------------


def test_second_epoch_of_a_symbol_waits_for_the_first() -> None:
    async def scenario(state: World) -> None:
        await inbox(state, entry("BTCUSDT"), entry("BTCUSDT"))
        await state.admission().drain()
        assert await states(state) == [
            ("BTCUSDT", IntentState.RESERVED, None),
            ("BTCUSDT", IntentState.REJECTED, "entry_pending"),
        ]

    run(scenario)


def test_released_entry_frees_its_slot_exactly_once_even_after_a_crash() -> None:
    async def scenario(state: World) -> None:
        await inbox(state, entry("BTCUSDT"))
        [first] = await state.admission().drain()

        async def release(crash: bool) -> bool:
            async with state.runtime.session() as session, session.begin():
                intent = await session.get(
                    ExecutionIntent, first.signal_id, with_for_update=True
                )
                assert intent is not None
                done = await release_intent(session, intent, reason="fok_expired")
                if crash:
                    raise RuntimeError("crash before commit")
                return done

        with pytest.raises(RuntimeError):
            await release(crash=True)
        assert (await states(state))[0][1] == IntentState.RESERVED
        assert await release(crash=False) is True
        assert await release(crash=False) is False  # replay
        await inbox(state, entry("BTCUSDT"), entry("ETHUSDT"))
        await state.admission().drain()
        assert [s for _, s, _ in await states(state)] == [
            IntentState.NO_FILL,
            IntentState.RESERVED,
            IntentState.RESERVED,
        ]
        async with state.runtime.session() as session:
            released = await session.get(ExecutionIntent, first.signal_id)
        assert released is not None and released.quote_reserved == 0

    run(scenario)


def test_cooldown_and_unprotected_positions_block() -> None:
    async def scenario(state: World) -> None:
        await add(
            state,
            SymbolCooldown(
                account="1001",
                network="live",
                symbol="ETHUSDT",
                cooldown_until=datetime.now(UTC) + timedelta(minutes=30),
                position_id=uuid.uuid4(),
            ),
        )
        await inbox(state, entry("ETHUSDT"))
        await state.admission().drain()
        await add(state, position("SOLUSDT", "0.01", PositionStatus.UNPROTECTED))
        await inbox(state, entry("BTCUSDT"))
        await state.admission().drain()
        assert [r for _, _, r in await states(state)] == [
            "cooldown",
            "unprotected_position",
        ]

    run(scenario)


# --- time ------------------------------------------------------------------------------------


def test_signal_past_valid_until_expires_in_the_queue() -> None:
    async def scenario(state: World) -> None:
        await inbox(
            state,
            entry(valid_for=timedelta(0)),
            entry("ETHUSDT", valid_for=timedelta(seconds=-5)),
        )
        await state.admission().drain()
        assert [s for _, s, _ in await states(state)] == [IntentState.EXPIRED] * 2

    run(scenario)


def test_daily_losing_trade_limit_pauses_until_midnight() -> None:
    async def scenario(state: World) -> None:
        today = datetime.now(UTC).date()
        await add(
            state,
            RiskState(account="1001", network="live", utc_date=today, losing_trades=5),
            RiskState(
                account="1001",
                network="live",
                utc_date=today - timedelta(days=1),
                losing_trades=9,
            ),
        )
        await inbox(state, entry())
        await state.admission().drain()
        assert (await states(state))[0][2] == "daily_loss_limit"
        assert "losing trades today" in state.runtime.alerts["daily_limit"]
        async with state.runtime.session() as session:
            row = await session.get(RiskState, ("1001", "live", today))
            assert row is not None
            row.utc_date = date(2000, 1, 1)
            await session.commit()
        await inbox(state, entry("ETHUSDT"))
        await state.admission().drain()
        assert (await states(state))[1][1] == IntentState.RESERVED
        assert "daily_limit" not in state.runtime.alerts

    run(scenario)


# --- modes and prices ---------------------------------------------------------------------


def test_execution_off_records_the_decision_and_sends_nothing() -> None:
    async def scenario(state: World) -> None:
        state.runtime.settings = ExecutionSettings(enabled=False)
        await inbox(state, entry())
        await state.admission().drain()
        assert await states(state) == [
            ("BTCUSDT", IntentState.REJECTED, "execution_off")
        ]
        assert state.fake.orders == {}

    run(scenario)


def test_price_already_at_the_stop_reference_is_refused() -> None:
    async def scenario(state: World) -> None:
        await inbox(state, entry(stop_ref="100"))
        await state.admission().drain()
        assert (await states(state))[0][2] == "stop_above_price"

    run(scenario)


# --- the engine and the inbox -------------------------------------------------------------------


def test_a_consumed_epoch_is_never_emitted_twice() -> None:
    async def scenario(state: World) -> None:
        redis = Mock(publish=AsyncMock(), set=AsyncMock())
        engine = Engine(redis)
        engine.execution = state.runtime
        engine._network = "live"
        engine._strategy_name = "tripple_screen"
        raw = replace(entry(epoch="BTCUSDT:42"), signal_id=str(uuid.uuid4()))
        await engine._on_signal(raw)
        again = replace(
            raw, signal_id=str(uuid.uuid4())
        )  # the same epoch after restart
        await engine._on_signal(again)
        async with state.runtime.session() as session:
            count = await session.scalar(select(func.count()).select_from(SignalInbox))
        assert count == 1
        assert redis.publish.await_count == 1
        published = json.loads(redis.publish.await_args.args[1])
        assert published["signal_id"] == entry(epoch="BTCUSDT:42").signal_id

    run(scenario)


def test_inbox_failure_fails_the_emit_so_the_strategy_retries() -> None:
    async def scenario(state: World) -> None:
        redis = Mock(publish=AsyncMock(), set=AsyncMock())
        engine = Engine(redis)
        engine.execution = state.runtime
        engine._network = "live"
        engine._strategy_name = "tripple_screen"
        broken = Mock(side_effect=RuntimeError("database down"))
        state.runtime.session = broken  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="database down"):
            await engine._on_signal(entry())
        redis.publish.assert_not_awaited()

    run(scenario)


async def _pending(state: World) -> list[SignalInbox]:
    async with state.runtime.session() as session:
        return list(
            (
                await session.scalars(
                    select(SignalInbox)
                    .where(SignalInbox.processed_at.is_(None))
                    .order_by(SignalInbox.received_at)
                )
            ).all()
        )
