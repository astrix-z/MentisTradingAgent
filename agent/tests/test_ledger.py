"""EXEC-15: PositionApplier and Finalizer — one writer, exactly once.

PostgreSQL throughout: row locks and ``ON CONFLICT`` are what make replays and
concurrent appliers safe, so they are tested for real.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from pg import scratch_database
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from agent.core.migrate import migrate
from agent.execution.ledger import (
    CommissionConverter,
    FillRecord,
    Finalizer,
    PositionApplier,
    lock_position,
    losing_trades_today,
    transaction_time,
)
from agent.execution.models import (
    ExecutionEvent,
    ExecutionIntent,
    Fill,
    IntentState,
    Position,
    PositionStatus,
    RiskState,
    SignalInbox,
    SymbolCooldown,
)
from agent.execution.outcome import ExitMechanism, ExitReason
from agent.strategies.base import EXIT_POLICY_VERSION, ExitPolicy

T0 = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
POLICY = ExitPolicy(
    version=EXIT_POLICY_VERSION,
    stop_ref=Decimal(99),
    swing_low_ref=Decimal(99),
    min_stop_distance_pct=Decimal(1),
    stop_buffer_pct=Decimal("0.1"),
    tp_pct=Decimal(10),
    trail_interval="15m",
    trail_n=2,
    trail_cap_pct=Decimal(4),
    max_hold_hours=Decimal(8),
    cooldown_minutes=60,
)
BNB = {"price": Decimal(600)}


async def bnb_price(asset: str, at: datetime) -> Decimal | None:
    return BNB["price"] if asset == "BNB" else None


def base_of(symbol: str) -> str:
    return symbol.removesuffix("USDT")


APPLIER = PositionApplier(CommissionConverter("USDT", base_of, bnb_price), base_of)
FINALIZER = Finalizer(fallback_price=bnb_price)


def fill(
    trade_id: int,
    *,
    order_id: int = 1,
    side: str = "BUY",
    qty: str = "0.1",
    price: str = "100",
    commission: str = "0.00001667",
    asset: str = "BNB",
    at: datetime = T0,
    source: str = "response",
) -> FillRecord:
    return FillRecord(
        symbol="BTCUSDT",
        trade_id=trade_id,
        order_id=order_id,
        side=side,
        price=Decimal(price),
        quantity=Decimal(qty),
        quote_quantity=Decimal(qty) * Decimal(price),
        commission=Decimal(commission),
        commission_asset=asset,
        is_maker=False,
        time=at,
        source=source,
    )


@asynccontextmanager
async def database() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with scratch_database() as url:
        engine: AsyncEngine = create_async_engine(url)
        await migrate(engine)
        try:
            yield async_sessionmaker(engine, expire_on_commit=False)
        finally:
            await engine.dispose()


def run(
    scenario: Callable[[async_sessionmaker[AsyncSession]], Awaitable[None]],
) -> None:
    async def wrapper() -> None:
        async with database() as sessions:
            await scenario(sessions)

    asyncio.run(wrapper())


async def reserved_intent(sessions: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    signal_id = uuid.uuid4()
    async with sessions() as session:
        session.add(
            SignalInbox(
                signal_id=signal_id,
                account="1001",
                network="live",
                strategy="tripple_screen",
                symbol="BTCUSDT",
                type="ENTRY",
                epoch_id=f"BTCUSDT:{signal_id.hex[:6]}",
                emitted_at=T0,
                payload={},
            )
        )
        await session.flush()
        session.add(
            ExecutionIntent(
                signal_id=signal_id,
                account="1001",
                network="live",
                strategy="tripple_screen",
                symbol="BTCUSDT",
                state=IntentState.SUBMITTED,
                quote_reserved=Decimal("10.03"),
            )
        )
        await session.commit()
    return signal_id


async def open_position(
    sessions: async_sessionmaker[AsyncSession], *fills: FillRecord
) -> uuid.UUID:
    signal_id = await reserved_intent(sessions)
    async with sessions() as session:
        intent = await session.get(ExecutionIntent, signal_id, with_for_update=True)
        assert intent is not None
        result = await APPLIER.open_from_entry(
            session, intent, exit_policy=POLICY, fills=fills or [fill(1)]
        )
        await session.commit()
        return result.position.id


async def apply(
    sessions: async_sessionmaker[AsyncSession],
    position_id: uuid.UUID,
    *fills: FillRecord,
) -> list[FillRecord]:
    async with sessions() as session:
        position = await lock_position(session, position_id)
        result = await APPLIER.apply(session, position, fills)
        await session.commit()
        return result.new_fills


async def close(
    sessions: async_sessionmaker[AsyncSession],
    position_id: uuid.UUID,
    *,
    crash: bool = False,
) -> Any:
    async with sessions() as session:
        position = await lock_position(session, position_id)
        outcome = await FINALIZER.close(
            session, position, reason=ExitReason.TP, mechanism=ExitMechanism.EXCHANGE_TP
        )
        if crash:
            raise RuntimeError("crash before commit")
        await session.commit()
        return outcome


async def get(
    sessions: async_sessionmaker[AsyncSession], position_id: uuid.UUID
) -> Position:
    async with sessions() as session:
        position = await session.get(Position, position_id)
        assert position is not None
        return position


async def events(
    sessions: async_sessionmaker[AsyncSession], position_id: uuid.UUID
) -> list[tuple[int | None, str]]:
    async with sessions() as session:
        rows = await session.execute(
            select(ExecutionEvent.aggregate_seq, ExecutionEvent.type)
            .where(ExecutionEvent.position_id == position_id)
            .order_by(ExecutionEvent.id)
        )
        return [(seq, kind) for seq, kind in rows.all()]


# --- applying fills ---------------------------------------------------------------


def test_filled_entry_opens_the_position_and_pays_off_the_reserve() -> None:
    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        position_id = await open_position(sessions, fill(1, price="100.2"))
        position = await get(sessions, position_id)
        assert position.status == PositionStatus.PROTECTING
        assert position.unprotected_since is not None
        assert position.quantity == Decimal("0.1")
        assert position.fill_vwap == Decimal("100.2")
        assert position.commission_quote == Decimal("0.00001667") * 600
        assert position.opened_at == T0
        async with sessions() as session:
            intent = (await session.scalars(select(ExecutionIntent))).one()
        assert intent.state == IntentState.FILLED
        assert intent.quote_reserved == 0 and intent.position_id == position_id
        assert await events(sessions, position_id) == [
            (1, "position_opened"),
            (2, "fills_applied"),
        ]

    run(scenario)


def test_replayed_and_re_sourced_fills_count_once() -> None:
    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        position_id = await open_position(sessions, fill(1))
        assert await apply(sessions, position_id, fill(1)) == []
        assert await apply(sessions, position_id, fill(1, source="my_trades")) == []
        new = await apply(sessions, position_id, fill(1), fill(2, qty="0.05"))
        assert [f.trade_id for f in new] == [2]
        position = await get(sessions, position_id)
        assert position.bought_qty == Decimal("0.15")
        assert await events(sessions, position_id) == [
            (1, "position_opened"),
            (2, "fills_applied"),
            (3, "fills_applied"),
        ]

    run(scenario)


def test_concurrent_appliers_of_the_same_fill_book_it_once() -> None:
    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        position_id = await open_position(sessions, fill(1))
        sell = fill(9, order_id=2, side="SELL", qty="0.04", price="110")
        results = await asyncio.gather(
            *(apply(sessions, position_id, sell) for _ in range(4))
        )
        assert sorted(len(r) for r in results) == [0, 0, 0, 1]
        position = await get(sessions, position_id)
        assert position.sold_qty == Decimal("0.04")
        assert position.quantity == Decimal("0.06")

    run(scenario)


def test_base_commission_reduces_the_quantity_and_lagging_history_is_not_proven() -> (
    None
):
    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        first = fill(1, qty="0.06", commission="0.00006", asset="BTC")
        position_id = await open_position(sessions, first)
        async with sessions() as session:
            position = await lock_position(session, position_id)
            # The exchange says the BUY executed 0.1; only 0.06 is booked so far.
            assert not await APPLIER.proven(session, position, {1: Decimal("0.1")})
        second = fill(2, qty="0.04", commission="0.00004", asset="BTC")
        await apply(sessions, position_id, second)
        async with sessions() as session:
            position = await lock_position(session, position_id)
            assert await APPLIER.proven(session, position, {1: Decimal("0.1")})
        position = await get(sessions, position_id)
        assert position.quantity == Decimal("0.0999")  # 0.1 bought − 0.0001 fee

    run(scenario)


# --- closing ---------------------------------------------------------------------


async def sold_out(
    sessions: async_sessionmaker[AsyncSession], price: str = "110", at: datetime = T0
) -> uuid.UUID:
    position_id = await open_position(sessions, fill(1))
    await apply(
        sessions,
        position_id,
        fill(
            5,
            order_id=2,
            side="SELL",
            qty="0.1",
            price=price,
            at=at + timedelta(hours=1),
        ),
    )
    return position_id


def test_close_happens_exactly_once_even_after_a_crash() -> None:
    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        position_id = await sold_out(sessions)
        with pytest.raises(RuntimeError, match="crash"):
            await close(sessions, position_id, crash=True)
        assert (await get(sessions, position_id)).status != PositionStatus.CLOSED
        outcome = await close(sessions, position_id)
        assert outcome is not None and outcome.realized_pnl > 0
        assert await close(sessions, position_id) is None
        assert (
            await apply(sessions, position_id, fill(5, order_id=2, side="SELL")) == []
        )
        position = await get(sessions, position_id)
        assert position.status == PositionStatus.CLOSED
        assert position.closed_at == T0 + timedelta(hours=1)
        async with sessions() as session:
            cooldowns = (await session.scalars(select(SymbolCooldown))).all()
            risks = (await session.scalars(select(RiskState))).all()
        assert [c.cooldown_until for c in cooldowns] == [T0 + timedelta(hours=2)]
        assert risks == []  # a winner does not count
        kinds = await events(sessions, position_id)
        assert [seq for seq, _ in kinds] == [1, 2, 3, 4]
        assert kinds[-1][1] == "position_closed"

    run(scenario)


def test_concurrent_closes_produce_one_outcome() -> None:
    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        position_id = await sold_out(sessions, price="90")
        outcomes = await asyncio.gather(
            *(close(sessions, position_id) for _ in range(3))
        )
        assert sum(o is not None for o in outcomes) == 1
        async with sessions() as session:
            assert await losing_trades_today(session, "1001", "live", T0) == 1

    run(scenario)


def test_gross_profit_that_fees_turn_negative_is_a_losing_trade() -> None:
    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        # Bought 10 at 100, sold at 100.01: +0.001 gross, fees in BNB ≈ 0.02.
        position_id = await open_position(sessions, fill(1, commission="0.00001667"))
        await apply(
            sessions,
            position_id,
            fill(5, order_id=2, side="SELL", price="100.01", commission="0.00001667"),
        )
        outcome = await close(sessions, position_id)
        assert outcome.realized_pnl < 0 < Decimal("100.01") * Decimal("0.1") - 10
        async with sessions() as session:
            assert await losing_trades_today(session, "1001", "live", T0) == 1

    run(scenario)


def test_partial_exits_are_one_trade() -> None:
    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        position_id = await open_position(sessions, fill(1))
        await apply(
            sessions,
            position_id,
            fill(5, order_id=2, side="SELL", qty="0.04", price="95"),
        )
        await apply(
            sessions,
            position_id,
            fill(6, order_id=3, side="SELL", qty="0.06", price="96"),
        )
        outcome = await close(sessions, position_id)
        assert outcome.exit_vwap == (
            Decimal("0.04") * 95 + Decimal("0.06") * 96
        ) / Decimal("0.1")
        async with sessions() as session:
            assert await losing_trades_today(session, "1001", "live", T0) == 1

    run(scenario)


def test_a_loss_counts_on_the_day_of_its_closing_fill() -> None:
    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        late = datetime(2026, 9, 24, 23, 59, 50, tzinfo=UTC)
        position_id = await open_position(
            sessions, fill(1, at=late - timedelta(hours=2))
        )
        await apply(
            sessions, position_id, fill(5, order_id=2, side="SELL", price="90", at=late)
        )
        await close(sessions, position_id)  # detected and closed "after midnight"
        next_day = late + timedelta(minutes=5)
        async with sessions() as session:
            assert await losing_trades_today(session, "1001", "live", late) == 1
            assert await losing_trades_today(session, "1001", "live", next_day) == 0

    run(scenario)


def test_commission_without_a_price_is_estimated_at_close() -> None:
    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        BNB["price"] = None  # type: ignore[assignment]
        try:
            position_id = await open_position(sessions, fill(1))
            position = await get(sessions, position_id)
            assert position.commission_quote == 0
            await apply(
                sessions, position_id, fill(5, order_id=2, side="SELL", price="110")
            )
        finally:
            BNB["price"] = Decimal(600)
        outcome = await close(sessions, position_id)
        position = await get(sessions, position_id)
        assert position.outcome is not None and position.outcome["commission_estimated"]
        assert outcome.commission_quote == 2 * Decimal("0.00001667") * 600
        async with sessions() as session:
            assert (await session.scalars(select(Fill.commission_quote))).all() == [
                Decimal("0.00001667") * 600
            ] * 2

    run(scenario)


def test_fill_of_another_symbol_is_refused() -> None:
    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        position_id = await open_position(sessions, fill(1))
        with pytest.raises(ValueError):
            await apply(sessions, position_id, replace(fill(2), symbol="ETHUSDT"))

    run(scenario)


async def revalue(
    sessions: async_sessionmaker[AsyncSession], position_id: uuid.UUID
) -> bool:
    async with sessions() as session:
        position = await lock_position(session, position_id)
        done = await FINALIZER.revalue(session, position)
        await session.commit()
        return done


def test_unpriced_fees_keep_the_close_provisional_and_losing_until_revalued() -> None:
    # Review 2026-09-30 (EXEC-15): BNB fees, entry 100, exit 100.01, no BNB
    # price through the close: recorded as a profit, not a losing trade.
    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        BNB["price"] = None  # type: ignore[assignment]
        try:
            position_id = await open_position(
                sessions, fill(1, commission="0.00001667")
            )
            await apply(
                sessions,
                position_id,
                fill(
                    5, order_id=2, side="SELL", price="100.01", commission="0.00001667"
                ),
            )
            await close(sessions, position_id)
            position = await get(sessions, position_id)
            assert position.outcome is not None
            assert position.outcome["commission_pending"] == [1, 5]
            assert position.outcome["counted_losing"] is True
            async with sessions() as session:
                assert await losing_trades_today(session, "1001", "live", T0) == 1
            assert not await revalue(sessions, position_id)  # still no price
        finally:
            BNB["price"] = Decimal(600)
        assert await revalue(sessions, position_id)
        position = await get(sessions, position_id)
        assert position.realized_pnl is not None and position.realized_pnl < 0
        assert position.outcome is not None
        assert position.outcome["commission_pending"] == []
        assert Decimal(position.outcome["realized_pnl"]) == position.realized_pnl
        async with sessions() as session:
            assert await losing_trades_today(session, "1001", "live", T0) == 1

    run(scenario)


def test_revalued_profit_gives_back_the_provisional_losing_count() -> None:
    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        BNB["price"] = None  # type: ignore[assignment]
        try:
            position_id = await open_position(sessions, fill(1))
            await apply(
                sessions, position_id, fill(5, order_id=2, side="SELL", price="110")
            )
            await close(sessions, position_id)
            async with sessions() as session:
                assert await losing_trades_today(session, "1001", "live", T0) == 1
        finally:
            BNB["price"] = Decimal(600)
        assert await revalue(sessions, position_id)
        position = await get(sessions, position_id)
        assert position.realized_pnl is not None and position.realized_pnl > 0
        assert position.outcome is not None
        assert position.outcome["counted_losing"] is False
        async with sessions() as session:
            assert await losing_trades_today(session, "1001", "live", T0) == 0
        assert await revalue(sessions, position_id)  # nothing left: no change
        async with sessions() as session:
            assert await losing_trades_today(session, "1001", "live", T0) == 0

    run(scenario)


# --- review 2026-10-03 (EXEC-15): exchange time, corrected by the trade list ------


def test_trade_list_corrects_a_late_response_time_and_moves_the_loss_day() -> None:
    # The losing SELL executed 23:59:58 UTC; its response, without a trade
    # time of its own, was booked at 00:00:03 the next day.
    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        executed = datetime(2026, 10, 3, 23, 59, 58, tzinfo=UTC)
        read = datetime(2026, 10, 4, 0, 0, 3, tzinfo=UTC)
        position_id = await open_position(
            sessions, fill(1, at=executed - timedelta(hours=2))
        )
        await apply(
            sessions, position_id, fill(5, order_id=2, side="SELL", price="90", at=read)
        )
        await close(sessions, position_id)
        async with sessions() as session:
            assert await losing_trades_today(session, "1001", "live", read) == 1
        authoritative = fill(
            5, order_id=2, side="SELL", price="90", at=executed, source="my_trades"
        )
        async with sessions() as session:
            position = await lock_position(session, position_id)
            result = await APPLIER.apply(session, position, [authoritative])
            assert result.new_fills == [] and result.retimed == [authoritative]
            await FINALIZER.retime(session, position)
            await session.commit()
        position = await get(sessions, position_id)
        assert position.closed_at == executed
        assert position.sold_qty == Decimal("0.1")  # not applied twice
        assert position.outcome is not None
        assert position.outcome["closed_at"] == executed.isoformat()
        async with sessions() as session:
            assert await losing_trades_today(session, "1001", "live", executed) == 1
            assert await losing_trades_today(session, "1001", "live", read) == 0
            cooldown = (await session.scalars(select(SymbolCooldown))).one()
            assert cooldown.cooldown_until == executed + timedelta(minutes=60)
        async with sessions() as session:  # a second sync changes nothing
            position = await lock_position(session, position_id)
            again = await APPLIER.apply(session, position, [authoritative])
            assert again.retimed == []

    run(scenario)


def test_a_retimed_fee_without_a_price_is_pending_until_revalued() -> None:
    # Re-review 2026-10-03: the trade list corrects the SELL's time while no
    # BNB price is there for the new time; the fee kept its old-time value
    # (0.001 USDT) for good — profit +0.0005, not losing. At BNB=2 the right
    # result is a fee of 0.002 and a loss of 0.0005.
    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        read = T0 + timedelta(hours=1)
        executed = read - timedelta(seconds=5)
        position_id = await open_position(
            sessions, fill(1, commission="0", asset="USDT")
        )
        BNB["price"] = Decimal(1)
        try:
            await apply(
                sessions,
                position_id,
                fill(
                    5,
                    order_id=2,
                    side="SELL",
                    price="100.015",
                    commission="0.001",
                    at=read,
                ),
            )
            await close(sessions, position_id)
            position = await get(sessions, position_id)
            assert position.outcome is not None
            assert position.outcome["counted_losing"] is False
            authoritative = fill(
                5,
                order_id=2,
                side="SELL",
                price="100.015",
                commission="0.001",
                at=executed,
                source="my_trades",
            )
            BNB["price"] = None  # type: ignore[assignment]
            async with sessions() as session:
                position = await lock_position(session, position_id)
                result = await APPLIER.apply(session, position, [authoritative])
                assert result.retimed == [authoritative]
                await FINALIZER.retime(session, position)
                await session.commit()
            position = await get(sessions, position_id)
            assert position.commission_quote == 0  # the old value is withdrawn
            assert position.outcome is not None
            assert position.outcome["commission_pending"] == [5]
            assert position.outcome["counted_losing"] is True
            assert not await revalue(sessions, position_id)  # still no price
            BNB["price"] = Decimal(2)
            await apply(sessions, position_id, authoritative)  # replay: no-op
            assert await revalue(sessions, position_id)
            assert await revalue(sessions, position_id)  # idempotent
        finally:
            BNB["price"] = Decimal(600)
        position = await get(sessions, position_id)
        assert position.commission_quote == Decimal("0.002")
        assert position.realized_pnl == Decimal("-0.0005")
        assert position.outcome is not None
        assert position.outcome["commission_pending"] == []
        assert position.outcome["counted_losing"] is True
        async with sessions() as session:
            assert await losing_trades_today(session, "1001", "live", executed) == 1

    run(scenario)


def test_transaction_time_is_the_exchanges_not_the_readers() -> None:
    order = SimpleNamespace(transact_time=int(T0.timestamp() * 1000))
    late = T0 + timedelta(seconds=5)
    assert transaction_time(order, late) == T0
    assert transaction_time(SimpleNamespace(transact_time=None), late) == late
