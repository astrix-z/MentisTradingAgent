"""Real-time market data via Binance websocket streams.

A thin asyncio facade over ``binance_sdk_spot.websocket_streams`` that keeps
market streams for a set of tracked symbols and feeds the best bid/ask into a
shared :class:`~agent.marketdata.MarketData` cache.

The SDK owns the underlying connection(s) and re-subscribes the tracked streams
after an automatic reconnect, so this wrapper does not need to manage
reconnection itself.

Network is chosen per request (``testnet`` / ``live``); market streams are
public and do not need credentials, but each network has its own base URL.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from binance_common.configuration import ConfigurationWebSocketStreams
from binance_sdk_spot.websocket_streams import SpotWebSocketStreams

from agent.exchange.binance import ExchangeError
from agent.marketdata import MarketData

logger = logging.getLogger(__name__)

# WebSocket market-stream base URLs (the SDK appends ``/stream``).
_WSS_LIVE = "wss://stream.binance.com:9443"
_WSS_TESTNET = "wss://stream.testnet.binance.vision"

# How long to wait for the very first websocket connection before giving up.
_CONNECT_TIMEOUT = 15.0


def _stream_url(network: str) -> str:
    if network == "live":
        return _WSS_LIVE
    if network == "testnet":
        return _WSS_TESTNET
    raise ExchangeError(f"unknown network: {network!r}")


class MarketDataFeed:
    """Subscribes to book-ticker streams and mirrors them into ``MarketData``."""

    def __init__(self, market: MarketData) -> None:
        self._market = market
        self._client: SpotWebSocketStreams | None = None
        self._handles: list[Any] = []
        self._symbols: list[str] = []
        self._network: str | None = None

    @property
    def running(self) -> bool:
        return self._client is not None

    @property
    def symbols(self) -> list[str]:
        return list(self._symbols)

    async def start(self, network: str, symbols: list[str]) -> None:
        """Connect and subscribe to book-ticker streams for ``symbols``."""
        if self._client is not None:
            logger.warning("market feed already started; ignoring start()")
            return
        normalized = [str(symbol).upper() for symbol in symbols]
        if not normalized:
            return

        client = SpotWebSocketStreams(
            ConfigurationWebSocketStreams(stream_url=_stream_url(network))
        )
        try:
            # The SDK method is untyped; the base class owns the connections.
            conn = client.create_connection()  # type: ignore[no-untyped-call]
            await asyncio.wait_for(conn, _CONNECT_TIMEOUT)
        except TimeoutError as exc:
            raise ExchangeError(f"websocket connect to {network!r} timed out") from exc

        if not client.connections:
            raise ExchangeError(
                f"websocket connect to {network!r} failed (no connections)"
            )

        for symbol in normalized:
            # Subscribes to ``<symbol>@bookTicker``; the returned handle keeps
            # the subscription alive and routes messages to our callback.
            handle = await client.book_ticker(symbol)
            handle.on("message", self._on_book_ticker)
            self._handles.append(handle)

        self._client = client
        self._network = network
        self._symbols = normalized
        logger.info("market feed started network=%s symbols=%s", network, normalized)

    def _on_book_ticker(self, data: Any) -> None:
        """Update the shared cache from a ``BookTickerResponse`` message."""
        symbol = _first_value(getattr(data, "s", None))
        bid_raw = getattr(data, "b", None)
        ask_raw = getattr(data, "a", None)
        if symbol is None or bid_raw is None or ask_raw is None:
            return
        try:
            bid = float(str(bid_raw))
            ask = float(str(ask_raw))
        except (TypeError, ValueError):
            logger.warning("ignoring malformed book ticker for %s", symbol)
            return
        self._market.update_book(symbol, bid, ask)

    async def stop(self) -> None:
        """Unsubscribe all streams and close the websocket connection."""
        client = self._client
        self._client = None
        self._symbols = []
        self._network = None
        if client is None:
            return
        handles, self._handles = self._handles, []
        for handle in handles:
            try:
                await handle.unsubscribe()
            except Exception:  # noqa: BLE001 - best-effort teardown
                logger.debug("error unsubscribing stream", exc_info=True)
        try:
            await client.close_connection(close_session=True)
        except Exception:  # noqa: BLE001 - best-effort teardown
            logger.debug("error closing websocket", exc_info=True)
        logger.info("market feed stopped")


def _first_value(value: Any) -> str | None:
    """Return ``value`` as a string, or ``None`` when it is missing/empty."""
    if value is None:
        return None
    text = str(value)
    return text if text else None
