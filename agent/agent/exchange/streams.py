"""Binance Spot websocket market streams — thin adapter over ``binance_sdk_spot``.

Mirrors :mod:`agent.exchange.rest`: it is an adapter, not a re-implementation.
The SDK owns the connection, the subscription messages and the message parsing;
this module only:

* chooses the network-specific stream URL,
* opens one connection and remembers which kline streams the caller wants,
* forwards typed SDK messages to the caller's callback,
* restores the connection and its kline subscriptions after the server drops it
  (the SDK does not: it only reconnects on its own 23h schedule).

Message callbacks receive the SDK's own ``KlineResponse`` model — no parallel
data model is defined.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from binance_common.configuration import ConfigurationWebSocketStreams
from binance_common.websocket import RequestStreamHandle, global_stream_connections
from binance_sdk_spot import (
    SPOT_WS_STREAMS_PROD_URL,
    SPOT_WS_STREAMS_TESTNET_URL,
)
from binance_sdk_spot.spot import Spot
from binance_sdk_spot.websocket_streams.models import (
    KlineIntervalEnum,
    KlineResponse,
    TradeResponse,
)

from agent.exchange.rest import Network

logger = logging.getLogger(__name__)

# Stream base URL per network (single source of truth is the SDK constants).
_STREAM_URL: dict[str, str] = {
    "live": SPOT_WS_STREAMS_PROD_URL,
    "testnet": SPOT_WS_STREAMS_TESTNET_URL,
}

# Delay before each reconnect attempt after a drop; the last one repeats.
_RECONNECT_BACKOFF_S: tuple[float, ...] = (1, 2, 5, 10, 30, 60)

# With any kline stream subscribed, Binance pushes an update every ~1-2s; this
# long without one means the data path is dead even if the socket looks open.
_STALE_AFTER = timedelta(minutes=2)
KLINE = "klines"
TRADE = "trades"


def _kind(stream: str) -> str:
    return KLINE if "@kline_" in stream else TRADE


@dataclass
class _Freshness:
    """When data of one stream kind last arrived, and the grace it started with."""

    last: datetime | None = None
    grace_until: datetime | None = None

    def stale(self, now: datetime) -> bool:
        if self.grace_until is not None and now <= self.grace_until:
            return False
        return self.last is None or now - self.last > _STALE_AFTER


KlineCallback = Callable[[KlineResponse], None]
TradeCallback = Callable[[TradeResponse], None]
KlineKey = tuple[str, str]


class MarketStreams(Protocol):
    """What a strategy may use of market streams (a hub view or the adapter)."""

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    def on_reconnect(self, listener: Callable[[], None]) -> None: ...

    def health_warning(self) -> str | None: ...

    async def subscribe_klines_batch(
        self, subscriptions: Sequence[tuple[str, str, KlineCallback]]
    ) -> None: ...

    async def unsubscribe_klines_batch(
        self, pairs: Sequence[tuple[str, str]]
    ) -> None: ...


class MarketDataStreams:
    """One websocket connection to the market-data stream endpoint.

    Not thread-safe and not re-entrant: create one per run.

    When the server drops the connection (rate limit, maintenance, network
    loss) the SDK removes it from its pool and stops there, leaving every
    subscription dead. This class watches the connection's ``close`` event and
    reconnects with backoff, re-subscribing every tracked kline stream, then
    notifies ``on_reconnect`` listeners so they can backfill the bars they
    missed while disconnected.
    """

    def __init__(self, network: Network) -> None:
        self._network = network
        config = ConfigurationWebSocketStreams(stream_url=_STREAM_URL[network])
        self._client = Spot(config_ws_streams=config).websocket_streams
        self._started = False
        self._stopping = False
        self._kline_callbacks: dict[KlineKey, KlineCallback] = {}
        self._trade_callbacks: dict[str, TradeCallback] = {}
        self._reconnect_listeners: list[Callable[[], None]] = []
        self._recovery: asyncio.Task[None] | None = None
        #: Per stream kind: when data last arrived, and the grace a kind gets
        #: when it goes from nothing subscribed to something. A subscription
        #: is not data: adding or re-subscribing streams never counts as fresh.
        self._fresh: dict[str, _Freshness] = {
            KLINE: _Freshness(),
            TRADE: _Freshness(),
        }

    @property
    def network(self) -> Network:
        return self._network

    @property
    def connected(self) -> bool:
        return bool(self._client.connections)

    async def start(self) -> None:
        """Open the underlying websocket connection (idempotent)."""
        if self._started:
            return
        await self._open()
        self._started = True
        logger.info("market streams connected network=%s", self._network)

    def health_warning(self) -> str | None:
        """Why market data is not flowing, or ``None`` while it is (or while
        nothing is subscribed, so nothing is expected).

        Klines and trades are judged separately: trades arriving do not make
        missing candles fresh, and the other way round.
        """
        if self._stopping:
            return None
        now = datetime.now(UTC)
        link = "connected" if self._client.connections else "disconnected, reconnecting"
        for kind, subscribed in (
            (KLINE, bool(self._kline_callbacks)),
            (TRADE, bool(self._trade_callbacks)),
        ):
            fresh = self._fresh[kind]
            if not subscribed or not fresh.stale(now):
                continue
            since = (
                f"since {fresh.last:%Y-%m-%d %H:%M:%S} UTC"
                if fresh.last is not None
                else "since subscribing"
            )
            return f"no market data ({kind}) {since} (stream {link})"
        return None

    def on_reconnect(self, listener: Callable[[], None]) -> None:
        """Call ``listener`` after a dropped connection has been restored and
        its kline streams re-subscribed. Bars that closed while disconnected
        were never delivered; the listener is the place to backfill them."""
        self._reconnect_listeners.append(listener)

    async def subscribe_klines_batch(
        self, subscriptions: Sequence[tuple[str, str, KlineCallback]]
    ) -> None:
        """Subscribe to many ``<symbol>@kline_<interval>`` streams at once.

        The SDK paces one ``asyncio.sleep(0.5s)`` per call to its low-level
        ``subscribe()``, not per stream in the list passed to it — going
        through the SDK's one-stream-at-a-time ``kline()`` helper N times costs
        N such delays, which matters once N is large (e.g. a 30-symbol ×
        3-interval stage-2 admission is 90 streams, ~45s one at a time).
        Batching all of them into a single ``subscribe()`` call turns that
        into one delay, at the cost of building ``RequestStreamHandle``
        objects ourselves instead of through the SDK's per-stream helper.
        """
        if not subscriptions:
            return
        await self._ensure_connected()
        await self._subscribe(
            [
                ((symbol.upper(), interval), callback)
                for symbol, interval, callback in subscriptions
            ]
        )

    async def subscribe_trades_batch(
        self, subscriptions: Sequence[tuple[str, TradeCallback]]
    ) -> None:
        """Subscribe to ``<symbol>@trade`` streams (last trade for the guard)."""
        if not subscriptions:
            return
        await self._ensure_connected()
        entries: dict[str, tuple[Any, Callable[[Any], None]]] = {
            _trade_stream(symbol): (TradeResponse, callback)
            for symbol, callback in subscriptions
        }
        await self._subscribe_streams(entries)
        for symbol, callback in subscriptions:
            self._trade_callbacks[symbol.upper()] = callback

    async def unsubscribe_trades_batch(self, symbols: Sequence[str]) -> None:
        streams = [
            _trade_stream(symbol)
            for symbol in symbols
            if self._trade_callbacks.pop(symbol.upper(), None) is not None
        ]
        await self._drop(streams)

    async def unsubscribe_klines_batch(self, pairs: Sequence[tuple[str, str]]) -> None:
        """Drop many symbol/interval kline subscriptions in one message.

        Binance allows at most 5 incoming messages per second per connection
        and drops the connection when a client exceeds it. One ``UNSUBSCRIBE``
        per stream (what ``RequestStreamHandle.unsubscribe()`` sends) blows
        through that as soon as a rescreen removes two symbols, so all streams
        go into a single ``UNSUBSCRIBE``, mirroring ``subscribe_klines_batch``.
        The rest of the connection and its other subscriptions stay untouched.
        """
        streams = [
            _kline_stream(symbol, interval)
            for symbol, interval in pairs
            if self._kline_callbacks.pop((symbol.upper(), interval), None) is not None
        ]
        await self._drop(streams)

    async def _drop(self, streams: list[str]) -> None:
        if not streams:
            return
        try:
            await self._client.unsubscribe(streams=streams)  # type: ignore[no-untyped-call]
        except Exception:  # noqa: BLE001 - best-effort teardown
            logger.debug("unsubscribe failed streams=%s", streams, exc_info=True)
        # A failed send (dead socket) leaves the SDK's module-global map
        # pointing at the old connection, which would make a later subscribe
        # of the same stream a silent no-op.
        self._forget_streams(streams)

    async def stop(self) -> None:
        """Unsubscribe every stream and close the connection (best effort)."""
        self._stopping = True
        recovery, self._recovery = self._recovery, None
        if recovery is not None:
            recovery.cancel()
            with contextlib.suppress(BaseException):
                await recovery
        await self.unsubscribe_klines_batch(list(self._kline_callbacks))
        await self.unsubscribe_trades_batch(list(self._trade_callbacks))
        self._started = False
        # create_connection may allocate a session/pool entry before failing or
        # being cancelled. Let the SDK close the entire owned pool even when it
        # never returned a connection handle.
        try:
            await self._client.close_connection(close_session=True)
        except Exception:  # noqa: BLE001 - best-effort teardown
            logger.debug("websocket close failed", exc_info=True)
        logger.info("market streams stopped network=%s", self._network)

    async def _open(self) -> None:
        """Create a connection and watch it for drops; raise if none came up.

        The SDK's ``create_connection`` swallows connect errors (it logs them
        and returns ``None``), so success is judged by the pool, not the return.
        """
        await self._client.create_connection()  # type: ignore[no-untyped-call]
        if not self._client.connections:
            raise ConnectionError(
                f"websocket connect to {self._network!r} failed (no connections)"
            )
        self._client.on_connection("close", self._on_close)

    async def _subscribe(self, items: Sequence[tuple[KlineKey, KlineCallback]]) -> None:
        entries: dict[str, tuple[Any, Callable[[Any], None]]] = {
            _kline_stream(*key): (KlineResponse, callback) for key, callback in items
        }
        await self._subscribe_streams(entries)
        for key, callback in items:
            self._kline_callbacks[key] = callback

    async def _subscribe_streams(
        self, streams: dict[str, tuple[Any, Callable[[Any], None]]]
    ) -> None:
        """Subscribe named streams, each with its SDK model and callback."""
        now = datetime.now(UTC)
        for kind, registered in (
            (KLINE, self._kline_callbacks),
            (TRADE, self._trade_callbacks),
        ):
            if not registered and any(_kind(s) == kind for s in streams):
                # First streams of this kind: a full window before silence
                # counts. Later additions and re-subscriptions get none.
                self._fresh[kind] = _Freshness(grace_until=now + _STALE_AFTER)
        # Entries left behind by a dropped connection would make the SDK skip
        # these streams as "already subscribed".
        self._forget_streams(list(streams), dead_only=True)
        # A stream still live on the connection is already subscribed and
        # already has its callback; re-adding it would double-deliver.
        fresh = [
            s
            for s in streams
            if s not in global_stream_connections.stream_connections_map
        ]
        if fresh:
            await self._client.subscribe(
                streams=fresh,
                response_model=streams[fresh[0]][0],
            )
            missing = [
                s
                for s in fresh
                if s not in global_stream_connections.stream_connections_map
            ]
            if missing:
                # The SDK skips the subscribe (with only a warning) while its
                # own reconnect is in flight.
                raise ConnectionError(f"kline subscribe not applied streams={missing}")
            for stream in fresh:
                model, callback = streams[stream]
                handle = RequestStreamHandle(self._client, stream, model)
                handle.on("message", self._track(callback, _kind(stream)))

    def _track(
        self, callback: Callable[[Any], None], kind: str
    ) -> Callable[[Any], None]:
        def tracked(data: Any) -> None:
            self._fresh[kind].last = datetime.now(UTC)
            callback(data)

        return tracked

    async def _ensure_connected(self) -> None:
        if not self._started:
            await self.start()
            return
        if self._client.connections or self._client.reconnect_tasks:
            return
        # Dropped and not restored yet: wait for the recovery rather than
        # letting the SDK fail with "No WebSocket connections available".
        self._schedule_recovery()
        assert self._recovery is not None
        await asyncio.shield(self._recovery)

    def _on_close(self) -> None:
        if self._stopping:
            return
        logger.warning(
            "market streams connection lost network=%s; reconnecting", self._network
        )
        self._schedule_recovery()

    def _schedule_recovery(self) -> None:
        if self._recovery is None or self._recovery.done():
            self._recovery = asyncio.get_running_loop().create_task(self._recover())

    async def _recover(self) -> None:
        attempt = 0
        while not self._stopping:
            await asyncio.sleep(
                _RECONNECT_BACKOFF_S[min(attempt, len(_RECONNECT_BACKOFF_S) - 1)]
            )
            attempt += 1
            try:
                await self._reconnect()
            except Exception:  # noqa: BLE001 - keep retrying until stopped
                logger.error(
                    "market streams reconnect attempt %s failed network=%s",
                    attempt,
                    self._network,
                    exc_info=True,
                )
                continue
            logger.info(
                "market streams reconnected network=%s attempt=%s streams=%s",
                self._network,
                attempt,
                len(self._kline_callbacks),
            )
            for listener in list(self._reconnect_listeners):
                try:
                    listener()
                except Exception:  # noqa: BLE001 - one listener must not block others
                    logger.exception("market streams reconnect listener failed")
            return

    async def _reconnect(self) -> None:
        if self._client.reconnect_tasks:
            # The SDK is already replacing the connection; let it finish.
            raise ConnectionError("SDK reconnect in flight")
        if not self._client.connections:
            await self._open()
        await self._subscribe(list(self._kline_callbacks.items()))
        if self._trade_callbacks:
            await self._subscribe_streams(
                {
                    _trade_stream(symbol): (TradeResponse, callback)
                    for symbol, callback in self._trade_callbacks.items()
                }
            )

    def _forget_streams(
        self, streams: Sequence[str], *, dead_only: bool = False
    ) -> None:
        """Drop streams from the SDK's module-global stream→connection map.

        The SDK leaves a dropped connection's streams mapped there; its
        ``subscribe`` then treats them as already live and silently skips them.
        """
        stream_map = global_stream_connections.stream_connections_map
        live = self._client.connections
        for stream in streams:
            connection = stream_map.get(stream)
            if connection is not None and (not dead_only or connection not in live):
                stream_map.pop(stream, None)


def _kline_stream(symbol: str, interval: str) -> str:
    return f"{symbol.lower()}@kline_{KlineIntervalEnum(interval).value}"


def _trade_stream(symbol: str) -> str:
    return f"{symbol.lower()}@trade"


__all__ = [
    "KlineCallback",
    "MarketDataStreams",
    "MarketStreams",
    "TradeCallback",
]
