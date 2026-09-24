"""Agent-owned tables.

The agent keeps its own settings here — starting with the working symbols the
user marked, and later the trades and orders it places — in its own database,
which nobody else reads or writes (see ``DESIGN.md``). The backend reaches this
data through the published document in Redis, never by querying the schema.

The schema is owned by Alembic migrations in :mod:`agent.migrations`, applied
at boot by :mod:`agent.core.migrate`; execution tables live in
:mod:`agent.execution.models`.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for every agent-owned table."""


def _now() -> datetime:
    return datetime.now(UTC)


class WorkingSymbol(Base):
    """A symbol the user marked as part of the strategy.

    A row *is* the mark: symbols the user left unchecked simply have no row,
    which keeps "what is selected" and "what exists" from drifting apart.
    """

    __tablename__ = "working_symbol"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    base_asset: Mapped[str | None] = mapped_column(String(32), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class MonitoredSymbol(Base):
    """A symbol the exchange has put under its Monitoring tag.

    Again a row *is* the mark, and the whole set is replaced in one write when
    the tag list is refreshed. It lives in the agent's own database because it
    outlives a restart and will sometimes inform a decision, and because the
    published document is a cache of it rather than the record (see
    ``agent/monitoring.py`` for where the list comes from and how it fails).
    """

    __tablename__ = "monitored_symbol"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


__all__ = ["Base", "MonitoredSymbol", "WorkingSymbol"]
