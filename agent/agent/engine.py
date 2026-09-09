"""Agent trading engine + Redis keys/heartbeat.

The engine is what the agent *actually* executes (network, strategy, protector)
and reports liveness through a heartbeat. It is driven by commands dispatched
from ``agent.commands`` (start/stop/balance).

The engine has two execution modes behind one ``_task``:

* a *run* mode (``start_run``) that composes the trading pipeline and drives it
  with a tick loop: market quotes -> fill reconciliation -> strategy -> protector;
* a legacy *balance* mode (``start``) that just keeps the account balance
  refreshed, kept for the current command/resume path until commands carry the
  full run parameters.

Commands and liveness between the backend and the agent travel **only** over
Redis (the "live plane"); no service reads or writes another service's DB.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any, Literal

from redis.asyncio import Redis
from redis.exceptions import RedisError

from agent.exchange import AssetBalance, BinanceSpot, ExchangeError
from agent.exchange.execution import ExecutionService
from agent.exchange.streams import MarketDataFeed
from agent.fills import FillTracker
from agent.marketdata import MarketData
from agent.portfolio import Portfolio
from agent.protection import PROTECTORS, ProtectionContext, Protector
from agent.risk import RiskLimits, RiskManager
from agent.strategies import STRATEGIES, Strategy, StrategyContext

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

# Refresh the account balance (for the heartbeat) once per this many ticks.
_BALANCE_EVERY_TICKS = 10


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


def _risk_limits_from(params: dict[str, Any]) -> RiskLimits:
    """Build risk limits from optional ``params["risk_limits"]`` overrides."""
    raw = params.get("risk_limits")
    defaults = RiskLimits()
    if not isinstance(raw, dict):
        return defaults
    return RiskLimits(
        max_symbols=int(raw.get("max_symbols", defaults.max_symbols)),
        max_position_notional=float(
            raw.get("max_position_notional", defaults.max_position_notional)
        ),
        max_total_notional=float(
            raw.get("max_total_notional", defaults.max_total_notional)
        ),
        max_open_orders=int(raw.get("max_open_orders", defaults.max_open_orders)),
        max_daily_loss=float(raw.get("max_daily_loss", defaults.max_daily_loss)),
    )


class Engine:
    """Executes trading on a network + strategy/protector and reports liveness."""

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
        # Composed trading pipeline (present only while a run is active).
        self._market: MarketData | None = None
        self._feed: MarketDataFeed | None = None
        self._portfolio: Portfolio | None = None
        self._execution: ExecutionService | None = None
        self._risk: RiskManager | None = None
        self._fills: FillTracker | None = None
        self._strategy_obj: Strategy | None = None
        self._protector_obj: Protector | None = None
        self._sctx: StrategyContext | None = None
        self._pctx: ProtectionContext | None = None
        self._symbols: list[str] = []

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
        callers can decide how to handle it.
        """
        target = network or self._network
        self._exchange = self._ensure_exchange(target)
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

    def _ensure_exchange(self, network: str) -> BinanceSpot:
        if self._exchange is None or self._connected_network != network:
            self._exchange = BinanceSpot.connect(network)
            self._connected_network = network
        return self._exchange

    # --- legacy balance mode (current command/resume path) --------------------

    async def start(self, network: str, strategy: str) -> None:
        """(Re)start the engine in balance mode with the given network/strategy."""
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
        self._task = asyncio.create_task(self._balance_loop())

    async def start_run(
        self,
        *,
        network: str,
        strategy: str,
        protector: str,
        symbols: list[str],
        params: dict[str, Any],
    ) -> None:
        """(Re)start a full trading run composing the whole pipeline."""
        await self.stop()
        if strategy not in STRATEGIES:
            raise ValueError(f"unknown strategy: {strategy!r}")
        if protector not in PROTECTORS:
            raise ValueError(f"unknown protector: {protector!r}")

        normalized = [str(s).upper() for s in symbols]
        if not normalized:
            raise ValueError("at least one symbol is required to start trading")

        self._network = network
        self._strategy = strategy
        self._state = "running"
        self._message = None
        self._balances = {}

        market = MarketData()
        feed = MarketDataFeed(market)
        exchange = self._ensure_exchange(network)
        portfolio = Portfolio()
        execution = ExecutionService(exchange)
        risk = RiskManager(
            portfolio=portfolio,
            market=market,
            limits=_risk_limits_from(params),
            orders_provider=lambda: execution.open_orders(),
        )
        fills = FillTracker(portfolio, execution)

        config = dict(params)
        config["symbols"] = normalized

        strategy_obj: Strategy = STRATEGIES[strategy](config)
        protector_obj: Protector = PROTECTORS[protector](config)
        sctx = StrategyContext(
            execution=execution,
            portfolio=portfolio,
            market=market,
            risk=risk,
            config=config,
            fills=fills,
        )
        pctx = ProtectionContext(
            execution=execution,
            portfolio=portfolio,
            market=market,
            risk=risk,
            config=config,
            fills=fills,
        )

        self._market = market
        self._feed = feed
        self._portfolio = portfolio
        self._execution = execution
        self._risk = risk
        self._fills = fills
        self._strategy_obj = strategy_obj
        self._protector_obj = protector_obj
        self._sctx = sctx
        self._pctx = pctx
        self._symbols = normalized

        await feed.start(network, normalized)
        await strategy_obj.on_start(sctx)
        await protector_obj.on_start(pctx)
        # Reconcile once now so orders placed at start-up are already watched.
        await self._reconcile_all()
        await self.publish_heartbeat()
        logger.info(
            "engine run started network=%s strategy=%s protector=%s symbols=%s",
            network,
            strategy,
            protector,
            normalized,
        )
        self._task = asyncio.create_task(self._run_loop())

    # --- run tick loop --------------------------------------------------------

    async def _run_loop(self) -> None:
        assert self._market is not None  # only started after a successful start_run
        ticks = 0
        try:
            while True:
                if not await self._market.wait_updated():
                    continue
                await self._reconcile_all()
                for symbol in list(self._symbols):
                    await self._tick(symbol)
                ticks += 1
                if ticks % _BALANCE_EVERY_TICKS == 0:
                    await self._refresh_balance_quiet()
        except asyncio.CancelledError:
            logger.info("engine run stopped")
            raise
        except Exception as exc:
            logger.exception("engine run failed")
            self._state = "error"
            self._message = str(exc)
            await self.publish_heartbeat()

    async def _tick(self, symbol: str) -> None:
        """One unit of work for a symbol: reconcile fills, run strategy+protector."""
        if self._fills is not None:
            await self._fills.reconcile(symbol)
        if self._strategy_obj is not None and self._sctx is not None:
            await self._strategy_obj.on_tick(self._sctx, symbol)
        if self._protector_obj is not None and self._pctx is not None:
            await self._protector_obj.on_tick(self._pctx, symbol)

    async def _reconcile_all(self) -> None:
        if self._fills is not None:
            await self._fills.reconcile()

    async def _refresh_balance_quiet(self) -> None:
        try:
            await self.fetch_account_balance(self._network)
        except ExchangeError as exc:
            logger.warning("balance refresh failed: %s", exc)
            self._balances = {}

    # --- lifecycle --------------------------------------------------------------

    async def stop(self) -> None:
        """Stop the engine, tear down the trading pipeline and go idle."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        await self._teardown_run()
        if self._state != "idle":
            self._state = "idle"
            self._message = None
            self._exchange = None
            self._connected_network = None
            self._balances = {}
            await self.publish_heartbeat()

    async def _teardown_run(self) -> None:
        """Ask strategy/protector to clean up and stop the market feed."""
        if self._strategy_obj is not None and self._sctx is not None:
            try:
                await self._strategy_obj.on_stop(self._sctx)
            except Exception:  # noqa: BLE001 - best-effort teardown
                logger.debug("strategy on_stop failed", exc_info=True)
        if self._protector_obj is not None and self._pctx is not None:
            try:
                await self._protector_obj.on_stop(self._pctx)
            except Exception:  # noqa: BLE001 - best-effort teardown
                logger.debug("protector on_stop failed", exc_info=True)
        if self._feed is not None:
            try:
                await self._feed.stop()
            except Exception:  # noqa: BLE001 - best-effort teardown
                logger.debug("feed stop failed", exc_info=True)

        self._market = None
        self._feed = None
        self._portfolio = None
        self._execution = None
        self._risk = None
        self._fills = None
        self._strategy_obj = None
        self._protector_obj = None
        self._sctx = None
        self._pctx = None
        self._symbols = []

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

    async def _balance_loop(self) -> None:
        logger.info(
            "engine started (balance mode) network=%s strategy=%s",
            self._network,
            self._strategy,
        )
        try:
            while True:
                await self._refresh_balance_quiet()
                await asyncio.sleep(HEARTBEAT_INTERVAL)
        except asyncio.CancelledError:
            logger.info("engine stopped")
            raise
        except Exception as exc:
            logger.exception("engine failed")
            self._state = "error"
            self._message = str(exc)
            await self.publish_heartbeat()
