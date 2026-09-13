# Agent

Standalone trading agent process. Runs as an **independent asyncio daemon**
(exchange REST + websocket connections) and does **not** depend on the FastAPI
backend lifecycle.

## Roles

- **The strategy is the core of the agent.** It analyses market data and emits
  signals. Everything else (data streams, order execution, lifecycle, heartbeat)
  is a service wrapper around it — see `../DESIGN.md`.
- UI ↔ agent communication goes **only** through the backend (never direct).
- Commands and liveness between the backend and the agent travel **only** over
  Redis (the "live plane"). The backend publishes *command envelopes* to a
  channel and keeps a *runtime snapshot* key (what should be running); the agent
  dispatches each command to its registered handler and publishes a heartbeat
  (plus signal events) in return.
- **All exchange access is built on `binance_sdk_spot`.** Own/duplicate models
  (`Quote`, `MarketData`, `NewOrder`, ...) are forbidden; the SDK already provides
  them. Docs and examples live in `../binance-spot/`.

## Run

From the repository root:

```bash
uv sync
uv run --package agent mentis-agent
# or
cd agent && uv run mentis-agent
```

Configuration comes from the top-level `.env` file plus environment variables.
Key variables:

- `REDIS_URL` — backend ↔ agent live plane (default `redis://localhost:6379/0`).
- `AGENT_DB_URL` — agent's own settings DB (optional).
- `BINANCE_DB_URL` — Binance read-only market DB (optional).
- `BINANCE_API_KEY` / `BINANCE_API_SECRET` — Binance credentials (live).
- `BINANCE_TESTNET_API_KEY` / `BINANCE_TESTNET_API_SECRET` — Binance testnet.

Without a saved running intent, the agent boots **idle**. A `start` command from
`/strategy` supplies the configuration. Existing resume behavior is preserved:
`agent:runtime.running=true` restores that configuration on restart, including LIVE.
Stop records `running=false`. The command listener and heartbeat stay available
during bootstrap; Stop cancels unfinished earlier starts.

The backend status separates `desired` configuration from `actual` heartbeat.
HTTP 202 only acknowledges acceptance. Results are correlated through
`agent:result:<command_id>` (3600-second TTL), with `agent:result` retained for
legacy readers. Financial status/results require a superuser.

Signals carry exact Decimal strings, provenance and `signal_id`. Failed emission
is retried in memory with the same ID while still fresh/valid; consumers must
deduplicate. Redis Pub/Sub is not a durable queue. Execution, sizing, position
protection and universe scanning remain unimplemented.

## Structure

- `agent/main.py` — asyncio entry point / daemon. Subscribes to the command
  channel, dispatches commands to their handlers, reports liveness.
- `agent/engine.py` — `Engine`: builds the SDK-backed services, starts/stops the
  strategy, publishes the heartbeat and signal events. Boots in `idle`.
- `agent/common/commands.py` — command envelope + handler registry (`@command`),
  built-in `start`/`stop`/`balance`, and boot-time resume from the snapshot.
- `agent/exchange/` — thin adapters over `binance_sdk_spot`:
  - `rest.py` — `ExchangeRest` (async `Spot.rest_api`);
  - `streams.py` — `MarketDataStreams` (async `Spot.websocket_streams`).
- `agent/strategies/` — the decision core:
  - `base.py` — `Strategy` contract, `StrategyServices`, `Signal`;
  - `registry.py` — `@strategy(name)` + `build_strategy`;
  - `tripple_screen/` — the current strategy (`settings.py`, `indicators.py`,
    `strategy.py`), see `../docs/STRATEGY_TRIPPLE_SCREEN.md`.
- `agent/core/config.py` — pydantic-settings.
- `agent/core/db.py` — async engines (own settings DB + external read-only DB).

## Checks

```bash
cd agent && uv run ruff check . && uv run ruff format --check . && uv run mypy agent tests
cd agent && uv run pytest
```
