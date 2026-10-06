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
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, Index, Numeric, String, text
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


class SignalWatch(Base):
    """An ENTRY signal followed until the price first crosses its stop or take.

    The strategy places no order of its own (D-SIGNAL-ONLY): the backend shows
    "what would have happened" for every ENTRY. The agent, which alone reads
    the exchange, finds the first 1m bar that crossed a level and publishes an
    EXIT with that moment and price (:mod:`agent.signal_tracker`). The levels
    are the ones the backend shows: stop = ``swing_low_ref``, take =
    ``entry_ref`` x the ``tp_rule`` factor.
    """

    __tablename__ = "signal_watch"
    __table_args__ = (
        # The tracker reads the open watches and the unpublished exits.
        Index(
            "ix_signal_watch_open",
            "network",
            postgresql_where=text("published_at IS NULL"),
        ),
    )

    signal_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    network: Mapped[str] = mapped_column(String(16))
    symbol: Mapped[str] = mapped_column(String(32))
    entry_ref: Mapped[Decimal] = mapped_column(Numeric)
    stop: Mapped[Decimal] = mapped_column(Numeric)
    take: Mapped[Decimal] = mapped_column(Numeric)
    emitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    #: Open time (ms) of the next 1m bar to read; bars before it are checked.
    checked_until: Mapped[int] = mapped_column(BigInteger)
    exit_reason: Mapped[str | None] = mapped_column(String(8), nullable=True)
    exit_price: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    exited_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: The EXIT reached Redis; until then it is published again.
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


__all__ = ["Base", "MonitoredSymbol", "SignalWatch", "WorkingSymbol"]
