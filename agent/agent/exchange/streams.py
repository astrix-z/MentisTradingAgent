"""Binance Spot websocket market streams — thin adapter over ``binance_sdk_spot``.

Mirrors :mod:`agent.exchange.rest`: it is an adapter, not a re-implementation.
The SDK owns the connection, the subscriptions, the reconnection and the message
parsing; this module only:

* chooses the network-specific stream URL,
* opens one connection and tracks the ``RequestStreamHandle`` objects,
* forwards typed SDK messages to the caller's callback.

Message callbacks receive the SDK's own models (``KlineResponse``,
``TickerResponse``, ``BookTickerResponse``) — no parallel data model is defined.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from binance_common.configuration import ConfigurationWebSocketStreams
from binance_sdk_spot import (
    SPOT_WS_STREAMS_PROD_URL,
    SPOT_WS_STREAMS_TESTNET_URL,
)
from binance_sdk_spot.spot import Spot
from binance_sdk_spot.websocket_streams.models import (
    BookTickerResponse,
    KlineIntervalEnum,
    KlineResponse,
    TickerResponse,
)

from agent.exchange.rest import Network

logger = logging.getLogger(__name__)

# Stream base URL per network (single source of truth is the SDK constants).
_STREAM_URL: dict[str, str] = {
    "live": SPOT_WS_STREAMS_PROD_URL,
    "testnet": SPOT_WS_STREAMS_TESTNET_URL,
}

KlineCallback = Callable[[KlineResponse], None]
TickerCallback = Callable[[TickerResponse], None]
BookTickerCallback = Callable[[BookTickerResponse], None]


class MarketDataStreams:
    """One websocket connection to the market-data stream endpoint.

    Not thread-safe and not re-entrant: create one per run. The SDK re-subscribes
    the tracked streams automatically after a reconnect, so no reconnect logic
    lives here.
    """

    def __init__(self, network: Network) -> None:
        self._network = network
        config = ConfigurationWebSocketStreams(stream_url=_STREAM_URL[network])
        self._client = Spot(config_ws_streams=config).websocket_streams
        self._connection: Any = None
        self._handles: list[Any] = []

    @property
    def network(self) -> Network:
        return self._network

    @property
    def connected(self) -> bool:
        return self._connection is not None

    async def start(self) -> None:
        """Open the underlying websocket connection (idempotent)."""
        if self._connection is not None:
            return
        self._connection = await self._client.create_connection()  # type: ignore[no-untyped-call]
        if not self._client.connections:
            raise ConnectionError(
                f"websocket connect to {self._network!r} failed (no connections)"
            )
        logger.info("market streams connected network=%s", self._network)

    async def subscribe_kline(
        self, *, symbol: str, interval: str, callback: KlineCallback
    ) -> None:
        """Subscribe to ``<symbol>@kline_<interval>`` and forward closed bars."""
        await self._ensure_started()
        handle = await self._client.kline(
            symbol=symbol.lower(),
            interval=KlineIntervalEnum(interval).value,  # type: ignore[arg-type]
        )
        handle.on("message", callback)
        self._handles.append(handle)

    async def subscribe_ticker(self, *, symbol: str, callback: TickerCallback) -> None:
        """Subscribe to the rolling 24h ticker (last price in ``.c``)."""
        await self._ensure_started()
        handle = await self._client.ticker(symbol=symbol.lower())
        handle.on("message", callback)
        self._handles.append(handle)

    async def subscribe_book_ticker(
        self, *, symbol: str, callback: BookTickerCallback
    ) -> None:
        """Subscribe to best bid/ask updates for ``symbol``."""
        await self._ensure_started()
        handle = await self._client.book_ticker(symbol=symbol.lower())
        handle.on("message", callback)
        self._handles.append(handle)

    async def _ensure_started(self) -> None:
        if self._connection is None:
            await self.start()

    async def stop(self) -> None:
        """Unsubscribe every stream and close the connection (best effort)."""
        handles, self._handles = self._handles, []
        for handle in handles:
            try:
                await handle.unsubscribe()
            except Exception:  # noqa: BLE001 - best-effort teardown
                logger.debug("stream unsubscribe failed", exc_info=True)

        connection, self._connection = self._connection, None
        if connection is not None:
            try:
                await self._client.close_connection(connection, close_session=True)
            except Exception:  # noqa: BLE001 - best-effort teardown
                logger.debug("websocket close failed", exc_info=True)
        logger.info("market streams stopped network=%s", self._network)


__all__ = [
    "BookTickerCallback",
    "KlineCallback",
    "MarketDataStreams",
    "TickerCallback",
]
