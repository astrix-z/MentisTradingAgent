"""Agent engine — lifecycle, live plane and the running strategy.

The engine is the *service wrapper* around a strategy: it owns the Redis live
plane (heartbeat + signals), builds the SDK-backed services the strategy needs,
and starts/stops the strategy on command. The strategy is the core; the engine
contains no trading logic.

The system boots idle unless the saved runtime explicitly has ``running=true``.
A ``start`` command carrying the UI settings starts one; ``stop`` tears it down. Exchange access is exclusively
through the adapters in :mod:`agent.exchange`, which wrap ``binance_sdk_spot``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any, Literal

from redis.asyncio import Redis
from redis.exceptions import RedisError

from agent.exchange import (
    ExchangeError,
    ExchangeRest,
    MarketDataStreams,
    balance_map,
    balance_snapshot,
)
from agent.strategies import Signal, Strategy, StrategyServices, build_strategy

logger = logging.getLogger(__name__)
# Dedicated, durable record of ENTRY/EXIT only — configured with its own file
# handler in agent.main._configure_signal_log(); still propagates to the root
# handler too, so nothing disappears from the normal console/docker log.
signal_logger = logging.getLogger("agent.signals")

# Redis keys/streams shared with the backend (keep in sync with
# backend/app/api/routes/trading.py).
REDIS_COMMAND_STREAM = "agent:commands:stream"
REDIS_COMMAND_GROUP = "agent-workers"
# Fixed consumer name: the engine owns exactly one running strategy, so a
# single named consumer is enough, and it lets a restart reclaim its own
# pending (unacked) entries by reading id "0" instead of needing XCLAIM.
REDIS_COMMAND_CONSUMER = "primary"
REDIS_RUNTIME_KEY = "agent:runtime"
REDIS_HEARTBEAT_KEY = "trading:heartbeat"
REDIS_RESULT_KEY = "agent:result"
REDIS_COMMAND_STATE_PREFIX = "agent:command:"
REDIS_EVENTS_CHANNEL = "agent:events"
# Published strategy catalog (settings schema + cross-field constraints) that
# the backend validates Start params against; see agent/protocol.py.
REDIS_STRATEGIES_KEY = "agent:strategies"
# Published tradeable universe (symbols, 24h volume, our classification of each)
# the working-symbols page renders; see agent/symbols.py.
REDIS_SYMBOLS_KEY = "agent:symbols"

# Command state TTLs (keep in sync with backend/app/api/routes/trading.py):
# short while in flight, long once terminal.
COMMAND_STATE_TTL_ACTIVE = 3600
COMMAND_STATE_TTL_TERMINAL = 86400
TERMINAL_STATUSES = ("succeeded", "failed", "timed_out")

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
        self._lifecycle_lock = asyncio.Lock()
        self._startup: asyncio.Task[None] | None = None
        self._network: str = "testnet"
        self._strategy_name: str = ""
        self._state: AgentState = "idle"
        self._message: str | None = None
        self._health_warning: str | None = None
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

    async def fetch_account_balance_snapshot(
        self, network: str
    ) -> list[dict[str, str]]:
        """Return non-zero ``{asset, free, locked}`` rows (Decimal strings) for
        ``network`` — the typed shape returned by the ``balance`` command."""
        rest = self._ensure_rest(network)
        return balance_snapshot(await rest.balances())

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
        if network not in ("testnet", "live"):
            raise ValueError(f"unknown network: {network!r}")
        if (
            not isinstance(symbols, list)
            or not symbols
            or any(
                not isinstance(symbol, str)
                or re.fullmatch(r"[A-Za-z0-9]+", symbol) is None
                for symbol in symbols
            )
        ):
            raise ValueError("symbols must be non-empty alphanumeric strings")
        normalized = list(dict.fromkeys(symbol.upper() for symbol in symbols))
        strategy_obj = build_strategy(strategy, params)

        async with self._lifecycle_lock:
            self._startup = asyncio.create_task(
                self._start_run(network, strategy, normalized, strategy_obj)
            )
            try:
                await self._startup
            finally:
                self._startup = None

    async def _start_run(
        self, network: str, strategy: str, symbols: list[str], strategy_obj: Strategy
    ) -> None:
        try:
            await self._stop_run()
            self._network = network
            self._strategy_name = strategy
            self._balances = {}
            self._message = None
            rest = self._ensure_rest(network)
            self._streams = MarketDataStreams(network)  # type: ignore[arg-type]
            await self._streams.start()
            # Register before on_start: it can acquire resources and then fail.
            self._strategy = strategy_obj
            services = StrategyServices(
                network=network,
                symbols=symbols,
                rest=rest,
                streams=self._streams,
                emit=self._on_signal,
            )
            await strategy_obj.on_start(services)
            self._task = asyncio.create_task(self._run_loop())
            self._state = "running"
            await self.publish_heartbeat()
        except (Exception, asyncio.CancelledError) as exc:
            # Shield teardown from repeated Stop/shutdown cancellation requests.
            cleanup = asyncio.create_task(self._rollback_start(exc))
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
            await cleanup
            raise
        logger.info(
            "strategy started network=%s strategy=%s symbols=%s",
            network,
            strategy,
            symbols,
        )

    async def _rollback_start(self, exc: BaseException) -> None:
        self._state = "error"
        self._message = (
            "strategy start cancelled"
            if isinstance(exc, asyncio.CancelledError)
            else f"strategy start failed: {exc}"
        )
        await self._stop_run(reset_state=False)
        self._rest = None
        self._rest_network = None
        await self.publish_heartbeat()

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
        if signal.type in ("ENTRY", "EXIT"):
            # Logged before the Redis publish: this record must not depend on
            # that call succeeding (see the RedisError path just below).
            signal_logger.info(
                "%s %s network=%s signal_id=%s epoch=%s reason=%s "
                "entry_ref=%s swing_low_ref=%s tp_rule=%s valid_until=%s "
                "exit_reason=%s",
                signal.type,
                signal.symbol,
                self._network,
                signal.signal_id,
                signal.epoch_id or "-",
                signal.reason or "-",
                signal.entry_ref,
                signal.swing_low_ref,
                signal.tp_rule,
                signal.valid_until,
                signal.exit_reason or "-",
            )
        await self._publish_event("signal", signal)

    async def _publish_event(self, kind: str, signal: Signal) -> None:
        payload = {"kind": kind, **signal.transport()}
        await self._redis.publish(REDIS_EVENTS_CHANNEL, json.dumps(payload))

    async def stop(self) -> None:
        """Cancel bootstrap before waiting for the serialized lifecycle lock."""
        if self._startup is not None and not self._startup.done():
            self._startup.cancel()
        async with self._lifecycle_lock:
            await self._stop_run()

    async def _stop_run(self, *, reset_state: bool = True) -> None:
        """Complete teardown even if its caller is cancelled repeatedly."""
        cleanup = asyncio.create_task(self._release_run(reset_state=reset_state))
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        await cleanup
        if cancelled:
            raise asyncio.CancelledError

    async def _release_run(self, *, reset_state: bool) -> None:
        """Release a run, including partially initialized services."""
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

        self._balances = {}
        if reset_state and self._state != "idle":
            self._state = "idle"
            self._message = None
            await self.publish_heartbeat()

    async def shutdown(self) -> None:
        await self.stop()

    # --- heartbeat ---------------------------------------------------------

    def _current_health_warning(self) -> str | None:
        """Degraded-but-running conditions: ``state=running`` alone does not
        mean the strategy is actually seeing the market."""
        warning = None
        if self._state == "running":
            parts = [
                part
                for part in (
                    self._streams.health_warning() if self._streams else None,
                    self._strategy.health_warning if self._strategy else None,
                )
                if part
            ]
            warning = "; ".join(parts) or None
        if warning != self._health_warning:
            if warning:
                logger.warning("run degraded: %s", warning)
            elif self._health_warning:
                logger.info("run recovered: %s", self._health_warning)
            self._health_warning = warning
        return warning

    async def publish_heartbeat(self) -> None:
        """Publish current state so the backend can report liveness."""
        payload = json.dumps(
            {
                "state": self._state,
                "message": self._message or self._current_health_warning(),
                "network": self._network,
                "strategy": self._strategy_name,
                "balances": self._balances,
                "active_symbols": (
                    self._strategy.active_symbols
                    if self._strategy is not None
                    else None
                ),
                "updated_at": _now().isoformat(),
            }
        )
        try:
            await self._redis.set(REDIS_HEARTBEAT_KEY, payload, ex=HEARTBEAT_TTL)
        except RedisError:
            logger.warning("cannot publish heartbeat (Redis unavailable)")
