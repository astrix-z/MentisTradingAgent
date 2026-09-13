"""Agent command envelope, registry and built-in handlers.

Backend -> agent traffic is *commands*. Each command is a JSON envelope on the
``agent:commands`` channel::

    {"id": "...", "type": "start", "payload": {...}, "issued_at": "..."}

Handlers are registered by command type via the :func:`command` decorator, so
adding a command is just adding a decorated handler here — the subscription loop
in :mod:`agent.main` never changes.

Every command may report a *result* (``CommandContext.report``), which the agent
writes atomically to ``agent:result`` (last result) and
``agent:result:<command_id>`` (TTL 3600 seconds) for correlated polling.

The runtime snapshot (``agent:runtime``) lets the agent restore its intent after
a restart: on boot we read it and, if it says ``running``, start the strategy
with the stored settings.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

from agent.engine import REDIS_RESULT_KEY, REDIS_RUNTIME_KEY, Engine
from agent.exchange import ExchangeError
from agent.strategies import STRATEGIES

logger = logging.getLogger(__name__)


def _as_params(value: Any) -> dict[str, Any]:
    """Coerce a command payload slice into a ``str -> Any`` params dict."""
    if not isinstance(value, dict):
        return {}
    return {str(key): val for key, val in value.items()}


def _as_symbols(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    if any(not isinstance(symbol, str) for symbol in value):
        return []
    return [symbol.upper() for symbol in value]


class AgentCommand:
    """A command envelope received from the backend."""

    def __init__(
        self,
        id: str,  # noqa: A002
        type_: str,
        payload: dict[str, Any],
        issued_at: datetime,
    ) -> None:
        self.id = id
        self.type = type_
        self.payload = payload
        self.issued_at = issued_at

    @classmethod
    def from_json(cls, raw: str) -> AgentCommand:
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get("payload", {}), dict):
            raise ValueError("command and payload must be objects")
        return cls(
            id=str(data["id"]),
            type_=str(data["type"]),
            payload=data.get("payload") or {},
            issued_at=datetime.fromisoformat(data["issued_at"]),
        )


@dataclass
class CommandContext:
    """Everything a handler needs: the engine, Redis and the command itself."""

    engine: Engine
    redis: Redis
    command: AgentCommand

    async def report(
        self,
        *,
        ok: bool,
        data: dict[str, Any] | list[Any] | None = None,
        message: str | None = None,
    ) -> None:
        """Publish the command result so the backend can surface it to the UI."""
        payload = json.dumps(
            {
                "command_id": self.command.id,
                "type": self.command.type,
                "ok": ok,
                "data": data,
                "message": message,
                "at": datetime.now(UTC).isoformat(),
            }
        )
        try:
            async with self.redis.pipeline(transaction=True) as pipe:
                pipe.set(REDIS_RESULT_KEY, payload)
                pipe.set(f"{REDIS_RESULT_KEY}:{self.command.id}", payload, ex=3600)
                await pipe.execute()
        except RedisError:
            logger.warning("cannot publish command result (Redis unavailable)")


CommandHandler = Callable[[CommandContext, dict[str, Any]], Awaitable[None]]

#: Registry: command type -> handler.
HANDLERS: dict[str, CommandHandler] = {}


def command(type_: str) -> Callable[[CommandHandler], CommandHandler]:
    """Decorator that registers a function as the handler for ``type_``."""

    def register(fn: CommandHandler) -> CommandHandler:
        HANDLERS[type_] = fn
        return fn

    return register


async def dispatch(engine: Engine, redis: Redis, raw: str) -> None:
    """Parse a command envelope and run its handler, if registered."""
    try:
        envelope = AgentCommand.from_json(raw)
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning("dropping malformed command envelope: %s", exc)
        return

    handler = HANDLERS.get(envelope.type)
    if handler is None:
        logger.warning("unknown command type=%r (ignored)", envelope.type)
        return
    ctx = CommandContext(engine=engine, redis=redis, command=envelope)
    try:
        await handler(ctx, envelope.payload)
    except Exception as exc:
        logger.exception("command type=%r failed", envelope.type)
        await ctx.report(ok=False, message=str(exc))


# --- Built-in commands ------------------------------------------------------


@command("start")
async def _start(ctx: CommandContext, payload: dict[str, Any]) -> None:
    network = str(payload.get("network") or "testnet")
    strategy = str(payload.get("strategy") or "")
    symbols = _as_symbols(payload.get("symbols"))
    params = _as_params(payload.get("params"))
    logger.info(
        "command start: network=%s strategy=%s symbols=%s params=%s",
        network,
        strategy,
        symbols,
        sorted(params),
    )
    try:
        await ctx.engine.start_run(
            network=network,
            strategy=strategy,
            symbols=symbols,
            params=params,
        )
    except ValueError as exc:
        logger.warning("start rejected: %s", exc)
        await ctx.report(ok=False, message=str(exc))
        return
    await ctx.report(
        ok=True,
        data={
            "running": True,
            "network": network,
            "strategy": strategy,
            "symbols": symbols,
        },
    )


@command("stop")
async def _stop(ctx: CommandContext, _payload: dict[str, Any]) -> None:
    logger.info("command stop")
    await ctx.engine.stop()
    await ctx.report(ok=True, data={"running": False})


@command("balance")
async def _balance(ctx: CommandContext, payload: dict[str, Any]) -> None:
    network = str(payload.get("network") or ctx.engine.network)
    logger.info("command balance: network=%s", network)
    try:
        balances = await ctx.engine.fetch_account_balance(network=network)
    except ExchangeError as exc:
        logger.warning("balance command failed: %s", exc)
        await ctx.report(ok=False, message=str(exc))
        return
    await ctx.report(ok=True, data=balances)


# --- Boot-time resume -------------------------------------------------------


async def resume_from_runtime(engine: Engine, redis: Redis) -> None:
    """Boot idle unless the saved snapshot has exactly ``running=true``.

    Preserve the existing opt-in-by-snapshot resume behavior; no new boot mode.
    Failure is isolated from the listener and heartbeat, and startup is cancellable.
    """
    try:
        raw = await redis.get(REDIS_RUNTIME_KEY)
    except RedisError:
        logger.warning("cannot read runtime snapshot (Redis unavailable)")
        return
    if not raw:
        return
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("ignoring malformed runtime snapshot")
        return

    if not isinstance(data, dict) or data.get("running") is not True:
        return
    network = str(data.get("network") or "testnet")
    strategy = str(data.get("strategy") or "")
    symbols = _as_symbols(data.get("symbols"))
    params = _as_params(data.get("params"))
    if strategy not in STRATEGIES or not symbols:
        logger.warning("runtime snapshot cannot be resumed (strategy/symbols)")
        return
    logger.info(
        "resuming strategy from snapshot: network=%s strategy=%s symbols=%s",
        network,
        strategy,
        symbols,
    )
    try:
        await engine.start_run(
            network=network, strategy=strategy, symbols=symbols, params=params
        )
    except Exception as exc:
        logger.exception("resume failed: %s", exc)
