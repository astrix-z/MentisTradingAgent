"""Release review 869fgbyaf: the execution barrier before a rollback to T0.

Stop + an empty command stream + ``idle`` prove that the strategy stopped,
not that execution finished: the stopped engine keeps following real
positions, and a revision without the executor would abandon them. The
barrier reads every context and refuses while anything is left.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from pg import scratch_database
from sqlalchemy.ext.asyncio import create_async_engine
from test_engine_lifecycle import setup_engine, start
from test_execution_runtime import ENABLED, USDT, World, add, position, run

from agent.execution.barrier import check, unfinished_by_context
from agent.execution.models import (
    AttemptState,
    ExecutionIntent,
    IntentState,
    OrderAttempt,
    PositionStatus,
    SignalInbox,
)
from agent.execution.runtime import ExecutionContext


def inbox(signal_id: uuid.UUID, network: str = "live") -> SignalInbox:
    return SignalInbox(
        signal_id=signal_id,
        account="1001",
        network=network,
        strategy="tripple_screen",
        symbol="BTCUSDT",
        type="ENTRY",
        epoch_id=f"BTCUSDT:{signal_id.hex[:6]}",
        emitted_at=datetime.now(UTC),
        payload={},
    )


def intent(signal_id: uuid.UUID, state: IntentState) -> ExecutionIntent:
    return ExecutionIntent(
        signal_id=signal_id,
        account="1001",
        network="live",
        strategy="tripple_screen",
        symbol="BTCUSDT",
        state=state,
    )


def attempt(signal_id: uuid.UUID, state: AttemptState) -> OrderAttempt:
    return OrderAttempt(
        client_order_id=f"mta-{signal_id.hex[:24]}-e1",
        account="1001",
        network="live",
        symbol="BTCUSDT",
        signal_id=signal_id,
        role="e",
        sequence=1,
        request={},
        state=state,
    )


def test_a_stopped_idle_engine_with_a_protected_position_is_not_finished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The review's probe: every condition of the old idle barrier holds, and
    # the position still needs the executor.
    async def scenario(state: World) -> None:
        engine, *_ = setup_engine(monkeypatch)
        runtime = state.runtime()
        engine.execution = runtime
        runtime.settings = ENABLED
        await runtime.prepare_start(network="live", params=USDT)
        await start(engine, network="live")
        await add(state, position())
        await engine.stop()
        assert engine.state == "idle"
        assert await runtime.unfinished(ExecutionContext("1001", "live")) == 1
        assert await unfinished_by_context(state.db) == {
            ("1001", "live"): {"positions": 1}
        }
        assert await check(create_async_engine(state.url)) == 1

    run(scenario)


def test_an_unknown_entry_is_unfinished_in_its_own_context() -> None:
    async def scenario(state: World) -> None:
        signal_id = uuid.uuid4()
        await add(state, inbox(signal_id))
        await add(
            state,
            intent(signal_id, IntentState.UNKNOWN),
            attempt(signal_id, AttemptState.UNKNOWN),
        )
        found = await unfinished_by_context(state.db)
        assert found == {("1001", "live"): {"intents": 1, "attempts": 1}}
        assert await check(create_async_engine(state.url)) == 1

    run(scenario)


def test_every_context_is_read_not_only_the_current_one() -> None:
    async def scenario(state: World) -> None:
        await add(state, position(network="testnet"), position(account="2002"))
        found = await unfinished_by_context(state.db)
        assert found == {
            ("1001", "testnet"): {"positions": 1},
            ("2002", "live"): {"positions": 1},
        }

    run(scenario)


def test_finished_execution_passes_the_barrier() -> None:
    async def scenario(state: World) -> None:
        closed = position()
        closed.status = PositionStatus.CLOSED
        signal_id = uuid.uuid4()
        await add(state, closed, inbox(signal_id))
        await add(
            state,
            intent(signal_id, IntentState.NO_FILL),
            attempt(signal_id, AttemptState.REJECTED),
        )
        assert await unfinished_by_context(state.db) == {}
        assert await check(create_async_engine(state.url)) == 0

    run(scenario)


def test_the_barrier_and_the_runtime_count_the_same_work() -> None:
    async def scenario(state: World) -> None:
        signal_id = uuid.uuid4()
        await add(state, position(), inbox(signal_id))
        await add(
            state,
            intent(signal_id, IntentState.RESERVED),
            attempt(signal_id, AttemptState.PREPARED),
        )
        found = await unfinished_by_context(state.db)
        assert found is not None
        runtime = state.runtime()
        assert sum(found[("1001", "live")].values()) == await runtime.unfinished(
            ExecutionContext("1001", "live")
        )

    run(scenario)


def test_a_database_without_the_execution_schema_has_nothing_to_finish() -> None:
    async def scenario() -> None:
        async with scratch_database() as url:
            assert await unfinished_by_context(create_async_engine(url)) is None
            assert await check(create_async_engine(url)) == 0

    asyncio.run(scenario())


def test_an_unreadable_database_proves_nothing() -> None:
    unreachable = create_async_engine(
        "postgresql+psycopg://nobody:nothing@127.0.0.1:1/agent",
        connect_args={"connect_timeout": 2},
    )
    assert asyncio.run(check(unreachable)) == 2


def test_no_agent_database_means_no_execution_records() -> None:
    assert asyncio.run(check(None)) == 0
