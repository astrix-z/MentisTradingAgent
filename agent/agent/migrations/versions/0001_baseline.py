"""Baseline: the two tables created by ``create_all`` before migrations existed.

A database that already has exactly these tables and no ``alembic_version`` is
adopted by stamping this revision (see :mod:`agent.core.migrate`).

Revision ID: 0001_baseline
Revises:
Create Date: 2026-09-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "working_symbol",
        sa.Column("symbol", sa.String(32), primary_key=True),
        sa.Column("base_asset", sa.String(32), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "monitored_symbol",
        sa.Column("symbol", sa.String(32), primary_key=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("monitored_symbol")
    op.drop_table("working_symbol")
