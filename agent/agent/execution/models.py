"""Execution tables (plan ``docs/ORDER_EXECUTION_PLAN.md`` §8).

Every row lives in an execution context ``(account, network)``: ``account`` is
the exchange ``uid`` from ``get_account``, so a key rotation keeps the history
and another account never sees it.

Money and quantities are ``Numeric`` and read back as ``Decimal``; nothing
here is ever a float. Times are UTC ``timestamptz``.

The schema carries constraints, the event envelope and ``aggregate_seq``, but
it does not decide anything: each table has exactly one writer (plan §8.5).
Order statuses are stored as the SDK's native strings, not as our own enum
(``CONTEXT.md`` §4).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from agent.models import Base

#: Precision for prices, quantities and quote amounts. Binance publishes eight
#: decimals; the headroom keeps VWAP and PnL arithmetic exact.
MONEY = Numeric(38, 18)


def _now() -> datetime:
    return datetime.now(UTC)


class SignalType(StrEnum):
    ENTRY = "ENTRY"
    HOLD = "HOLD"
    EXIT = "EXIT"


class IntentState(StrEnum):
    """Lifecycle of one ENTRY decision (plan §8.2, §7.5)."""

    RECEIVED = "received"
    REJECTED = "rejected"  # admission said no; nothing reserved
    EXPIRED = "expired"  # valid_until passed before submission
    RESERVED = "reserved"  # symbol, slot and quote reserve held
    SUBMITTED = "submitted"  # BUY sent, outcome not yet proven
    UNKNOWN = "unknown"  # BUY outcome ambiguous; everything stays held
    NO_FILL = "no_fill"  # proven zero-fill or reject; everything released
    FILLED = "filled"  # symbol and slot handed to the position


#: Intent states that still hold the symbol and the slot.
INTENT_HOLDING_STATES = (
    IntentState.RESERVED,
    IntentState.SUBMITTED,
    IntentState.UNKNOWN,
)


class AttemptState(StrEnum):
    PREPARED = "prepared"  # committed before send; may have been sent
    SENT = "sent"
    ACKED = "acked"  # the exchange answered; exchange_status holds its word
    REJECTED = "rejected"  # proven refusal
    UNKNOWN = "unknown"  # timeout, 5xx, broken connection, cancelled wait
    RESOLVED = "resolved"  # an UNKNOWN whose outcome was later proven


class PositionStatus(StrEnum):
    OPENING = "opening"
    PROTECTING = "protecting"
    PROTECTED = "protected"
    REPRICING = "repricing"
    CLOSING = "closing"
    UNPROTECTED = "unprotected"
    PROTECTION_OVERDUE = "protection_overdue"
    UNKNOWN = "unknown"
    CLOSED = "closed"


class SignalInbox(Base):
    """Every signal the strategy emitted, committed before anything else sees it.

    Committing here *is* a successful emit (plan §4.1). An ENTRY epoch is
    consumed once per context; a HOLD of the same epoch stays auditable.
    """

    __tablename__ = "signal_inbox"
    __table_args__ = (
        Index(
            "uq_signal_inbox_entry_epoch",
            "account",
            "network",
            "strategy",
            "epoch_id",
            unique=True,
            postgresql_where=text("type = 'ENTRY'"),
        ),
        CheckConstraint("type IN ('ENTRY', 'HOLD', 'EXIT')", name="ck_signal_type"),
    )

    signal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    account: Mapped[str] = mapped_column(String(32))
    network: Mapped[str] = mapped_column(String(16))
    strategy: Mapped[str] = mapped_column(String(64))
    symbol: Mapped[str] = mapped_column(String(32))
    type: Mapped[str] = mapped_column(String(8))
    epoch_id: Mapped[str | None] = mapped_column(String(128))
    emitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ExecutionIntent(Base):
    """Admission's decision about one ENTRY, and the resources it holds (§7.5).

    ``quote_reserved`` is the USDT held for the entry; it drops to zero when the
    BUY is proven, one way or the other. The symbol and slot are held while the
    state is in :data:`INTENT_HOLDING_STATES` and then either released or handed
    to the position.
    """

    __tablename__ = "execution_intent"
    __table_args__ = (
        Index(
            "uq_execution_intent_active_symbol",
            "account",
            "network",
            "symbol",
            unique=True,
            postgresql_where=text("state IN ('reserved', 'submitted', 'unknown')"),
        ),
        CheckConstraint("quote_reserved >= 0", name="ck_intent_reserve"),
    )

    signal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("signal_inbox.signal_id"),
        primary_key=True,
    )
    account: Mapped[str] = mapped_column(String(32))
    network: Mapped[str] = mapped_column(String(16))
    strategy: Mapped[str] = mapped_column(String(64))
    symbol: Mapped[str] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(String(64))
    plan: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    quantity: Mapped[Decimal | None] = mapped_column(MONEY)
    cap_price: Mapped[Decimal | None] = mapped_column(MONEY)
    quote_reserved: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    position_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OrderAttempt(Base):
    """Our journal of each exchange request, committed before it is sent.

    It records what we did and the SDK's raw answer; it is not a model of an
    order (``CONTEXT.md`` §4). ``exchange_status`` is the SDK's own string.
    """

    __tablename__ = "order_attempt"
    __table_args__ = (
        Index("ix_order_attempt_signal", "signal_id"),
        Index("ix_order_attempt_position", "position_id"),
        Index(
            "ix_order_attempt_open",
            "account",
            "network",
            postgresql_where=text("state IN ('prepared', 'sent', 'unknown')"),
        ),
    )

    client_order_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    account: Mapped[str] = mapped_column(String(32))
    network: Mapped[str] = mapped_column(String(16))
    symbol: Mapped[str] = mapped_column(String(32))
    signal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    position_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    role: Mapped[str] = mapped_column(String(4))
    sequence: Mapped[int] = mapped_column(Integer)
    list_client_order_id: Mapped[str | None] = mapped_column(String(36))
    request: Mapped[dict[str, Any]] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(16))
    exchange_status: Mapped[str | None] = mapped_column(String(32))
    order_id: Mapped[int | None] = mapped_column(BigInteger)
    order_list_id: Mapped[int | None] = mapped_column(BigInteger)
    executed_qty: Mapped[Decimal | None] = mapped_column(MONEY)
    response: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Fill(Base):
    """One exchange trade, from a response, ``my_trades`` or the user stream."""

    __tablename__ = "fill"
    __table_args__ = (
        Index("ix_fill_order", "account", "network", "symbol", "order_id"),
        Index("ix_fill_position", "position_id"),
    )

    account: Mapped[str] = mapped_column(String(32), primary_key=True)
    network: Mapped[str] = mapped_column(String(16), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    trade_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(BigInteger)
    client_order_id: Mapped[str | None] = mapped_column(String(36))
    position_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    side: Mapped[str] = mapped_column(String(4))
    price: Mapped[Decimal] = mapped_column(MONEY)
    quantity: Mapped[Decimal] = mapped_column(MONEY)
    quote_quantity: Mapped[Decimal] = mapped_column(MONEY)
    commission: Mapped[Decimal] = mapped_column(MONEY)
    commission_asset: Mapped[str] = mapped_column(String(16))
    commission_quote: Mapped[Decimal | None] = mapped_column(MONEY)
    is_maker: Mapped[bool] = mapped_column(Boolean)
    trade_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(16))
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Position(Base):
    """A real position and its ledger (plan §8.3).

    Any status but ``closed`` holds the symbol in its context, so a second
    position can never open next to an opening, unprotected or UNKNOWN one.
    """

    __tablename__ = "position"
    __table_args__ = (
        Index(
            "uq_position_open_symbol",
            "account",
            "network",
            "symbol",
            unique=True,
            postgresql_where=text("status <> 'closed'"),
        ),
        CheckConstraint("quantity >= 0", name="ck_position_quantity"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account: Mapped[str] = mapped_column(String(32))
    network: Mapped[str] = mapped_column(String(16))
    symbol: Mapped[str] = mapped_column(String(32))
    strategy: Mapped[str] = mapped_column(String(64))
    signal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), unique=True)
    status: Mapped[str] = mapped_column(String(24))
    exit_policy: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # Ledger, in base asset and quote currency.
    quantity: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    bought_qty: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    sold_qty: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    base_commission: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    bought_quote: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    sold_quote: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    commission_quote: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    fill_vwap: Mapped[Decimal | None] = mapped_column(MONEY)
    # Protection.
    desired_stop: Mapped[Decimal | None] = mapped_column(MONEY)
    exchange_stop: Mapped[Decimal | None] = mapped_column(MONEY)
    take_profit: Mapped[Decimal | None] = mapped_column(MONEY)
    protection_list_id: Mapped[str | None] = mapped_column(String(36))
    unprotected_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Serialized operation in flight (plan §6.7).
    operation: Mapped[str | None] = mapped_column(String(24))
    op_version: Mapped[int] = mapped_column(Integer, default=0)
    op_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    aggregate_seq: Mapped[int] = mapped_column(Integer, default=0)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    realized_pnl: Mapped[Decimal | None] = mapped_column(MONEY)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class SymbolCooldown(Base):
    """Absolute end of a symbol's cooldown, written with the close outcome."""

    __tablename__ = "symbol_cooldown"

    account: Mapped[str] = mapped_column(String(32), primary_key=True)
    network: Mapped[str] = mapped_column(String(16), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    cooldown_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    position_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class MonitorWatermark(Base):
    """Last closed trailing bar whose result is already stored (plan §9.3)."""

    __tablename__ = "monitor_watermark"

    position_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("position.id"), primary_key=True
    )
    interval: Mapped[str] = mapped_column(String(8), primary_key=True)
    bar_open_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class RiskState(Base):
    """Losing trades per UTC day (plan §7.4, Q19)."""

    __tablename__ = "risk_state"

    account: Mapped[str] = mapped_column(String(32), primary_key=True)
    network: Mapped[str] = mapped_column(String(16), primary_key=True)
    utc_date: Mapped[date] = mapped_column(Date, primary_key=True)
    losing_trades: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class ExecutionEvent(Base):
    """Append-only audit and transactional outbox (plan §10).

    Written in the same transaction as the state it describes; a position's
    events carry a gapless ``aggregate_seq``.
    """

    __tablename__ = "execution_event"
    __table_args__ = (
        UniqueConstraint(
            "position_id", "aggregate_seq", name="uq_execution_event_position_seq"
        ),
        Index(
            "ix_execution_event_unpublished",
            "id",
            postgresql_where=text("published_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, default=uuid.uuid4
    )
    account: Mapped[str] = mapped_column(String(32))
    network: Mapped[str] = mapped_column(String(16))
    position_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    aggregate_seq: Mapped[int | None] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(48))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TradeCursor(Base):
    """Where ``my_trades`` reconciliation stopped for a symbol (plan §9.2)."""

    __tablename__ = "trade_cursor"

    account: Mapped[str] = mapped_column(String(32), primary_key=True)
    network: Mapped[str] = mapped_column(String(16), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    last_trade_id: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


__all__ = [
    "INTENT_HOLDING_STATES",
    "MONEY",
    "AttemptState",
    "ExecutionEvent",
    "ExecutionIntent",
    "Fill",
    "IntentState",
    "MonitorWatermark",
    "OrderAttempt",
    "Position",
    "PositionStatus",
    "RiskState",
    "SignalInbox",
    "SignalType",
    "SymbolCooldown",
    "TradeCursor",
]
