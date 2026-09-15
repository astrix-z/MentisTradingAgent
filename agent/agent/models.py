"""Agent-owned tables.

The agent keeps its own settings here — starting with the working symbols the
user marked, and later the trades and orders it places — in its own database,
which nobody else reads or writes (see ``DESIGN.md``). The backend reaches this
data through the published document in Redis, never by querying the schema.

Tables are created with ``create_all`` on boot (see :mod:`agent.core.db`): the
schema is one table and the agent owns it end to end. A migration tool belongs
here once the schema starts changing shape under real data.
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


__all__ = ["Base", "WorkingSymbol"]
