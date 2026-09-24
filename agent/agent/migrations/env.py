"""Alembic environment for the agent database.

Migrations run in-process at boot through :func:`agent.core.migrate.migrate`,
which hands over an open connection; there is no offline (SQL script) mode.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy.engine import Connection

import agent.execution.models  # noqa: F401 - registers the execution tables
from agent.models import Base

connection = context.config.attributes.get("connection")
if not isinstance(connection, Connection):
    raise RuntimeError("agent migrations run through agent.core.migrate only")

context.configure(
    connection=connection,
    target_metadata=Base.metadata,
    compare_type=True,
)
with context.begin_transaction():
    context.run_migrations()
