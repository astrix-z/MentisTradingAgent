"""Agent entry point.

Runs an asyncio daemon that is fully independent of the FastAPI backend.
This is where the exchange websocket connection and the trading loop will live.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from agent.core import db
from agent.core.config import settings

logger = logging.getLogger(__name__)


async def _heartbeat(stop: asyncio.Event) -> None:
    """Placeholder task: keeps the loop alive and visible in logs.

    TODO: replace with the real work loop (exchange websocket, trading logic,
    command polling from the settings DB, result persistence).
    """
    while not stop.is_set():
        logger.info("agent alive: %s", settings.AGENT_NAME)
        try:
            await asyncio.wait_for(stop.wait(), timeout=60)
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
    logger.info(
        "settings DB: %s | external DB: %s",
        "configured" if db.settings_engine() else "NOT configured",
        "configured" if db.external_engine() else "NOT configured",
    )

    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(_heartbeat(stop))
    finally:
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
