"""EXEC-01: migrations, legacy adoption and the execution constraints.

Runs on a throwaway PostgreSQL database (see ``tests/pg.py``).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from pg import scratch_engine
from sqlalchemy import inspect, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import agent.migrations
from agent.core.migrate import MigrationPath, SchemaMismatchError, migrate
from agent.execution.models import Position, PositionStatus, SignalInbox
from agent.models import Base, MonitoredSymbol, WorkingSymbol

EXECUTION_TABLES = {
    "signal_inbox",
    "execution_intent",
    "order_attempt",
    "fill",
    "position",
    "symbol_cooldown",
    "monitor_watermark",
    "risk_state",
    "execution_event",
    "trade_cursor",
}
NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)


def run(scenario: Callable[[AsyncEngine], Awaitable[None]]) -> None:
    async def wrapper() -> None:
        async with scratch_engine() as engine:
            await scenario(engine)

    asyncio.run(wrapper())


async def _tables(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as connection:
        return set(await connection.run_sync(lambda c: inspect(c).get_table_names()))


async def _create_legacy(engine: AsyncEngine) -> None:
    """Exactly what the old boot path did: ``create_all`` of the two tables."""
    legacy = [WorkingSymbol.__table__, MonitoredSymbol.__table__]
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda c: Base.metadata.create_all(c, tables=legacy)  # type: ignore[arg-type]
        )


def _session(engine: AsyncEngine) -> AsyncSession:
    return async_sessionmaker(engine, expire_on_commit=False)()


def test_fresh_database_upgrades_to_head() -> None:
    async def scenario(engine: AsyncEngine) -> None:
        result = await migrate(engine)
        assert result.path is MigrationPath.FRESH
        assert result.revision == "0004_legacy_lists"
        assert EXECUTION_TABLES | {"working_symbol", "monitored_symbol"} <= (
            await _tables(engine)
        )

    run(scenario)


def test_rerun_is_a_no_op_upgrade() -> None:
    async def scenario(engine: AsyncEngine) -> None:
        await migrate(engine)
        again = await migrate(engine)
        assert again.path is MigrationPath.UPGRADE
        assert again.revision == "0004_legacy_lists"

    run(scenario)


def test_legacy_database_is_adopted_and_keeps_its_rows() -> None:
    async def scenario(engine: AsyncEngine) -> None:
        await _create_legacy(engine)
        async with _session(engine) as session:
            session.add(WorkingSymbol(symbol="BTCUSDT", base_asset="BTC"))
            session.add(MonitoredSymbol(symbol="SENTUSDT"))
            await session.commit()

        result = await migrate(engine)

        assert result.path is MigrationPath.ADOPTED
        assert result.revision == "0004_legacy_lists"
        assert EXECUTION_TABLES <= await _tables(engine)
        async with _session(engine) as session:
            working = (await session.scalars(select(WorkingSymbol))).all()
            monitored = (await session.scalars(select(MonitoredSymbol))).all()
        assert [(row.symbol, row.base_asset) for row in working] == [("BTCUSDT", "BTC")]
        assert [row.symbol for row in monitored] == ["SENTUSDT"]
        assert (await migrate(engine)).path is MigrationPath.UPGRADE

    run(scenario)


@pytest.mark.parametrize(
    "ddl",
    [
        # Partial: only one of the two legacy tables.
        [
            "create table working_symbol (symbol varchar(32) primary key, "
            "base_asset varchar(32), updated_at timestamptz not null)"
        ],
        # Different: a column is missing.
        [
            "create table working_symbol (symbol varchar(32) primary key, "
            "updated_at timestamptz not null)",
            "create table monitored_symbol (symbol varchar(32) primary key, "
            "updated_at timestamptz not null)",
        ],
        # Foreign: an unknown table next to nothing we know.
        ["create table something_else (id int primary key)"],
        # Same columns and keys, plus a unique index the baseline lacks: it
        # would reject BTCUSDT and BTCUSDC sharing a base asset.
        [
            "create table working_symbol (symbol varchar(32) primary key, "
            "base_asset varchar(32), updated_at timestamptz not null)",
            "create table monitored_symbol (symbol varchar(32) primary key, "
            "updated_at timestamptz not null)",
            "create unique index ws_base on working_symbol (base_asset)",
        ],
        # Same again with a plain (non-unique) index.
        [
            "create table working_symbol (symbol varchar(32) primary key, "
            "base_asset varchar(32), updated_at timestamptz not null)",
            "create table monitored_symbol (symbol varchar(32) primary key, "
            "updated_at timestamptz not null)",
            "create index ms_updated on monitored_symbol (updated_at)",
        ],
        # Same again with a check constraint.
        [
            "create table working_symbol (symbol varchar(32) primary key, "
            "base_asset varchar(32), updated_at timestamptz not null, "
            "check (symbol <> ''))",
            "create table monitored_symbol (symbol varchar(32) primary key, "
            "updated_at timestamptz not null)",
        ],
    ],
    ids=["partial", "different", "foreign", "unique-index", "index", "check"],
)
def test_mismatched_schema_is_refused_not_stamped(ddl: list[str]) -> None:
    async def scenario(engine: AsyncEngine) -> None:
        async with engine.begin() as connection:
            for statement in ddl:
                await connection.execute(text(statement))
        with pytest.raises(SchemaMismatchError):
            await migrate(engine)
        tables = await _tables(engine)
        assert "alembic_version" not in tables
        assert not EXECUTION_TABLES & tables

    run(scenario)


def test_models_match_the_migrated_schema() -> None:
    import agent.execution.models  # noqa: F401 - registers the tables

    def drift(connection: Connection) -> list[Any]:
        diff: list[Any] = compare_metadata(
            MigrationContext.configure(connection), Base.metadata
        )
        return diff

    async def scenario(engine: AsyncEngine) -> None:
        await migrate(engine)
        async with engine.connect() as connection:
            assert await connection.run_sync(drift) == []

    run(scenario)


def test_migrations_ship_inside_the_package() -> None:
    """The Docker image copies only ``agent/agent``; migrations must be there."""
    package = Path(agent.migrations.__file__).parent
    assert (package / "env.py").is_file()
    assert sorted(p.name for p in (package / "versions").glob("0*.py")) == [
        "0001_baseline.py",
        "0002_execution.py",
        "0003_attempt_applied.py",
        "0004_legacy_lists.py",
    ]


def _position(
    status: str, *, network: str = "live", symbol: str = "BTCUSDT"
) -> Position:
    return Position(
        id=uuid.uuid4(),
        account="1001",
        network=network,
        symbol=symbol,
        strategy="tripple_screen",
        signal_id=uuid.uuid4(),
        status=status,
        exit_policy={},
        quantity=Decimal("0.001"),
    )


@pytest.mark.parametrize(
    "status", [s.value for s in PositionStatus if s is not PositionStatus.CLOSED]
)
def test_every_unfinished_position_holds_its_symbol(status: str) -> None:
    async def scenario(engine: AsyncEngine) -> None:
        await migrate(engine)
        async with _session(engine) as session:
            session.add(_position(status))
            await session.commit()
        async with _session(engine) as session:
            session.add(_position(PositionStatus.OPENING))
            with pytest.raises(IntegrityError):
                await session.commit()

    run(scenario)


def test_closed_position_frees_the_symbol_and_contexts_are_isolated() -> None:
    async def scenario(engine: AsyncEngine) -> None:
        await migrate(engine)
        async with _session(engine) as session:
            session.add(_position(PositionStatus.CLOSED))
            session.add(_position(PositionStatus.CLOSED))
            session.add(_position(PositionStatus.PROTECTED))
            session.add(_position(PositionStatus.PROTECTED, network="testnet"))
            session.add(_position(PositionStatus.PROTECTED, symbol="ETHUSDT"))
            await session.commit()

    run(scenario)


def test_concurrent_opens_of_one_symbol_admit_exactly_one() -> None:
    async def scenario(engine: AsyncEngine) -> None:
        await migrate(engine)

        async def open_one() -> bool:
            async with _session(engine) as session:
                session.add(_position(PositionStatus.OPENING))
                try:
                    await session.commit()
                except IntegrityError:
                    return False
                return True

        results = await asyncio.gather(*(open_one() for _ in range(5)))
        assert sorted(results) == [False, False, False, False, True]

    run(scenario)


def _signal(kind: str, epoch: str, *, network: str = "live") -> SignalInbox:
    return SignalInbox(
        signal_id=uuid.uuid4(),
        account="1001",
        network=network,
        strategy="tripple_screen",
        symbol="BTCUSDT",
        type=kind,
        epoch_id=epoch,
        emitted_at=NOW,
        payload={},
    )


def test_entry_epoch_is_unique_but_hold_of_that_epoch_is_kept() -> None:
    async def scenario(engine: AsyncEngine) -> None:
        await migrate(engine)
        async with _session(engine) as session:
            session.add(_signal("ENTRY", "e1"))
            session.add(_signal("HOLD", "e1"))
            session.add(_signal("HOLD", "e1"))
            session.add(_signal("ENTRY", "e1", network="testnet"))
            session.add(_signal("ENTRY", "e2"))
            await session.commit()
        async with _session(engine) as session:
            session.add(_signal("ENTRY", "e1"))
            with pytest.raises(IntegrityError):
                await session.commit()

    run(scenario)
