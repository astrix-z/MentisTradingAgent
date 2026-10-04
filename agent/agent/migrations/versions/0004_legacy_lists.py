"""Recover OCO answers 0003 marked applied without proof (EXEC-09 re-review).

0003 set ``applied_at`` on every answer recorded before it, including the
one the column exists for: an OCO accepted, its answer committed, the process
dead before the list was associated with the position. After the upgrade that
list was invisible to recovery, and the position went overdue and tried to
sell the asset the list holds.

A list answer counts as applied only where the record proves it: its
position is closed, or protected by this list, or by a list sent after it.
Every other answered list of an open position goes back to recovery, which
reads it from the exchange: a live one becomes the protection, a finished
one has its legs proven and booked (a no-op for a list already booked).
Positions in an EXIT operation are left alone — the worker's exit does not
settle lists, and an exit was under way before the upgrade.

Revision ID: 0004_legacy_lists
Revises: 0003_attempt_applied
Create Date: 2026-10-04
"""

from __future__ import annotations

from alembic import op

revision = "0004_legacy_lists"
down_revision = "0003_attempt_applied"
branch_labels = None
depends_on = None

#: Answered lists of open positions whose association is not on record.
UNPROVEN = """
UPDATE order_attempt AS a SET applied_at = NULL
WHERE a.role = 'pl'
  AND a.state IN ('acked', 'resolved')
  AND a.order_list_id IS NOT NULL
  AND a.applied_at IS NOT NULL
  AND EXISTS (
    SELECT 1 FROM position AS p
    WHERE p.signal_id = a.signal_id
      AND p.status <> 'closed'
      AND (p.operation IS NULL OR p.operation = 'reprice')
      AND p.protection_list_id IS DISTINCT FROM a.client_order_id
      AND NOT EXISTS (
        SELECT 1 FROM order_attempt AS current
        WHERE current.client_order_id = p.protection_list_id
          AND current.created_at > a.created_at
      )
  )
"""


def upgrade() -> None:
    op.execute(UNPROVEN)


def downgrade() -> None:
    # Nothing to undo: an answer recovery re-applied stays applied.
    pass
