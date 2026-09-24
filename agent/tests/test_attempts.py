"""EXEC-08a: attempts, client ids and UNKNOWN — no second BUY, ever."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fake_exchange import FakeExchange, SymbolRules
from pg import scratch_database
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agent.core.migrate import migrate
from agent.exchange.budget import Priority, RequestBudget
from agent.exchange.trade import Outcome, TradeCallError, TradeClient
from agent.execution.attempts import (
    AttemptJournal,
    ClientIdOverflow,
    Found,
    Role,
    client_id,
)
from agent.execution.models import AttemptState, OrderAttempt, SignalInbox
from agent.execution.runtime import ExecutionContext

CONTEXT = ExecutionContext("1001", "live")
SIGNAL = uuid.UUID("0123456789abcdef0123456789abcdef")


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 24, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@dataclass
class World:
    sessions: async_sessionmaker[AsyncSession]
    fake: FakeExchange
    clock: Clock
    journal: AttemptJournal

    def trade(self) -> TradeClient:
        """A fresh client: what a restarted runtime would build."""
        return TradeClient("live", self.fake.spot(), budget=RequestBudget())


@asynccontextmanager
async def world() -> AsyncIterator[World]:
    async with scratch_database() as url:
        engine = create_async_engine(url)
        await migrate(engine)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            session.add(
                SignalInbox(
                    signal_id=SIGNAL,
                    account="1001",
                    network="live",
                    strategy="tripple_screen",
                    symbol="BTCUSDT",
                    type="ENTRY",
                    epoch_id="BTCUSDT:1",
                    emitted_at=datetime.now(UTC),
                    payload={},
                )
            )
            await session.commit()
        fake = FakeExchange(balances={"USDT": Decimal(1000), "BNB": Decimal(1)})
        fake.list_symbol(SymbolRules(symbol="BTCUSDT", base="BTC"), Decimal(100))
        clock = Clock()
        try:
            yield World(sessions, fake, clock, AttemptJournal(sessions, clock=clock))
        finally:
            await engine.dispose()


def run(scenario: Callable[[World], Awaitable[None]]) -> None:
    async def wrapper() -> None:
        async with world() as state:
            await scenario(state)

    asyncio.run(wrapper())


async def prepare(state: World, sequence: int = 1) -> str:
    async with state.sessions() as session:
        attempt = await state.journal.prepare(
            session,
            context=CONTEXT,
            symbol="BTCUSDT",
            signal_id=SIGNAL,
            role=Role.ENTRY,
            sequence=sequence,
            request={"qty": Decimal("0.1"), "price": Decimal("100.3")},
        )
        await session.commit()
        return attempt.client_order_id


def buy(trade: TradeClient, cid: str) -> Callable[[], Awaitable[Any]]:
    return lambda: trade.new_order(
        symbol="BTCUSDT",
        side="BUY",
        order_type="LIMIT",
        quantity=Decimal("0.1"),
        price=Decimal("100.3"),
        time_in_force="FOK",
        client_order_id=cid,
        priority=Priority.ENTRY,
    )


def query(trade: TradeClient, cid: str) -> Callable[[], Awaitable[Any]]:
    return lambda: trade.get_order(symbol="BTCUSDT", client_order_id=cid)


async def attempt(state: World, cid: str) -> OrderAttempt:
    async with state.sessions() as session:
        row = await session.get(OrderAttempt, cid)
        assert row is not None
        return row


async def open_entries(state: World) -> list[str]:
    async with state.sessions() as session:
        rows = await state.journal.open_of(
            session, signal_id=SIGNAL, roles=(Role.ENTRY,)
        )
        return [r.client_order_id for r in rows]


# --- ids ---------------------------------------------------------------------------


def test_client_ids_fit_36_characters_or_are_refused() -> None:
    assert client_id(SIGNAL, Role.ENTRY, 1) == "mta-0123456789abcdef01234567-e1"
    assert len(client_id(SIGNAL, Role.LIST, 99999)) == 36
    with pytest.raises(ClientIdOverflow):
        client_id(SIGNAL, Role.LIST, 100000)
    with pytest.raises(ValueError):
        client_id(SIGNAL, Role.EXIT, 0)


# --- the protocol ----------------------------------------------------------------------


def test_acked_attempt_keeps_the_exchange_answer() -> None:
    async def scenario(state: World) -> None:
        cid = await prepare(state)
        result = await state.journal.send(cid, buy(state.trade(), cid))
        row = await attempt(state, cid)
        assert row.state == AttemptState.ACKED and row.exchange_status == "FILLED"
        assert row.order_id == result.data.order_id
        assert row.executed_qty == Decimal("0.1")
        assert await open_entries(state) == []

    run(scenario)


def test_proven_refusals_are_rejected_not_unknown() -> None:
    async def scenario(state: World) -> None:
        for kind, code in (("reject", -2010), ("rate_limit", None)):
            state.fake.fault("new_order", kind, code=code, retry_after=1)
            cid = await prepare(state, sequence=len(state.fake.calls) + 1)
            with pytest.raises(TradeCallError):
                await state.journal.send(cid, buy(state.trade(), cid))
            assert (await attempt(state, cid)).state == AttemptState.REJECTED
        assert state.fake.orders == {}

    run(scenario)


def test_crash_between_commit_and_send_is_potentially_sent_until_proven() -> None:
    async def scenario(state: World) -> None:
        cid = await prepare(state)  # committed; the process "dies" here
        assert await open_entries(state) == [cid]
        trade = state.trade()
        assert (
            await state.journal.lookup(cid, query(trade, cid))
        ).found is Found.UNKNOWN
        assert (
            await state.journal.lookup(cid, query(trade, cid))
        ).found is Found.UNKNOWN
        state.clock.now += timedelta(seconds=66)  # recvWindow 5 s + 60 s margin
        assert (
            await state.journal.lookup(cid, query(trade, cid))
        ).found is Found.ABSENT
        assert state.fake.calls_to("new_order") == 0

    run(scenario)


def test_accepted_then_crash_before_the_answer_is_found_not_rebought() -> None:
    async def scenario(state: World) -> None:
        cid = await prepare(state)
        state.fake.fault("new_order", "timeout_after")
        with pytest.raises(TradeCallError) as caught:
            await state.journal.send(cid, buy(state.trade(), cid))
        assert caught.value.outcome is Outcome.UNKNOWN
        assert (await attempt(state, cid)).state == AttemptState.UNKNOWN
        restarted = state.trade()
        state.fake.fault("get_order", "not_found")  # still propagating
        assert (
            await state.journal.lookup(cid, query(restarted, cid))
        ).found is Found.UNKNOWN
        found = await state.journal.lookup(cid, query(restarted, cid))
        assert found.found is Found.PRESENT and found.data.status == "FILLED"
        await state.journal.resolve(cid, AttemptState.RESOLVED, data=found.data)
        row = await attempt(state, cid)
        assert row.state == AttemptState.RESOLVED and row.executed_qty == Decimal("0.1")
        assert len(state.fake.orders_of("BTCUSDT", "BUY")) == 1

    run(scenario)


def test_cancelled_wait_with_a_live_request_is_unknown() -> None:
    async def scenario(state: World) -> None:
        cid = await prepare(state)
        entered, release = state.fake.gate("new_order")
        waiting = asyncio.create_task(state.journal.send(cid, buy(state.trade(), cid)))
        await asyncio.to_thread(entered.wait, 5)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        row = await attempt(state, cid)
        assert row.state == AttemptState.UNKNOWN
        assert row.error == {"outcome": "unknown", "message": "wait cancelled"}
        release.set()
        for _ in range(100):
            if state.fake.orders:
                break
            await asyncio.sleep(0.01)
        assert await open_entries(state) == [cid]  # still blocks the logical BUY
        found = await state.journal.lookup(cid, query(state.trade(), cid))
        assert found.found is Found.PRESENT
        assert len(state.fake.orders_of("BTCUSDT", "BUY")) == 1

    run(scenario)


def test_sequences_count_per_role() -> None:
    async def scenario(state: World) -> None:
        await prepare(state, 1)
        await prepare(state, 2)
        async with state.sessions() as session:
            assert (
                await state.journal.next_sequence(
                    session, signal_id=SIGNAL, role=Role.ENTRY
                )
                == 3
            )
            assert (
                await state.journal.next_sequence(
                    session, signal_id=SIGNAL, role=Role.LIST
                )
                == 1
            )

    run(scenario)
