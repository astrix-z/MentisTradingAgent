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

from agent import commands
from agent.core import db
from agent.core.config import settings
from agent.engine import HEARTBEAT_INTERVAL, REDIS_COMMAND_CHANNEL, Engine

logger = logging.getLogger(__name__)

_POLL_QUANTUM = 0.2  # how often the listener wakes to check the stop flag
_RECONNECT_DELAY = 2.0


async def _listen(
    redis: Redis, engine: Engine, stop: asyncio.Event
) -> None:
    """Subscribe to the command channel and dispatch incoming commands."""
    while not stop.is_set():
        try:
            async with redis.pubsub() as pubsub:
                await pubsub.subscribe(REDIS_COMMAND_CHANNEL)
                logger.info("listening on channel %s", REDIS_COMMAND_CHANNEL)
                while not stop.is_set():
                    msg = await pubsub.get_message(ignore_subscribe_messages=True)
                    if msg is None:
                        try:
                            await asyncio.wait_for(stop.wait(), timeout=_POLL_QUANTUM)
                        except TimeoutError:
                            pass
                        continue
                    if msg["type"] == "message":
                        await commands.dispatch(engine, redis, msg["data"])
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
        # Restore the last runtime snapshot (e.g. an engine that was running
        # before this agent restarted).
        await commands.resume_from_runtime(engine, redis)
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
