"""ENTRY signals followed to their first stop or take crossing.

The backend closed a signal position only when someone viewed it, at the
price of that moment: SENTUSDT crossed its stop at 14:48 UTC on 2026-09-23 and
was recorded closed at 15:14 at a price 1.7 % below the stop (ClickUp
869f6m7vn). The agent now finds the crossing in 1m bars and publishes an EXIT.

Revision ID: 0005_signal_watch
Revises: 0004_legacy_lists
Create Date: 2026-10-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_signal_watch"
down_revision = "0004_legacy_lists"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "signal_watch",
        sa.Column("signal_id", sa.String(64), primary_key=True),
        sa.Column("network", sa.String(16), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("entry_ref", sa.Numeric(), nullable=False),
        sa.Column("stop", sa.Numeric(), nullable=False),
        sa.Column("take", sa.Numeric(), nullable=False),
        sa.Column("emitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("checked_until", sa.BigInteger(), nullable=False),
        sa.Column("exit_reason", sa.String(8), nullable=True),
        sa.Column("exit_price", sa.Numeric(), nullable=True),
        sa.Column("exited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
    )
    # The tracker reads the open watches and the unpublished exits.
    op.create_index(
        "ix_signal_watch_open",
        "signal_watch",
        ["network"],
        postgresql_where=sa.text("published_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_signal_watch_open", table_name="signal_watch")
    op.drop_table("signal_watch")
