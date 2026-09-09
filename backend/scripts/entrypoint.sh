#! /usr/bin/env bash
# Backend container entrypoint: apply migrations + initial data, then run the
# real command (fastapi run/dev) passed through from the image CMD or a Compose
# `command:` override.
#
# Compose guarantees `db` is healthy before the backend starts (depends_on), so
# a plain `docker compose up -d --build` brings up a working app without a
# separate one-off migration step.

set -euo pipefail

cd /app/backend

# Apply Alembic migrations and create the initial data/superuser if missing.
# prestart.sh is idempotent, so re-running it on every start is safe.
bash scripts/prestart.sh

# Run the container's real command (e.g. `fastapi run` or `fastapi dev ...`).
exec "$@"
