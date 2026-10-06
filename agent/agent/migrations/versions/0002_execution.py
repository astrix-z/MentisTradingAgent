"""Execution tables (plan docs/ORDER_EXECUTION_PLAN.md §8.1).

Revision ID: 0002_execution
Revises: 0001_baseline
Create Date: 2026-09-24
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0002_execution"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None

MONEY = sa.Numeric(38, 18)


def _ts(name: str, *, nullable: bool = False) -> sa.Column[datetime]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def _context() -> list[sa.Column[str]]:
    return [
        sa.Column("account", sa.String(32), nullable=False),
        sa.Column("network", sa.String(16), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "signal_inbox",
        sa.Column("signal_id", UUID(as_uuid=True), primary_key=True),
        *_context(),
        sa.Column("strategy", sa.String(64), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("type", sa.String(8), nullable=False),
        sa.Column("epoch_id", sa.String(128), nullable=True),
        _ts("emitted_at"),
        _ts("valid_until", nullable=True),
        sa.Column("payload", JSONB, nullable=False),
        _ts("received_at"),
        _ts("processed_at", nullable=True),
        sa.CheckConstraint("type IN ('ENTRY', 'HOLD', 'EXIT')", name="ck_signal_type"),
    )
    op.create_index(
        "uq_signal_inbox_entry_epoch",
        "signal_inbox",
        ["account", "network", "strategy", "epoch_id"],
        unique=True,
        postgresql_where=sa.text("type = 'ENTRY'"),
    )

    op.create_table(
        "execution_intent",
        sa.Column(
            "signal_id",
            UUID(as_uuid=True),
            sa.ForeignKey("signal_inbox.signal_id"),
            primary_key=True,
        ),
        *_context(),
        sa.Column("strategy", sa.String(64), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(64), nullable=True),
        sa.Column("plan", JSONB, nullable=True),
        sa.Column("quantity", MONEY, nullable=True),
        sa.Column("cap_price", MONEY, nullable=True),
        sa.Column("quote_reserved", MONEY, nullable=False),
        sa.Column("position_id", UUID(as_uuid=True), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        _ts("resolved_at", nullable=True),
        sa.CheckConstraint("quote_reserved >= 0", name="ck_intent_reserve"),
    )
    op.create_index(
        "uq_execution_intent_active_symbol",
        "execution_intent",
        ["account", "network", "symbol"],
        unique=True,
        postgresql_where=sa.text("state IN ('reserved', 'submitted', 'unknown')"),
    )

    op.create_table(
        "order_attempt",
        sa.Column("client_order_id", sa.String(36), primary_key=True),
        *_context(),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("signal_id", UUID(as_uuid=True), nullable=False),
        sa.Column("position_id", UUID(as_uuid=True), nullable=True),
        sa.Column("role", sa.String(4), nullable=False),
        sa.Column("sequence", sa.Integer, nullable=False),
        sa.Column("list_client_order_id", sa.String(36), nullable=True),
        sa.Column("request", JSONB, nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("exchange_status", sa.String(32), nullable=True),
        sa.Column("order_id", sa.BigInteger, nullable=True),
        sa.Column("order_list_id", sa.BigInteger, nullable=True),
        sa.Column("executed_qty", MONEY, nullable=True),
        sa.Column("response", JSONB, nullable=True),
        sa.Column("error", JSONB, nullable=True),
        _ts("created_at"),
        _ts("sent_at", nullable=True),
        _ts("answered_at", nullable=True),
        _ts("resolved_at", nullable=True),
    )
    op.create_index("ix_order_attempt_signal", "order_attempt", ["signal_id"])
    op.create_index("ix_order_attempt_position", "order_attempt", ["position_id"])
    op.create_index(
        "ix_order_attempt_open",
        "order_attempt",
        ["account", "network"],
        postgresql_where=sa.text("state IN ('prepared', 'sent', 'unknown')"),
    )

    op.create_table(
        "fill",
        sa.Column("account", sa.String(32), primary_key=True),
        sa.Column("network", sa.String(16), primary_key=True),
        sa.Column("symbol", sa.String(32), primary_key=True),
        sa.Column("trade_id", sa.BigInteger, primary_key=True),
        sa.Column("order_id", sa.BigInteger, nullable=False),
        sa.Column("client_order_id", sa.String(36), nullable=True),
        sa.Column("position_id", UUID(as_uuid=True), nullable=True),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("price", MONEY, nullable=False),
        sa.Column("quantity", MONEY, nullable=False),
        sa.Column("quote_quantity", MONEY, nullable=False),
        sa.Column("commission", MONEY, nullable=False),
        sa.Column("commission_asset", sa.String(16), nullable=False),
        sa.Column("commission_quote", MONEY, nullable=True),
        sa.Column("is_maker", sa.Boolean, nullable=False),
        _ts("trade_time"),
        sa.Column("source", sa.String(16), nullable=False),
        _ts("applied_at"),
    )
    op.create_index(
        "ix_fill_order", "fill", ["account", "network", "symbol", "order_id"]
    )
    op.create_index("ix_fill_position", "fill", ["position_id"])

    op.create_table(
        "position",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        *_context(),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("strategy", sa.String(64), nullable=False),
        sa.Column("signal_id", UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("exit_policy", JSONB, nullable=False),
        sa.Column("quantity", MONEY, nullable=False),
        sa.Column("bought_qty", MONEY, nullable=False),
        sa.Column("sold_qty", MONEY, nullable=False),
        sa.Column("base_commission", MONEY, nullable=False),
        sa.Column("bought_quote", MONEY, nullable=False),
        sa.Column("sold_quote", MONEY, nullable=False),
        sa.Column("commission_quote", MONEY, nullable=False),
        sa.Column("fill_vwap", MONEY, nullable=True),
        sa.Column("desired_stop", MONEY, nullable=True),
        sa.Column("exchange_stop", MONEY, nullable=True),
        sa.Column("take_profit", MONEY, nullable=True),
        sa.Column("protection_list_id", sa.String(36), nullable=True),
        _ts("unprotected_since", nullable=True),
        sa.Column("operation", sa.String(24), nullable=True),
        sa.Column("op_version", sa.Integer, nullable=False),
        sa.Column("op_state", JSONB, nullable=True),
        sa.Column("aggregate_seq", sa.Integer, nullable=False),
        _ts("opened_at", nullable=True),
        _ts("closed_at", nullable=True),
        sa.Column("outcome", JSONB, nullable=True),
        sa.Column("realized_pnl", MONEY, nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.CheckConstraint("quantity >= 0", name="ck_position_quantity"),
    )
    op.create_index(
        "uq_position_open_symbol",
        "position",
        ["account", "network", "symbol"],
        unique=True,
        postgresql_where=sa.text("status <> 'closed'"),
    )

    op.create_table(
        "symbol_cooldown",
        sa.Column("account", sa.String(32), primary_key=True),
        sa.Column("network", sa.String(16), primary_key=True),
        sa.Column("symbol", sa.String(32), primary_key=True),
        _ts("cooldown_until"),
        sa.Column("position_id", UUID(as_uuid=True), nullable=False),
        _ts("updated_at"),
    )

    op.create_table(
        "monitor_watermark",
        sa.Column(
            "position_id",
            UUID(as_uuid=True),
            sa.ForeignKey("position.id"),
            primary_key=True,
        ),
        sa.Column("interval", sa.String(8), primary_key=True),
        _ts("bar_open_time"),
        _ts("updated_at"),
    )

    op.create_table(
        "risk_state",
        sa.Column("account", sa.String(32), primary_key=True),
        sa.Column("network", sa.String(16), primary_key=True),
        sa.Column("utc_date", sa.Date, primary_key=True),
        sa.Column("losing_trades", sa.Integer, nullable=False),
        _ts("updated_at"),
    )

    op.create_table(
        "execution_event",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("event_id", UUID(as_uuid=True), nullable=False, unique=True),
        *_context(),
        sa.Column("position_id", UUID(as_uuid=True), nullable=True),
        sa.Column("aggregate_seq", sa.Integer, nullable=True),
        sa.Column("type", sa.String(48), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        _ts("created_at"),
        _ts("published_at", nullable=True),
        sa.UniqueConstraint(
            "position_id", "aggregate_seq", name="uq_execution_event_position_seq"
        ),
    )
    op.create_index(
        "ix_execution_event_unpublished",
        "execution_event",
        ["id"],
        postgresql_where=sa.text("published_at IS NULL"),
    )

    op.create_table(
        "trade_cursor",
        sa.Column("account", sa.String(32), primary_key=True),
        sa.Column("network", sa.String(16), primary_key=True),
        sa.Column("symbol", sa.String(32), primary_key=True),
        sa.Column("last_trade_id", sa.BigInteger, nullable=False),
        _ts("updated_at"),
    )


def downgrade() -> None:
    for table in (
        "trade_cursor",
        "execution_event",
        "risk_state",
        "monitor_watermark",
        "symbol_cooldown",
        "position",
        "fill",
        "order_attempt",
        "execution_intent",
        "signal_inbox",
    ):
        op.drop_table(table)
