"""Agent trading engine + Redis keys/heartbeat.

The engine holds what the agent is *actually* executing (network, strategy,
state) and publishes a liveness heartbeat. It is driven by commands dispatched
from ``agent.commands`` (start/stop/...).

Commands and liveness between the backend and the agent travel **only** over
Redis (the "live plane"); no service reads or writes another service's DB.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Literal

from redis.asyncio import Redis
from redis.exceptions import RedisError

from agent.exchange import AssetBalance, BinanceSpot, ExchangeError

logger = logging.getLogger(__name__)

# Redis keys/channel shared with the backend (keep in sync with
# backend/app/api/routes/trading.py).
REDIS_COMMAND_CHANNEL = "agent:commands"
REDIS_RUNTIME_KEY = "agent:runtime"
REDIS_HEARTBEAT_KEY = "trading:heartbeat"
REDIS_RESULT_KEY = "agent:result"

# How often the heartbeat is published. Keep below the backend's staleness
# threshold (backend/app/api/routes/trading.py: HEARTBEAT_STALE_SECONDS).
HEARTBEAT_INTERVAL = 5.0
# TTL for the heartbeat key; a safety net so a dead agent stops reading online.
HEARTBEAT_TTL = 30

AgentState = Literal["running", "idle", "error"]


def _now() -> datetime:
    return datetime.now(UTC)


def _encode_heartbeat(
    state: AgentState,
    message: str | None,
    network: str,
    strategy: str,
    balances: dict[str, float],
) -> str:
    return json.dumps(
        {
            "state": state,
            "message": message,
            "network": network,
            "strategy": strategy,
            "balances": balances,
            "updated_at": _now().isoformat(),
        }
    )


class Engine:
    """Executes trading on a network + strategy and reports liveness.

    The real exchange websocket/trading loop will replace ``_trading_loop``
    (see the empty ``agent.exchange`` package).
    """

    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._task: asyncio.Task[None] | None = None
        self._network: str = "testnet"
        self._strategy: str = ""
        self._state: AgentState = "idle"
        self._message: str | None = None
        self._exchange: BinanceSpot | None = None
        self._connected_network: str | None = None
        self._balances: dict[str, float] = {}

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
        """Most recently fetched account balances (asset -> free), if any."""
        return dict(self._balances)

    async def fetch_account_balance(self, network: str | None = None) -> dict[str, float]:
        """(Re)connect if needed and fetch the account balance now.

        ``network`` may override the engine's current network (e.g. an on-demand
        balance command for another network). Returns a mapping ``asset -> free``
        for non-zero balances. Raises :class:`ExchangeError` on failure so
        callers can decide how to handle it (the trading loop catches it; a
        command handler surfaces it to the user).
        """
        target = network or self._network
        if self._exchange is None or self._connected_network != target:
            self._exchange = BinanceSpot.connect(target)
            self._connected_network = target
        result: list[AssetBalance] = await self._exchange.fetch_balances()

        # Only cache into the heartbeat when it matches what the engine runs;
        # an on-demand fetch for another network must not clobber running state.
        if target == self._network:
            self._balances = {
                item.asset: item.free
                for item in result
                if item.free > 0 or item.locked > 0
            }
            logger.info("account balance refreshed: %s assets", len(self._balances))
        return {
            item.asset: item.free for item in result if item.free > 0 or item.locked > 0
        }

    async def start(self, network: str, strategy: str) -> None:
        """(Re)start the engine with the given network and strategy."""
        already = self.running and self._network == network and self._strategy == strategy
        if already:
            return
        await self.stop()
        self._network = network
        self._strategy = strategy
        self._state = "running"
        self._message = None
        self._exchange = None
        self._connected_network = None
        self._balances = {}
        await self.publish_heartbeat()
        self._task = asyncio.create_task(self._trading_loop())

    async def stop(self) -> None:
        """Stop the engine and go idle."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        if self._state != "idle":
            self._state = "idle"
            self._message = None
            self._exchange = None
            self._connected_network = None
            self._balances = {}
            await self.publish_heartbeat()

    async def shutdown(self) -> None:
        await self.stop()

    async def publish_heartbeat(self) -> None:
        """Publish current state so the backend can report liveness."""
        try:
            payload = _encode_heartbeat(
                self._state,
                self._message,
                self._network,
                self._strategy,
                self._balances,
            )
            await self._redis.set(REDIS_HEARTBEAT_KEY, payload, ex=HEARTBEAT_TTL)
        except RedisError:
            logger.warning("cannot publish heartbeat (Redis unavailable)")

    async def _trading_loop(self) -> None:
        logger.info(
            "engine started network=%s strategy=%s", self._network, self._strategy
        )
        try:
            # TODO: real exchange websocket + trading logic lives here. Load the
            # actual strategy by self._strategy; for now we connect to the
            # exchange on self._network and keep the account balance refreshed.
            while True:
                try:
                    await self.fetch_account_balance(self._network)
                except ExchangeError as exc:
                    # Non-fatal: report and keep trying on the next tick.
                    logger.warning("balance refresh failed: %s", exc)
                    self._balances = {}
                await asyncio.sleep(HEARTBEAT_INTERVAL)
        except asyncio.CancelledError:
            logger.info("engine stopped")
            raise
        except Exception as exc:
            logger.exception("engine failed")
            self._state = "error"
            self._message = str(exc)
            await self.publish_heartbeat()
