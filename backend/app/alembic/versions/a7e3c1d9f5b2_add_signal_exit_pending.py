"""Add signal_exit_pending (EXITs waiting for a late ENTRY)

Revision ID: a7e3c1d9f5b2
Revises: c5d2e8f1a7b3
Create Date: 2026-10-04 08:00:00.000000

"""
import sqlalchemy as sa
import sqlmodel.sql.sqltypes
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "a7e3c1d9f5b2"
down_revision = "c5d2e8f1a7b3"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "signal_exit_pending",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("network", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("symbol", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("emitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("exit_reason", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_signal_exit_pending",
        "signal_exit_pending",
        ["network", "symbol", "emitted_at"],
        unique=True,
    )


def downgrade():
    op.drop_index("uq_signal_exit_pending", table_name="signal_exit_pending")
    op.drop_table("signal_exit_pending")
