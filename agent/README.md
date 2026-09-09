# Agent

Standalone trading agent process. Runs as an **independent asyncio daemon**
(exchange REST + websocket connections) and does **not** depend on the FastAPI
backend lifecycle.

## Roles

- UI ↔ agent communication goes **only** through the backend (never direct).
- Commands and liveness between the backend and the agent travel **only** over
  Redis (the "live plane"); no service reads or writes another service's DB.
  The backend publishes *command envelopes* to a channel and keeps a *runtime
  snapshot* key (what should be running); the agent dispatches each command to
  its registered handler and publishes a heartbeat in return.
- The agent reads market data from an external, separate DB (**read-only**).
- The agent stores its own settings/results in a dedicated database on the
  project's Postgres instance (or a dedicated schema — see below).

## Run

From the repository root:

```bash
uv sync          # after adding the `agent` member to the uv workspace
uv run --package agent mentis-agent
# or
cd agent && uv run mentis-agent
```

Configuration comes from the top-level `.env` file plus environment variables.
Key variables:

- `REDIS_URL` — backend ↔ agent live plane (default `redis://localhost:6379/0`).
- `AGENT_DB_URL` — agent's own settings DB (e.g. `postgresql://postgres:***@db:5432/agent`).
- `BINANCE_DB_URL` — Binance read-only market DB (use a SELECT-only role).
- `BINANCE_API_KEY` / `BINANCE_API_SECRET` — Binance credentials.
- `BINANCE_TESTNET_API_KEY` / `BINANCE_TESTNET_API_SECRET` — Binance testnet (sandbox) credentials.

## Database strategy

Two independent apps (`backend` and `agent`) each own their migrations. To avoid
two Alembic trees fighting over one catalog:

- Prefer a **dedicated database** (e.g. `agent`) on the same Postgres instance.
- Or, if sharing the `app` database, give the agent a **dedicated schema** and
  point its Alembic at that schema.

## Structure

- `agent/main.py` — asyncio entry point / daemon loop. Subscribes to the
  command channel and dispatches commands to their handlers; reports liveness.
- `agent/engine.py` — the trading engine (holds network/strategy + state) and
  the Redis keys/heartbeat. ``_trading_loop`` is a placeholder for the real
  trading logic.
- `agent/commands.py` — command envelope + handler registry (``@command``
  decorator) and boot-time resume from the runtime snapshot.
- `agent/core/config.py` — pydantic-settings.
- `agent/core/db.py` — async engines (own settings DB + external read-only DB).
- `agent/exchange/` — exchange REST/websocket clients (to be implemented).
