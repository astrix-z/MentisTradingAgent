"""Agent entry point.

Runs an asyncio daemon that is fully independent of the FastAPI backend.
This is where the exchange websocket connection and the trading loop will live.

The agent talks to the backend **only** through Redis (commands + liveness),
never directly and never through a shared database: it reads commands off a
durable Redis Stream through a consumer group (at-least-once, survives an
agent restart/reconnect — see the Prompt 5 design note in
``backend/app/api/routes/trading.py``), dispatches each to its handler, and
reports liveness via a heartbeat key.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from redis.asyncio import Redis
from redis.exceptions import RedisError, ResponseError

from agent.common import commands
from agent.core import db
from agent.core.config import settings
from agent.engine import (
    HEARTBEAT_INTERVAL,
    REDIS_COMMAND_CONSUMER,
    REDIS_COMMAND_GROUP,
    REDIS_COMMAND_STREAM,
    Engine,
)
from agent.protocol import publish_catalog
from agent.symbols import publish_universe

logger = logging.getLogger(__name__)

_POLL_QUANTUM = 0.2  # how often the listener wakes to check the stop flag
_RECONNECT_DELAY = 2.0
_READ_COUNT = 10  # entries per XREADGROUP call
# The catalog only changes with a deploy, but it is republished periodically so
# a Redis restart (which drops the key, the registry being static in memory)
# cannot leave the backend without a schema to validate against.
_CATALOG_REFRESH_SECONDS = 300.0
# The universe changes as symbols are listed, halted and delisted, and its 24h
# volumes are always moving, so it is refreshed on the same cadence.
_SYMBOLS_REFRESH_SECONDS = 300.0


async def _ensure_group(redis: Redis) -> None:
    """Idempotently create the consumer group at the start of the stream.

    Starting at id ``0`` (not ``$``) means a brand-new group still picks up
    anything the backend already enqueued before the agent's first-ever
    connection — no command is silently skipped just because the group is new.
    """
    try:
        await redis.xgroup_create(
            REDIS_COMMAND_STREAM, REDIS_COMMAND_GROUP, id="0", mkstream=True
        )
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def _ack(redis: Redis, entry_id: str) -> None:
    """Best-effort XACK. A failed ack just means a harmless redelivery later
    (protected by :func:`agent.common.commands.already_terminal`)."""
    try:
        await redis.xack(REDIS_COMMAND_STREAM, REDIS_COMMAND_GROUP, entry_id)
    except RedisError:
        logger.warning("cannot ack command entry_id=%s (Redis unavailable)", entry_id)


class _CommandQueue:
    """FIFO execution with Stop as a barrier for older, unfinished starts.

    Only one handler runs at a time. Stop cancels active bootstrap/resume and
    invalidates queued starts preceding it, not commands arriving after it.
    Observation-only commands never enter this queue (see ``_handle_independent``)
    so a slow balance or price fetch can never make Stop wait behind it.
    """

    def __init__(self, redis: Redis, engine: Engine) -> None:
        self.redis = redis
        self.engine = engine
        self.queue: asyncio.Queue[tuple[str | None, int, str | None]] = asyncio.Queue()
        self.generation = 0
        self.active: asyncio.Task[None] | None = None
        self.active_type: str | None = None

    def submit(self, raw: str, ack_id: str) -> None:
        try:
            envelope = commands.AgentCommand.from_json(raw)
        except (ValueError, KeyError, TypeError):
            logger.warning("dropping malformed command envelope")
            return
        if envelope.type == "stop":
            self.generation += 1
            if self.active is not None and self.active_type in ("start", "resume"):
                self.active.cancel()
        self.queue.put_nowait((raw, self.generation, ack_id))

    async def run(self) -> None:
        while True:
            raw, generation, ack_id = await self.queue.get()
            should_ack = True
            try:
                envelope = (
                    commands.AgentCommand.from_json(raw) if raw is not None else None
                )
                self.active_type = envelope.type if envelope is not None else "resume"
                if generation != self.generation and self.active_type in (
                    "start",
                    "resume",
                ):
                    if envelope is not None:
                        await commands.CommandContext(
                            self.engine, self.redis, envelope
                        ).report(ok=False, message="start cancelled by Stop")
                    continue
                self.active = asyncio.create_task(
                    commands.resume_from_runtime(self.engine, self.redis)
                    if raw is None
                    else commands.dispatch(self.engine, self.redis, raw)
                )
                try:
                    await self.active
                except asyncio.CancelledError:
                    if envelope is not None:
                        await commands.CommandContext(
                            self.engine, self.redis, envelope
                        ).report(
                            ok=False, message="command cancelled by Stop or shutdown"
                        )
                    current = asyncio.current_task()
                    if current is not None and current.cancelling():
                        # Propagating for shutdown: leave the entry unacked so
                        # a restart redelivers (and, if truly unfinished,
                        # retries) it.
                        should_ack = False
                        raise
            finally:
                self.active = None
                self.active_type = None
                if should_ack and ack_id is not None:
                    await _ack(self.redis, ack_id)
                self.queue.task_done()


async def _handle_independent(
    redis: Redis, engine: Engine, raw: str, ack_id: str
) -> None:
    """Run one observation-only command as an independent task, outside the
    serialized start/stop queue, and ack once it's actually done (success or
    failure)."""
    try:
        await commands.dispatch(engine, redis, raw)
    finally:
        await _ack(redis, ack_id)


#: Command types that read the outside world and change nothing about the
#: running intent. They must not queue behind (or in front of) Start/Stop.
_INDEPENDENT_COMMANDS = ("balance", "prices")


async def _route(
    redis: Redis,
    engine: Engine,
    control: _CommandQueue,
    background_tasks: set[asyncio.Task[None]],
    entry_id: str,
    fields: dict[str, str],
) -> None:
    raw = fields.get("data")
    if raw is None:
        logger.warning("dropping command entry without a data field (acked)")
        await _ack(redis, entry_id)
        return
    try:
        kind = commands.AgentCommand.from_json(raw).type
    except (ValueError, KeyError, TypeError):
        logger.warning("dropping malformed command envelope (acked)")
        await _ack(redis, entry_id)
        return
    if kind in _INDEPENDENT_COMMANDS:
        task = asyncio.create_task(_handle_independent(redis, engine, raw, entry_id))
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
    else:
        control.submit(raw, entry_id)


async def _listen(redis: Redis, engine: Engine, stop: asyncio.Event) -> None:
    """Subscribe before resume; receiving commands never waits for bootstrap."""
    control = _CommandQueue(redis, engine)
    background_tasks: set[asyncio.Task[None]] = set()
    worker = asyncio.create_task(control.run())
    try:
        await _receive(redis, engine, stop, control, background_tasks)
    finally:
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
        for task in list(background_tasks):
            task.cancel()
        for task in list(background_tasks):
            try:
                await task
            except asyncio.CancelledError:
                pass


async def _receive(
    redis: Redis,
    engine: Engine,
    stop: asyncio.Event,
    control: _CommandQueue,
    background_tasks: set[asyncio.Task[None]],
) -> None:
    resume_pending = True
    while not stop.is_set():
        try:
            await _ensure_group(redis)
            logger.info(
                "reading command stream %s (group=%s consumer=%s)",
                REDIS_COMMAND_STREAM,
                REDIS_COMMAND_GROUP,
                REDIS_COMMAND_CONSUMER,
            )
            if resume_pending:
                control.queue.put_nowait((None, control.generation, None))
                resume_pending = False
            # Drain this consumer's own unacked backlog (id "0") before
            # waiting for new entries (">") — this is what redelivers a
            # command across an agent restart/crash.
            read_id = "0"
            while not stop.is_set():
                response = await redis.xreadgroup(
                    REDIS_COMMAND_GROUP,
                    REDIS_COMMAND_CONSUMER,
                    {REDIS_COMMAND_STREAM: read_id},
                    count=_READ_COUNT,
                    block=int(_POLL_QUANTUM * 1000),
                )
                if not response:
                    if read_id == "0":
                        read_id = ">"
                    continue
                for _stream_name, entries in response:
                    if read_id == "0" and len(entries) < _READ_COUNT:
                        read_id = ">"
                    for entry_id, fields in entries:
                        await _route(
                            redis, engine, control, background_tasks, entry_id, fields
                        )
        except RedisError as exc:
            logger.warning("command stream error: %s; retrying", exc)
            try:
                await asyncio.wait_for(stop.wait(), timeout=_RECONNECT_DELAY)
            except TimeoutError:
                pass


async def _heartbeat_loop(engine: Engine, stop: asyncio.Event) -> None:
    """Keep the agent visible: publish current state at a fixed interval."""
    while not stop.is_set():
        await engine.publish_heartbeat()
        try:
            await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_INTERVAL)
        except TimeoutError:
            pass


async def _catalog_loop(redis: Redis, stop: asyncio.Event) -> None:
    """Keep the published strategy catalog present and current."""
    while not stop.is_set():
        await publish_catalog(redis)
        try:
            await asyncio.wait_for(stop.wait(), timeout=_CATALOG_REFRESH_SECONDS)
        except TimeoutError:
            pass


async def _symbols_loop(redis: Redis, stop: asyncio.Event) -> None:
    """Keep the published tradeable universe present and current.

    Runs in its own task: the two exchange calls it makes are independent of
    commands and of the heartbeat, and a slow or unreachable exchange must not
    delay either.
    """
    while not stop.is_set():
        await publish_universe(redis)
        try:
            await asyncio.wait_for(stop.wait(), timeout=_SYMBOLS_REFRESH_SECONDS)
        except TimeoutError:
            pass


async def run() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Not available on all platforms (e.g. Windows); fall back to
            # KeyboardInterrupt handling in main().
            pass

    logger.info(
        "starting agent %s (environment=%s)",
        settings.AGENT_NAME,
        settings.ENVIRONMENT,
    )
    logger.info("control plane (Redis): %s", settings.REDIS_URL)

    redis: Redis = Redis.from_url(str(settings.REDIS_URL), decode_responses=True)
    engine = Engine(redis)

    # The agent owns its own database (working symbols now, trades later). It is
    # optional at boot, so a missing or unreachable one is reported and the
    # daemon carries on: trading must not depend on it.
    try:
        if await db.ensure_settings_database():
            await db.init_settings_schema()
    except Exception:  # noqa: BLE001 - never let settings storage block boot
        logger.exception("cannot prepare the agent settings database")

    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(_listen(redis, engine, stop))
            tg.create_task(_heartbeat_loop(engine, stop))
            tg.create_task(_catalog_loop(redis, stop))
            tg.create_task(_symbols_loop(redis, stop))
    finally:
        await engine.shutdown()
        await redis.aclose()
        await db.dispose_engines()
        logger.info("agent stopped")


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
