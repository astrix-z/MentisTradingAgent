"""StreamHub: the one owner of market-data websocket subscriptions (plan §9.3).

The SDK keeps a **module-global** map from stream name to connection. Two
``MarketDataStreams`` in one process therefore share it: one instance's
``_forget_streams`` or unsubscribe can silently kill the other's subscription
of the same stream, and the same stream name on live and on testnet collides.
So there is exactly one connection manager per process, and everyone who needs
market data — the strategy, the position monitor — is a subscriber of it:

* **fan-out and reference counting**: a stream is subscribed on the exchange
  while at least one subscriber wants it; each message goes to every
  subscriber of that stream; a subscriber leaving (Stop of the strategy) only
  removes its own callbacks;
* **one network**: attaching to another network while any subscriber is
  attached is refused before anything changes. When the last subscriber is
  gone, a switch tears the old connection down completely (its streams leave
  the SDK map, its callbacks are dropped) before the new one opens.

Subscribers get a :class:`StreamView` with the ``MarketDataStreams`` API, so a
strategy does not know it shares the connection.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from typing import Any

from agent.exchange.rest import Network
from agent.exchange.streams import (
    KlineCallback,
    KlineKey,
    MarketDataStreams,
    TradeCallback,
)

logger = logging.getLogger(__name__)


class NetworkBusy(RuntimeError):
    """Another network's streams are still in use in this process."""


class StreamHub:
    def __init__(
        self, factory: Callable[[Network], MarketDataStreams] | None = None
    ) -> None:
        self._factory = factory
        self._network: Network | None = None
        self._streams: MarketDataStreams | None = None
        self._views: set[StreamView] = set()
        self._klines: dict[KlineKey, dict[StreamView, KlineCallback]] = {}
        self._trades: dict[str, dict[StreamView, TradeCallback]] = {}
        self._lock = asyncio.Lock()

    @property
    def network(self) -> Network | None:
        return self._network

    def _make(self, network: Network) -> MarketDataStreams:
        if self._factory is not None:
            return self._factory(network)
        return MarketDataStreams(network)

    async def attach(self, network: Network, owner: str) -> StreamView:
        """A subscriber view on ``network``; refuses a second network."""
        async with self._lock:
            if self._network is not None and self._network != network:
                if self._views:
                    owners = sorted(v.owner for v in self._views)
                    raise NetworkBusy(
                        f"market streams are on {self._network} for {owners}; "
                        f"{owner} cannot use {network}"
                    )
                await self._teardown()
            if self._streams is None:
                self._streams = self._make(network)
                self._network = network
                self._streams.on_reconnect(self._reconnected)
            view = StreamView(self, owner)
            self._views.add(view)
            return view

    async def _teardown(self) -> None:
        streams, self._streams = self._streams, None
        self._klines.clear()
        self._trades.clear()
        if streams is not None:
            await streams.stop()
            logger.info("market streams torn down network=%s", self._network)
        self._network = None

    def _reconnected(self) -> None:
        for view in list(self._views):
            view.notify_reconnect()

    # --- per-view operations (called by StreamView) -------------------------

    async def _start(self) -> None:
        assert self._streams is not None
        await self._streams.start()

    async def _subscribe_klines(
        self, view: StreamView, items: Sequence[tuple[str, str, KlineCallback]]
    ) -> None:
        assert self._streams is not None
        new: list[tuple[str, str, KlineCallback]] = []
        for symbol, interval, callback in items:
            key = (symbol.upper(), interval)
            owners = self._klines.setdefault(key, {})
            if not owners:
                new.append((key[0], interval, self._kline_fanout(key)))
            owners[view] = callback
        await self._streams.subscribe_klines_batch(new)

    async def _unsubscribe_klines(
        self, view: StreamView, pairs: Sequence[tuple[str, str]]
    ) -> None:
        gone: list[tuple[str, str]] = []
        for symbol, interval in pairs:
            key = (symbol.upper(), interval)
            owners = self._klines.get(key)
            if owners is None or owners.pop(view, None) is None:
                continue
            if not owners:
                del self._klines[key]
                gone.append(key)
        if gone and self._streams is not None:
            await self._streams.unsubscribe_klines_batch(gone)

    async def _subscribe_trades(
        self, view: StreamView, items: Sequence[tuple[str, TradeCallback]]
    ) -> None:
        assert self._streams is not None
        new: list[tuple[str, TradeCallback]] = []
        for symbol, callback in items:
            key = symbol.upper()
            owners = self._trades.setdefault(key, {})
            if not owners:
                new.append((key, self._trade_fanout(key)))
            owners[view] = callback
        await self._streams.subscribe_trades_batch(new)

    async def _unsubscribe_trades(
        self, view: StreamView, symbols: Sequence[str]
    ) -> None:
        gone: list[str] = []
        for symbol in symbols:
            key = symbol.upper()
            owners = self._trades.get(key)
            if owners is None or owners.pop(view, None) is None:
                continue
            if not owners:
                del self._trades[key]
                gone.append(key)
        if gone and self._streams is not None:
            await self._streams.unsubscribe_trades_batch(gone)

    async def _detach(self, view: StreamView) -> None:
        async with self._lock:
            await self._unsubscribe_klines(
                view, [k for k, owners in self._klines.items() if view in owners]
            )
            await self._unsubscribe_trades(
                view, [k for k, owners in self._trades.items() if view in owners]
            )
            self._views.discard(view)
            if not self._views:
                await self._teardown()

    def _kline_fanout(self, key: KlineKey) -> KlineCallback:
        def deliver(message: Any) -> None:
            for callback in list(self._klines.get(key, {}).values()):
                try:
                    callback(message)
                except Exception:  # noqa: BLE001 - one subscriber must not starve others
                    logger.exception("kline subscriber failed for %s", key)

        return deliver

    def _trade_fanout(self, symbol: str) -> TradeCallback:
        def deliver(message: Any) -> None:
            for callback in list(self._trades.get(symbol, {}).values()):
                try:
                    callback(message)
                except Exception:  # noqa: BLE001
                    logger.exception("trade subscriber failed for %s", symbol)

        return deliver

    def health_warning(self) -> str | None:
        return self._streams.health_warning() if self._streams is not None else None

    @property
    def connected(self) -> bool:
        return self._streams is not None and self._streams.connected


class StreamView:
    """One subscriber's handle, with the ``MarketDataStreams`` API."""

    def __init__(self, hub: StreamHub, owner: str) -> None:
        self._hub = hub
        self.owner = owner
        self._reconnect_listeners: list[Callable[[], None]] = []
        self._detached = False

    @property
    def network(self) -> Network:
        assert self._hub.network is not None
        return self._hub.network

    @property
    def connected(self) -> bool:
        return self._hub.connected

    async def start(self) -> None:
        await self._hub._start()

    async def stop(self) -> None:
        """Leave the hub: only this subscriber's streams are released."""
        if self._detached:
            return
        self._detached = True
        await self._hub._detach(self)

    def on_reconnect(self, listener: Callable[[], None]) -> None:
        self._reconnect_listeners.append(listener)

    def notify_reconnect(self) -> None:
        for listener in list(self._reconnect_listeners):
            try:
                listener()
            except Exception:  # noqa: BLE001
                logger.exception("%s reconnect listener failed", self.owner)

    def health_warning(self) -> str | None:
        return self._hub.health_warning()

    async def subscribe_klines_batch(
        self, subscriptions: Sequence[tuple[str, str, KlineCallback]]
    ) -> None:
        await self._hub._subscribe_klines(self, subscriptions)

    async def unsubscribe_klines_batch(self, pairs: Sequence[tuple[str, str]]) -> None:
        await self._hub._unsubscribe_klines(self, pairs)

    async def subscribe_trades_batch(
        self, subscriptions: Sequence[tuple[str, TradeCallback]]
    ) -> None:
        await self._hub._subscribe_trades(self, subscriptions)

    async def unsubscribe_trades_batch(self, symbols: Sequence[str]) -> None:
        await self._hub._unsubscribe_trades(self, symbols)


__all__ = ["NetworkBusy", "StreamHub", "StreamView"]
