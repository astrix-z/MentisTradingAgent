"""Agent entry point.

Runs an asyncio daemon that is fully independent of the FastAPI backend.
This is where the exchange websocket connection and the trading loop will live.

The agent talks to the backend **only** through Redis (commands + liveness),
never directly and never through a shared database: it subscribes to the
command channel, dispatches each command to its handler, and reports liveness
via a heartbeat key.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from redis.asyncio import Redis
from redis.exceptions import RedisError

from agent.common import commands
from agent.core import db
from agent.core.config import settings
from agent.engine import HEARTBEAT_INTERVAL, REDIS_COMMAND_CHANNEL, Engine

logger = logging.getLogger(__name__)

_POLL_QUANTUM = 0.2  # how often the listener wakes to check the stop flag
_RECONNECT_DELAY = 2.0


class _CommandQueue:
    """FIFO execution with Stop as a barrier for older, unfinished starts.

    Only one handler runs at a time. Stop cancels active bootstrap/resume and
    invalidates queued starts preceding it, not commands arriving after it.
    """

    def __init__(self, redis: Redis, engine: Engine) -> None:
        self.redis = redis
        self.engine = engine
        self.queue: asyncio.Queue[tuple[str | None, int]] = asyncio.Queue()
        self.generation = 0
        self.active: asyncio.Task[None] | None = None
        self.active_type: str | None = None

    def submit(self, raw: str) -> None:
        try:
            envelope = commands.AgentCommand.from_json(raw)
        except ValueError, KeyError, TypeError:
            logger.warning("dropping malformed command envelope")
            return
        if envelope.type == "stop":
            self.generation += 1
            if self.active is not None and self.active_type in ("start", "resume"):
                self.active.cancel()
        self.queue.put_nowait((raw, self.generation))

    async def run(self) -> None:
        while True:
            raw, generation = await self.queue.get()
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
                        raise
            finally:
                self.active = None
                self.active_type = None
                self.queue.task_done()


async def _listen(redis: Redis, engine: Engine, stop: asyncio.Event) -> None:
    """Subscribe before resume; receiving commands never waits for bootstrap."""
    control = _CommandQueue(redis, engine)
    worker = asyncio.create_task(control.run())
    try:
        await _receive(redis, stop, control)
    finally:
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass


async def _receive(redis: Redis, stop: asyncio.Event, control: _CommandQueue) -> None:
    resume_pending = True
    while not stop.is_set():
        try:
            async with redis.pubsub() as pubsub:
                await pubsub.subscribe(REDIS_COMMAND_CHANNEL)
                logger.info("listening on channel %s", REDIS_COMMAND_CHANNEL)
                if resume_pending:
                    control.queue.put_nowait((None, control.generation))
                    resume_pending = False
                while not stop.is_set():
                    msg = await pubsub.get_message(ignore_subscribe_messages=True)
                    if msg is None:
                        try:
                            await asyncio.wait_for(stop.wait(), timeout=_POLL_QUANTUM)
                        except TimeoutError:
                            pass
                        continue
                    if msg["type"] == "message":
                        control.submit(msg["data"])
        except RedisError as exc:
            logger.warning("command channel error: %s; retrying", exc)
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

    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(_listen(redis, engine, stop))
            tg.create_task(_heartbeat_loop(engine, stop))
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
