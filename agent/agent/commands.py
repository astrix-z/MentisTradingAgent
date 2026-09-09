"""Agent command envelope, registry and built-in handlers.

Backend -> agent traffic is *commands*. Each command is a JSON envelope on the
``agent:commands`` channel:

    {"id": "...", "type": "start", "payload": {...}, "issued_at": "..."}

Handlers are registered by command type via the ``command`` decorator. Adding a
new command is just adding a new decorated handler here (no changes to the
subscription loop in ``main``).

A command may produce a *result* (e.g. ``balance``): the handler publishes it
to a result key through ``CommandContext.report``, and the backend exposes it
to the UI via ``GET /trading/result``.

A *runtime snapshot* key (``agent:runtime``) lets the agent resume its intent
after a restart: on boot we read it and, if it says ``running``, start the
engine with the stored network/strategy.
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

logger = logging.getLogger(__name__)


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
        return cls(
            id=str(data["id"]),
            type_=str(data["type"]),
            payload=data.get("payload") or {},
            issued_at=datetime.fromisoformat(data["issued_at"]),
        )


@dataclass
class CommandContext:
    """Everything a handler needs: the engine, Redis, and the command itself."""

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
            await self.redis.set(REDIS_RESULT_KEY, payload)
        except RedisError:
            logger.warning("cannot publish command result (Redis unavailable)")


# A handler receives a context (engine + redis + the command) and its payload.
CommandHandler = Callable[[CommandContext, dict[str, Any]], Awaitable[None]]

# Command registry: command type -> handler.
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
        command = AgentCommand.from_json(raw)
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning("dropping malformed command envelope: %s", exc)
        return

    handler = HANDLERS.get(command.type)
    if handler is None:
        logger.warning("unknown command type=%r (ignored)", command.type)
        return
    ctx = CommandContext(engine=engine, redis=redis, command=command)
    try:
        await handler(ctx, command.payload)
    except Exception:
        logger.exception("command type=%r failed", command.type)
        await ctx.report(ok=False, message="command handler failed")


# --- Built-in commands ------------------------------------------------------


@command("start")
async def _start(ctx: CommandContext, payload: dict[str, Any]) -> None:
    network = str(payload.get("network") or "testnet")
    strategy = str(payload.get("strategy") or "")
    logger.info("command start: network=%s strategy=%s", network, strategy)
    await ctx.engine.start(network=network, strategy=strategy)
    await ctx.report(
        ok=True, data={"running": True, "network": network, "strategy": strategy}
    )


@command("stop")
async def _stop(ctx: CommandContext, payload: dict[str, Any]) -> None:
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


# --- Boot-time resume ------------------------------------------------------


async def resume_from_runtime(engine: Engine, redis: Redis) -> None:
    """Re-apply the last runtime snapshot so the engine survives restarts."""
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
    if data.get("running"):
        network = str(data.get("network") or "testnet")
        strategy = str(data.get("strategy") or "")
        logger.info(
            "resuming engine from snapshot: network=%s strategy=%s", network, strategy
        )
        await engine.start(network=network, strategy=strategy)
