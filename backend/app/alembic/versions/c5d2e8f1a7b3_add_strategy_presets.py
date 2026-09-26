"""Add strategy presets, field option lists and built-in sync state

Tables of docs/STRATEGY_PRESETS_PLAN.md §4.3.1 (PRESET-05a). Needs neither the
agent nor Redis: it creates the tables and seeds the T2 option lists for
tripple_screen. downgrade drops everything it created (rollback runbook,
PRESET-09a).

Revision ID: c5d2e8f1a7b3
Revises: b3f7a1c9d2e4
Create Date: 2026-09-26 12:00:00.000000

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "c5d2e8f1a7b3"
down_revision = "b3f7a1c9d2e4"
branch_labels = None
depends_on = None

#: Initial option lists (plan §1, T2), as Binance codes in display order,
#: from the longest interval to the shortest. W1 is "1w", D1 "1d", M1 "1m".
INITIAL_OPTIONS: dict[str, list[str]] = {
    "quote_asset": ["USDT", "USDC"],
    "tide_interval": ["1w", "1d", "12h", "6h", "4h", "2h", "1h", "30m", "15m"],
    "wave_interval": ["1d", "12h", "6h", "4h", "2h", "1h", "30m", "15m", "5m"],
    "ripple_interval": ["12h", "6h", "4h", "2h", "1h", "30m", "15m", "5m", "1m"],
}
STRATEGY = "tripple_screen"


def upgrade():
    op.create_table(
        "strategy_preset",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("strategy", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("params", postgresql.JSONB(), nullable=False),
        sa.Column("is_builtin", sa.Boolean(), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ux_strategy_preset_name",
        "strategy_preset",
        ["strategy", sa.text("lower(name)")],
        unique=True,
    )
    op.create_table(
        "strategy_field_options",
        sa.Column("strategy", sa.String(length=64), nullable=False),
        sa.Column("field", sa.String(length=64), nullable=False),
        sa.Column("options", postgresql.JSONB(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("strategy", "field"),
    )
    op.create_table(
        "strategy_builtin_sync",
        sa.Column("strategy", sa.String(length=64), nullable=False),
        sa.Column(
            "content_hash", sa.String(length=64), server_default="", nullable=False
        ),
        sa.Column(
            "status", sa.String(length=16), server_default="never", nullable=False
        ),
        sa.Column(
            "errors",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('never', 'ok', 'partial')",
            name="ck_strategy_builtin_sync_status",
        ),
        sa.PrimaryKeyConstraint("strategy"),
    )
    options = sa.table(
        "strategy_field_options",
        sa.column("strategy", sa.String()),
        sa.column("field", sa.String()),
        sa.column("options", postgresql.JSONB()),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    op.execute(
        options.insert().values(
            [
                {
                    "strategy": STRATEGY,
                    "field": field,
                    "options": values,
                    "updated_at": sa.func.now(),
                }
                for field, values in INITIAL_OPTIONS.items()
            ]
        )
    )


def downgrade():
    op.drop_table("strategy_builtin_sync")
    op.drop_table("strategy_field_options")
    op.drop_index("ux_strategy_preset_name", table_name="strategy_preset")
    op.drop_table("strategy_preset")
