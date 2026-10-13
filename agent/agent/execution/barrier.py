"""Unfinished real execution in every context (release review 869fgbyaf).

A revision without the executor (before e133313, among them T0 567f4f8)
cannot accompany a position, resolve an UNKNOWN entry or finish an operation
on one. Stop proves only that the strategy stopped and the commands were
delivered: the stopped engine keeps following real positions. So before
such a rollback the deploy fence asks this module — with the image of the
agent being replaced, after that agent stopped — whether anything is left:

    python -m agent.execution.barrier

Exit 0: nothing unfinished, or the agent has no execution records at all.
Exit 1: unfinished work, listed per context. Exit 2: the database could not
be read, which proves nothing.

"Unfinished" is what :meth:`ExecutionRuntime.unfinished` counts for one
context; both read :data:`UNFINISHED`.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from agent.execution.models import (
    INTENT_HOLDING_STATES,
    AttemptState,
    ExecutionIntent,
    OrderAttempt,
    Position,
    PositionStatus,
)

#: (kind, model, condition) of every row that is still work for the executor:
#: a position that is not closed (its protection, trailing, time exit and any
#: operation in flight), an intent that holds a symbol and a slot, an order
#: attempt without a proven outcome.
UNFINISHED: tuple[tuple[str, Any, Any], ...] = (
    ("positions", Position, Position.status != PositionStatus.CLOSED),
    (
        "intents",
        ExecutionIntent,
        ExecutionIntent.state.in_(list(INTENT_HOLDING_STATES)),
    ),
    (
        "attempts",
        OrderAttempt,
        OrderAttempt.state.in_(
            [AttemptState.PREPARED, AttemptState.SENT, AttemptState.UNKNOWN]
        ),
    ),
)

logger = logging.getLogger(__name__)


async def unfinished_by_context(
    db: AsyncEngine,
) -> dict[tuple[str, str], dict[str, int]] | None:
    """Unfinished rows per (account, network); ``None`` without the schema."""
    async with db.connect() as connection:
        tables = [model.__tablename__ for _, model, _ in UNFINISHED]
        present = [
            await connection.scalar(text("select to_regclass(:t)"), {"t": table})
            for table in tables
        ]
        if not any(present):
            return None
        if not all(present):
            missing = [t for t, p in zip(tables, present, strict=True) if p is None]
            raise RuntimeError(f"execution schema is incomplete: no {missing}")
        found: dict[tuple[str, str], dict[str, int]] = {}
        for kind, model, condition in UNFINISHED:
            rows = await connection.execute(
                select(model.account, model.network, func.count())
                .where(condition)
                .group_by(model.account, model.network)
            )
            for account, network, count in rows:
                found.setdefault((account, network), {})[kind] = int(count)
    return found


async def check(db: AsyncEngine | None) -> int:
    if db is None:
        logger.info("barrier: no AGENT_DB_URL — the agent keeps no execution records")
        return 0
    try:
        found = await unfinished_by_context(db)
    except Exception as exc:  # noqa: BLE001 - any failure means "not proven"
        logger.error("barrier: cannot read the agent database: %s", exc)
        return 2
    finally:
        await db.dispose()
    if found is None:
        logger.info("barrier: no execution schema — nothing was ever executed")
        return 0
    if not found:
        logger.info("barrier: no unfinished execution in any context")
        return 0
    for (account, network), kinds in sorted(found.items()):
        counts = " ".join(f"{kind}={count}" for kind, count in sorted(kinds.items()))
        logger.error(
            "barrier: unfinished execution %s/%s: %s", account, network, counts
        )
    return 1


def main() -> int:
    from agent.core import db

    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    return asyncio.run(check(db.settings_engine()))


if __name__ == "__main__":
    sys.exit(main())
