"""Agent command envelope, registry and built-in handlers.

Backend -> agent traffic is *commands*. Each command is a JSON envelope read
off the ``agent:commands:stream`` Redis Stream (a consumer group, not
Pub/Sub — see :mod:`agent.main` for the durable read/dedup loop)::

    {"id": "...", "type": "start", "payload": {...}, "issued_at": "..."}

Handlers are registered by command type via the :func:`command` decorator, so
adding a command is just adding a decorated handler here — the read loop
in :mod:`agent.main` never changes.

Every command may report a *result* (``CommandContext.report``), which the agent
writes atomically to ``agent:result`` (last result) and
``agent:result:<command_id>`` (TTL 3600 seconds) for correlated polling, and
also advances a per-command lifecycle state hash (``agent:command:<id>``:
accepted -> dispatched -> running -> succeeded|failed|timed_out) that
:func:`dispatch` uses to skip re-running a command whose stream entry was
redelivered after it already reached a terminal state.

The runtime snapshot (``agent:runtime``) lets the agent restore its intent after
a restart: on boot we read it and, if it says ``running``, start the strategy
with the stored settings.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError

from agent.core.config import settings
from agent.engine import (
    COMMAND_STATE_TTL_ACTIVE,
    COMMAND_STATE_TTL_TERMINAL,
    REDIS_COMMAND_STATE_PREFIX,
    REDIS_RESULT_KEY,
    REDIS_RUNTIME_KEY,
    TERMINAL_STATUSES,
    Engine,
)
from agent.exchange import ExchangeError
from agent.selection import save_selected, validate_selection
from agent.strategies import STRATEGIES
from agent.symbols import (
    fetch_universe,
    latest_universe,
    publish_universe,
    refresh_tickers,
)

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


def _state_key(command_id: str) -> str:
    return f"{REDIS_COMMAND_STATE_PREFIX}{command_id}"


async def already_terminal(redis: Redis, command_id: str) -> bool:
    """True if this command_id already reached a terminal state.

    Used to skip re-invoking a handler for a redelivered stream entry (a
    crash-recovered restart, or an at-least-once redelivery) without
    re-running an already-succeeded/failed Start. Fails open (returns False)
    on a Redis error: dropping a command silently is worse than a rare
    duplicate run, and handlers are themselves safe to retry (Stop is
    idempotent; Start rolls back cleanly on failure).
    """
    try:
        status = await redis.hget(_state_key(command_id), "status")  # type: ignore[misc]
    except RedisError:
        return False
    return status in TERMINAL_STATUSES


@dataclass
class CommandContext:
    """Everything a handler needs: the engine, Redis and the command itself."""

    engine: Engine
    redis: Redis
    command: AgentCommand

    async def _set_state(self, status: str, *, error: str | None = None) -> None:
        fields = {
            "status": status,
            "type": self.command.type,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        if error is not None:
            fields["error"] = error
        ttl = (
            COMMAND_STATE_TTL_TERMINAL
            if status in TERMINAL_STATUSES
            else COMMAND_STATE_TTL_ACTIVE
        )
        key = _state_key(self.command.id)
        try:
            async with self.redis.pipeline(transaction=True) as pipe:
                pipe.hset(key, mapping=fields)
                pipe.expire(key, ttl)
                await pipe.execute()
        except RedisError:
            logger.warning("cannot update command state (Redis unavailable)")

    async def mark_dispatched(self) -> None:
        """The stream entry was claimed; a handler is about to run."""
        await self._set_state("dispatched")

    async def mark_running(self) -> None:
        """The handler is now actually executing."""
        await self._set_state("running")

    async def report(
        self,
        *,
        ok: bool,
        data: dict[str, Any] | list[Any] | None = None,
        message: str | None = None,
        timed_out: bool = False,
    ) -> None:
        """Publish the command result so the backend can surface it to the UI,
        and advance the lifecycle state hash to its terminal status."""
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
        status = "timed_out" if timed_out else ("succeeded" if ok else "failed")
        await self._set_state(status, error=None if ok else message)


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
    """Parse a command envelope and run its handler, if registered.

    Skips (without erroring) a handler already run to a terminal state for
    this ``command_id`` — the redelivery-safety half of durable delivery; see
    :func:`already_terminal`. Logs ``command_id``, ``type``, ``network`` (when
    present) and elapsed time for every dispatch, without params/secrets.
    """
    try:
        envelope = AgentCommand.from_json(raw)
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning("dropping malformed command envelope: %s", exc)
        return

    handler = HANDLERS.get(envelope.type)
    if handler is None:
        logger.warning("unknown command type=%r (ignored)", envelope.type)
        return

    network = envelope.payload.get("network", "-")
    if await already_terminal(redis, envelope.id):
        logger.info(
            "command_id=%s type=%s network=%s already terminal; skipping redelivery",
            envelope.id,
            envelope.type,
            network,
        )
        return

    ctx = CommandContext(engine=engine, redis=redis, command=envelope)
    await ctx.mark_dispatched()
    await ctx.mark_running()
    started = time.monotonic()
    # "completed" means the handler returned without an unhandled exception —
    # a domain-level rejection the handler itself catches (e.g. invalid start
    # settings) still logs as "completed"; its outcome is in the result/state.
    outcome = "completed"
    reason: str | None = None
    try:
        await handler(ctx, envelope.payload)
    except Exception as exc:
        outcome = "unhandled_exception"
        reason = str(exc)
        logger.exception("command type=%r failed", envelope.type)
        await ctx.report(ok=False, message=str(exc))
    finally:
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        logger.info(
            "command_id=%s type=%s network=%s elapsed_ms=%s outcome=%s%s",
            envelope.id,
            envelope.type,
            network,
            elapsed_ms,
            outcome,
            f" reason={reason}" if reason else "",
        )


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
        await asyncio.wait_for(
            ctx.engine.start_run(
                network=network,
                strategy=strategy,
                symbols=symbols,
                params=params,
            ),
            timeout=settings.COMMAND_IO_TIMEOUT_SECONDS,
        )
    except ValueError as exc:
        logger.warning("start rejected: %s", exc)
        await ctx.report(ok=False, message=str(exc))
        return
    except TimeoutError:
        message = f"start timed out after {settings.COMMAND_IO_TIMEOUT_SECONDS}s"
        logger.warning(message)
        await ctx.report(ok=False, message=message, timed_out=True)
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


@command("symbols")
async def _symbols(ctx: CommandContext, payload: dict[str, Any]) -> None:
    """Replace the marked working symbols.

    The agent owns this list and is the only service that can check it: a marked
    symbol has to be listed, spot-tradable, and not a stablecoin, tokenized
    equity, wrapped token, gold token or a non-Latin name. Rejecting the whole
    request is deliberate — storing part of a selection would leave the user
    believing symbols were marked that were not.
    """
    requested = payload.get("symbols")
    if not isinstance(requested, list):
        await ctx.report(ok=False, message="symbols must be a list")
        return
    if any(not isinstance(symbol, str) for symbol in requested):
        await ctx.report(ok=False, message="symbols must be a list of names")
        return
    wanted = sorted({symbol.strip().upper() for symbol in requested if symbol.strip()})

    document = latest_universe()
    if document is None:
        try:
            document = await asyncio.wait_for(
                fetch_universe(), timeout=settings.COMMAND_IO_TIMEOUT_SECONDS
            )
        except TimeoutError:
            await ctx.report(ok=False, message="reading the exchange timed out")
            return
        except ExchangeError as exc:
            await ctx.report(ok=False, message=f"cannot read the exchange: {exc}")
            return

    rejected = validate_selection(wanted, document)
    if rejected:
        await ctx.report(
            ok=False,
            message="not usable symbols: " + ", ".join(rejected),
        )
        return

    try:
        stored = await save_selected(wanted)
    except (SQLAlchemyError, RuntimeError) as exc:
        logger.warning("cannot store the working symbols: %s", exc)
        await ctx.report(ok=False, message=f"cannot store the selection: {exc}")
        return

    # Republish from the universe already in hand so the page updates at once.
    await publish_universe(ctx.redis, document=document)
    await ctx.report(ok=True, data={"symbols": stored, "count": len(stored)})
    logger.info("working symbols set: %s", len(stored))


@command("prices")
async def _prices(ctx: CommandContext, _payload: dict[str, Any]) -> None:
    """Refresh the universe's last prices (and 24h volumes) on demand.

    Prices go stale between the periodic publishes, and the agent is the only
    service that can read them. The refresh republishes the universe document, so
    the page picks the values up from the document it already reads instead of
    being handed a second shape of the same data.
    """
    logger.info("command prices")
    try:
        document = await asyncio.wait_for(
            refresh_tickers(ctx.redis), timeout=settings.COMMAND_IO_TIMEOUT_SECONDS
        )
    except TimeoutError:
        message = f"prices timed out after {settings.COMMAND_IO_TIMEOUT_SECONDS}s"
        logger.warning(message)
        await ctx.report(ok=False, message=message, timed_out=True)
        return
    if document is None:
        await ctx.report(
            ok=False,
            message="cannot refresh prices; the published list is unchanged",
        )
        return
    entries = document.get("symbols", [])
    priced = sum(1 for entry in entries if entry.get("last_price"))
    await ctx.report(
        ok=True,
        data={
            "prices": priced,
            "total": len(entries),
            "at": document.get("prices_updated_at"),
        },
    )
    logger.info("refreshed prices: %s of %s symbols", priced, len(entries))


@command("balance")
async def _balance(ctx: CommandContext, payload: dict[str, Any]) -> None:
    network = payload.get("network")
    if not network:
        await ctx.report(ok=False, message="network is required")
        return
    network = str(network)
    logger.info("command balance: network=%s", network)
    try:
        assets = await asyncio.wait_for(
            ctx.engine.fetch_account_balance_snapshot(network=network),
            timeout=settings.COMMAND_IO_TIMEOUT_SECONDS,
        )
    except ExchangeError as exc:
        logger.warning("balance command failed: %s", exc)
        await ctx.report(ok=False, message=str(exc))
        return
    except TimeoutError:
        message = f"balance timed out after {settings.COMMAND_IO_TIMEOUT_SECONDS}s"
        logger.warning(message)
        await ctx.report(ok=False, message=message, timed_out=True)
        return
    await ctx.report(
        ok=True,
        data={
            "network": network,
            "fetched_at": datetime.now(UTC).isoformat(),
            "assets": assets,
        },
    )


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
