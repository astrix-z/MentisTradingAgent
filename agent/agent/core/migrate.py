"""Bring the agent's database to the current schema at boot.

Three starting points (plan §8.1):

* empty database → plain ``upgrade``;
* database already under Alembic → ``upgrade``;
* database created by the old ``create_all`` (tables present, no
  ``alembic_version``) → **verified adoption**: its tables must match the
  baseline exactly, then the baseline is stamped and the rest upgraded.

Anything else — a partial or different legacy schema — raises
:class:`SchemaMismatchError` instead of being stamped over: a blind stamp would
skip the execution tables, and a blind ``CREATE TABLE`` would collide with the
existing ones. The caller keeps execution fail-closed on any failure here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)

BASELINE_REVISION = "0001_baseline"
_MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"

#: The legacy schema the baseline revision stands for: table → column →
#: (type name, nullable). Primary keys are checked separately; any other index
#: or constraint is a mismatch, since the baseline has none.
_BASELINE: dict[str, dict[str, tuple[str, bool]]] = {
    "working_symbol": {
        "symbol": ("VARCHAR(32)", False),
        "base_asset": ("VARCHAR(32)", True),
        "updated_at": ("TIMESTAMP WITH TIME ZONE", False),
    },
    "monitored_symbol": {
        "symbol": ("VARCHAR(32)", False),
        "updated_at": ("TIMESTAMP WITH TIME ZONE", False),
    },
}
_BASELINE_KEYS = {"working_symbol": ["symbol"], "monitored_symbol": ["symbol"]}


class SchemaMismatchError(RuntimeError):
    """The database holds a schema this code must not adopt or upgrade."""


class MigrationPath(StrEnum):
    FRESH = "fresh"
    UPGRADE = "upgrade"
    ADOPTED = "adopted"


@dataclass(frozen=True, slots=True)
class MigrationResult:
    path: MigrationPath
    revision: str


def _config(connection: Connection) -> Config:
    config = Config()
    config.set_main_option("script_location", str(_MIGRATIONS))
    config.attributes["connection"] = connection
    return config


def _baseline_problems(connection: Connection) -> list[str]:
    """Differences between the legacy tables and the baseline; empty if equal."""
    inspector = inspect(connection)
    present = set(inspector.get_table_names())
    problems: list[str] = []
    unexpected = present - set(_BASELINE)
    if unexpected:
        problems.append(f"unexpected tables {sorted(unexpected)}")
    for table, expected in _BASELINE.items():
        if table not in present:
            problems.append(f"missing table {table}")
            continue
        actual = {
            column["name"]: (
                column["type"].compile(dialect=connection.dialect),
                bool(column["nullable"]),
            )
            for column in inspector.get_columns(table)
        }
        if actual != expected:
            problems.append(f"{table} columns {actual} != {expected}")
        keys = inspector.get_pk_constraint(table).get("constrained_columns")
        if keys != _BASELINE_KEYS[table]:
            problems.append(f"{table} primary key {keys}")
        # The baseline has nothing beyond the primary key. An extra unique
        # index would survive adoption and reject rows the code expects to
        # write (e.g. BTCUSDT and BTCUSDC sharing base_asset).
        extras = {
            "indexes": [i.get("name") for i in inspector.get_indexes(table)],
            "unique constraints": [
                u.get("name") for u in inspector.get_unique_constraints(table)
            ],
            "foreign keys": [f.get("name") for f in inspector.get_foreign_keys(table)],
            "check constraints": [
                c.get("name") for c in inspector.get_check_constraints(table)
            ],
        }
        for kind, names in extras.items():
            if names:
                problems.append(f"{table} has {kind} {sorted(map(str, names))}")
    return problems


def _current_revision(connection: Connection) -> str | None:
    return MigrationContext.configure(connection).get_current_revision()


def _migrate(connection: Connection) -> MigrationResult:
    config = _config(connection)
    tables = set(inspect(connection).get_table_names())
    if "alembic_version" in tables:
        path = MigrationPath.UPGRADE
    elif not tables:
        path = MigrationPath.FRESH
    else:
        problems = _baseline_problems(connection)
        if problems:
            raise SchemaMismatchError(
                "legacy agent schema does not match the baseline: "
                + "; ".join(problems)
            )
        command.stamp(config, BASELINE_REVISION)
        path = MigrationPath.ADOPTED
    command.upgrade(config, "head")
    revision = _current_revision(connection)
    if revision is None:
        raise SchemaMismatchError("no revision recorded after upgrade")
    return MigrationResult(path=path, revision=revision)


async def migrate(engine: AsyncEngine) -> MigrationResult:
    """Upgrade ``engine``'s database to head; see the module docstring."""
    async with engine.begin() as connection:
        result = await connection.run_sync(_migrate)
    logger.info("agent database schema at %s (%s)", result.revision, result.path.value)
    return result


__all__ = [
    "BASELINE_REVISION",
    "MigrationPath",
    "MigrationResult",
    "SchemaMismatchError",
    "migrate",
]
