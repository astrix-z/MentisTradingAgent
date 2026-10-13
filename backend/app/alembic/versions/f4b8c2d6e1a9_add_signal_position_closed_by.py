"""Add signal_position.closed_by (who closed it: the agent's EXIT or a view)

A read closed a position at the last known price of that moment: SENTUSDT
crossed its stop at 14:48 UTC and was recorded closed at 15:14, 1.7 % below
it (ClickUp 869f6m7vn). The agent's EXIT now carries the bar and price of the
crossing and corrects a closure made by a view.

Revision ID: f4b8c2d6e1a9
Revises: a7e3c1d9f5b2
Create Date: 2026-10-05 15:00:00.000000

"""
import sqlalchemy as sa
import sqlmodel.sql.sqltypes
from alembic import op

# revision identifiers, used by Alembic.
revision = "f4b8c2d6e1a9"
down_revision = "a7e3c1d9f5b2"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "signalposition",
        sa.Column("closed_by", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    )
    # Before this revision only views closed positions: no EXIT was emitted.
    op.execute(
        "UPDATE signalposition SET closed_by = 'view' WHERE status <> 'open'"
    )


def downgrade():
    op.drop_column("signalposition", "closed_by")
