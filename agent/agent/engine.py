"""Agent engine — lifecycle, live plane and the running strategy.

The engine is the *service wrapper* around a strategy: it owns the Redis live
plane (heartbeat + signals), builds the SDK-backed services the strategy needs,
and starts/stops the strategy on command. The strategy is the core; the engine
contains no trading logic.

The system boots with **no strategy running**. A ``start`` command carrying the
UI settings starts one; ``stop`` tears it down. Exchange access is exclusively
through the adapters in :mod:`agent.exchange`, which wrap ``binance_sdk_spot``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any, Literal

from redis.asyncio import Redis
from redis.exceptions import RedisError

from agent.exchange import ExchangeError, ExchangeRest, MarketDataStreams, balance_map
from agent.strategies import Signal, Strategy, StrategyServices, build_strategy

logger = logging.getLogger(__name__)

# Redis keys/channels shared with the backend (keep in sync with
# backend/app/api/routes/trading.py).
REDIS_COMMAND_CHANNEL = "agent:commands"
REDIS_RUNTIME_KEY = "agent:runtime"
REDIS_HEARTBEAT_KEY = "trading:heartbeat"
REDIS_RESULT_KEY = "agent:result"
REDIS_EVENTS_CHANNEL = "agent:events"

# How often the heartbeat is published; keep below the backend staleness window.
HEARTBEAT_INTERVAL = 5.0
# TTL for the heartbeat key so a dead agent stops reading as online.
HEARTBEAT_TTL = 30
# How often the account balance is refreshed while running.
_BALANCE_REFRESH_SECONDS = 30.0

AgentState = Literal["running", "idle", "error"]


def _now() -> datetime:
    return datetime.now(UTC)


class Engine:
    """Owns the live plane and exactly one running strategy."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._task: asyncio.Task[None] | None = None
        self._network: str = "testnet"
        self._strategy_name: str = ""
        self._state: AgentState = "idle"
        self._message: str | None = None
        self._balances: dict[str, float] = {}

        # SDK-backed services for the current run.
        self._rest: ExchangeRest | None = None
        self._rest_network: str | None = None
        self._streams: MarketDataStreams | None = None
        self._strategy: Strategy | None = None

    # --- introspection -----------------------------------------------------

    @property
    def state(self) -> AgentState:
        return self._state

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def network(self) -> str:
        return self._network

    @property
    def balances(self) -> dict[str, float]:
        return dict(self._balances)

    # --- SDK services ------------------------------------------------------

    def _ensure_rest(self, network: str) -> ExchangeRest:
        """Return a REST adapter for ``network``, reconnecting when it changes."""
        if self._rest is None or self._rest_network != network:
            self._rest = ExchangeRest.connect(network)  # type: ignore[arg-type]
            self._rest_network = network
        return self._rest

    async def fetch_account_balance(
        self, network: str | None = None
    ) -> dict[str, float]:
        """Fetch ``{asset: free}`` for non-zero balances on ``network``."""
        target = network or self._network
        rest = self._ensure_rest(target)
        balances = balance_map(await rest.balances())
        if target == self._network:
            self._balances = balances
        return balances

    async def _refresh_balance_quiet(self) -> None:
        try:
            await self.fetch_account_balance(self._network)
        except ExchangeError as exc:
            logger.warning("balance refresh failed: %s", exc)

    # --- run lifecycle -----------------------------------------------------

    async def start_run(
        self,
        *,
        network: str,
        strategy: str,
        symbols: list[str],
        params: dict[str, Any],
    ) -> None:
        """Start a strategy with the UI settings; raises ValueError if invalid."""
        normalized = [str(symbol).upper() for symbol in symbols]
        if not normalized:
            raise ValueError("at least one symbol is required to start a strategy")

        strategy_obj = build_strategy(strategy, params)

        await self.stop()

        self._network = network
        self._strategy_name = strategy
        self._balances = {}
        self._message = None

        rest = self._ensure_rest(network)
        streams = MarketDataStreams(network)  # type: ignore[arg-type]
        try:
            await streams.start()
        except Exception as exc:  # noqa: BLE001 - surface a clear start failure
            self._state = "error"
            self._message = f"websocket connect failed: {exc}"
            await self.publish_heartbeat()
            raise ValueError(self._message) from exc

        self._streams = streams
        self._strategy = strategy_obj
        services = StrategyServices(
            network=network,
            symbols=normalized,
            rest=rest,
            streams=streams,
            emit=self._on_signal,
        )
        await strategy_obj.on_start(services)

        self._state = "running"
        await self.publish_heartbeat()
        logger.info(
            "strategy started network=%s strategy=%s symbols=%s",
            network,
            strategy,
            normalized,
        )
        self._task = asyncio.create_task(self._run_loop())

    async def _run_loop(self) -> None:
        """Keep the run alive; data arrives through the strategy's WS callbacks."""
        try:
            while True:
                await asyncio.sleep(_BALANCE_REFRESH_SECONDS)
                await self._refresh_balance_quiet()
        except asyncio.CancelledError:
            logger.info("strategy run stopped")
            raise
        except Exception as exc:  # noqa: BLE001 - report and keep the agent alive
            logger.exception("strategy run failed")
            self._state = "error"
            self._message = str(exc)
            await self.publish_heartbeat()

    async def _on_signal(self, signal: Signal) -> None:
        """Handle a strategy signal (execution lane hooks in here later)."""
        logger.info(
            "signal %s %s reason=%s epoch=%s",
            signal.type,
            signal.symbol,
            signal.reason or signal.exit_reason or "-",
            signal.epoch_id or "-",
        )
        await self._publish_event("signal", signal)

    async def _publish_event(self, kind: str, signal: Signal) -> None:
        payload = {
            "kind": kind,
            "type": signal.type,
            "symbol": signal.symbol,
            "reason": signal.reason,
            "exit_reason": signal.exit_reason,
            "epoch_id": signal.epoch_id,
            "emitted_at": signal.emitted_at.isoformat(),
            "provenance": signal.provenance,
        }
        try:
            await self._redis.publish(REDIS_EVENTS_CHANNEL, json.dumps(payload))
        except RedisError:
            logger.warning("cannot publish event (Redis unavailable)")

    async def stop(self) -> None:
        """Stop the running strategy and release its services."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

        strategy, self._strategy = self._strategy, None
        if strategy is not None:
            try:
                await strategy.on_stop()
            except Exception:  # noqa: BLE001 - best-effort teardown
                logger.debug("strategy on_stop failed", exc_info=True)

        streams, self._streams = self._streams, None
        if streams is not None:
            try:
                await streams.stop()
            except Exception:  # noqa: BLE001 - best-effort teardown
                logger.debug("streams stop failed", exc_info=True)

        if self._state != "idle":
            self._state = "idle"
            self._message = None
            self._balances = {}
            await self.publish_heartbeat()

    async def shutdown(self) -> None:
        await self.stop()

    # --- heartbeat ---------------------------------------------------------

    async def publish_heartbeat(self) -> None:
        """Publish current state so the backend can report liveness."""
        payload = json.dumps(
            {
                "state": self._state,
                "message": self._message,
                "network": self._network,
                "strategy": self._strategy_name,
                "balances": self._balances,
                "updated_at": _now().isoformat(),
            }
        )
        try:
            await self._redis.set(REDIS_HEARTBEAT_KEY, payload, ex=HEARTBEAT_TTL)
        except RedisError:
            logger.warning("cannot publish heartbeat (Redis unavailable)")
