"""When an attempt's answer was applied to its position (EXEC-09 review).

An OCO's answer is committed before it is associated with the position; a
crash in between left a live list nobody tracked. ``applied_at`` marks the
answers whose effect is recorded, so recovery finds the ones that are not.

Revision ID: 0003_attempt_applied
Revises: 0002_execution
Create Date: 2026-10-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_attempt_applied"
down_revision = "0002_execution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "order_attempt",
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Answers recorded before this revision were acted on by the code of the
    # time; only new ones are tracked.
    op.execute(
        "UPDATE order_attempt SET applied_at = coalesce(resolved_at, answered_at) "
        "WHERE state IN ('acked', 'resolved', 'rejected')"
    )


def downgrade() -> None:
    op.drop_column("order_attempt", "applied_at")
