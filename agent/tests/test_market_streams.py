"""MarketDataStreams against a fake SDK client that keeps the real SDK's quirks:
a module-global stream→connection map that a server-side drop leaves stale, a
``subscribe`` that silently skips streams still in that map, and a ``close``
connection event with no reconnect after it."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from binance_common.websocket import global_stream_connections

import agent.exchange.streams as streams_module
from agent.exchange.streams import MarketDataStreams

STREAM_MAP = global_stream_connections.stream_connections_map


class FakeSdk:
    def __init__(self, *, failed_connects: int = 0) -> None:
        self.connections: list[Any] = []
        self.reconnect_tasks: list[str] = []
        self.failed_connects = failed_connects
        self.close_listeners: list[Callable[[], None]] = []
        self.subscribe_messages: list[list[str]] = []
        self.unsubscribe_messages: list[list[str]] = []
        self.callbacks: dict[str, list[Callable[[Any], None]]] = {}

    async def create_connection(self) -> FakeSdk | None:
        if self.failed_connects:
            self.failed_connects -= 1
            return None  # the SDK logs connect errors and returns None
        self.connections.append(object())
        return self

    def on_connection(self, event: str, callback: Callable[[], None]) -> None:
        assert event == "close"
        self.close_listeners.append(callback)

    async def subscribe(self, streams: list[str], response_model: Any = None) -> None:
        if not self.connections:
            raise ValueError("No WebSocket connections available.")
        streams = [s for s in streams if s not in STREAM_MAP]
        self.subscribe_messages.append(streams)
        for stream in streams:
            STREAM_MAP[stream] = self.connections[0]
            self.callbacks[stream] = []

    def on(self, event: str, callback: Callable[[Any], None], stream: str) -> None:
        self.callbacks[stream].append(callback)

    async def unsubscribe(self, streams: list[str]) -> None:
        self.unsubscribe_messages.append(list(streams))
        for stream in streams:
            STREAM_MAP.pop(stream, None)
            self.callbacks.pop(stream, None)

    async def close_connection(self, close_session: bool = True) -> None:
        self.connections.clear()

    def drop(self) -> None:
        """Server-side drop: pool emptied, stream map left stale, `close` fired."""
        self.connections.clear()
        for listener in list(self.close_listeners):
            listener()


@pytest.fixture(autouse=True)
def isolate_sdk_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(streams_module, "_RECONNECT_BACKOFF_S", (0,))
    saved = dict(STREAM_MAP)
    STREAM_MAP.clear()
    yield
    STREAM_MAP.clear()
    STREAM_MAP.update(saved)


def make_streams(sdk: FakeSdk) -> MarketDataStreams:
    streams = MarketDataStreams("live")
    streams._client = sdk  # type: ignore[assignment]
    return streams


def noop(_: Any) -> None:
    pass


async def wait_recovered(streams: MarketDataStreams) -> None:
    assert streams._recovery is not None
    await streams._recovery


def test_unsubscribe_klines_in_one_message() -> None:
    async def scenario() -> None:
        sdk = FakeSdk()
        streams = make_streams(sdk)
        await streams.subscribe_klines_batch(
            [
                ("UNIUSDT", "4h", noop),
                ("UNIUSDT", "15m", noop),
                ("ZILUSDT", "1h", noop),
                ("BTCUSDT", "1h", noop),
            ]
        )
        await streams.unsubscribe_klines_batch(
            [
                ("UNIUSDT", "4h"),
                ("uniusdt", "15m"),
                ("ZILUSDT", "1h"),
                ("ETHUSDT", "1h"),
            ]
        )
        assert sdk.unsubscribe_messages == [
            ["uniusdt@kline_4h", "uniusdt@kline_15m", "zilusdt@kline_1h"]
        ]
        assert list(streams._kline_callbacks) == [("BTCUSDT", "1h")]
        # Nothing tracked left to drop -> no message at all.
        await streams.unsubscribe_klines_batch([("ETHUSDT", "1h")])
        assert len(sdk.unsubscribe_messages) == 1

    asyncio.run(scenario())


def test_drop_reconnects_and_resubscribes_every_stream_once() -> None:
    async def scenario() -> None:
        sdk = FakeSdk()
        streams = make_streams(sdk)
        reconnects: list[None] = []
        streams.on_reconnect(lambda: reconnects.append(None))
        await streams.subscribe_klines_batch(
            [("BTCUSDT", "4h", noop), ("BTCUSDT", "1h", noop)]
        )

        sdk.drop()
        await wait_recovered(streams)

        assert streams.connected
        assert sdk.subscribe_messages[-1] == ["btcusdt@kline_4h", "btcusdt@kline_1h"]
        # Fresh callback list per re-subscribed stream: no double delivery.
        assert all(len(sdk.callbacks[s]) == 1 for s in sdk.subscribe_messages[-1])
        assert reconnects == [None]

    asyncio.run(scenario())


def test_reconnect_retries_until_the_connection_comes_back() -> None:
    async def scenario() -> None:
        sdk = FakeSdk()
        streams = make_streams(sdk)
        await streams.subscribe_klines_batch([("BTCUSDT", "4h", noop)])

        sdk.failed_connects = 3
        sdk.drop()
        await wait_recovered(streams)

        assert streams.connected
        assert sdk.failed_connects == 0
        assert sdk.subscribe_messages[-1] == ["btcusdt@kline_4h"]

    asyncio.run(scenario())


def test_subscribe_while_dropped_waits_for_recovery_instead_of_failing() -> None:
    """Live run 2026-09-21: after the drop every rescreen failed with
    'No WebSocket connections available.' for 45 hours."""

    async def scenario() -> None:
        sdk = FakeSdk()
        streams = make_streams(sdk)
        await streams.subscribe_klines_batch([("BTCUSDT", "4h", noop)])
        # Drop without the close event reaching us.
        sdk.connections.clear()

        await streams.subscribe_klines_batch([("ETHUSDT", "4h", noop)])

        assert streams.connected
        assert set(STREAM_MAP) == {"btcusdt@kline_4h", "ethusdt@kline_4h"}

    asyncio.run(scenario())


def test_stop_during_outage_cancels_recovery_and_clears_stale_streams() -> None:
    async def scenario() -> None:
        sdk = FakeSdk()
        streams = make_streams(sdk)
        await streams.subscribe_klines_batch([("BTCUSDT", "4h", noop)])

        sdk.failed_connects = 10**6
        sdk.drop()
        await asyncio.sleep(0)
        await streams.stop()

        assert streams._recovery is None
        assert not streams._kline_callbacks
        # A later run in the same process must be able to subscribe again.
        assert "btcusdt@kline_4h" not in STREAM_MAP

    asyncio.run(scenario())


def test_subscribe_ignores_streams_mapped_to_a_dead_connection() -> None:
    """The SDK's stream map is module-global: entries a dropped connection left
    behind must not make a new subscribe a silent no-op."""

    async def scenario() -> None:
        STREAM_MAP["btcusdt@kline_4h"] = object()  # type: ignore[assignment]
        sdk = FakeSdk()
        streams = make_streams(sdk)

        await streams.subscribe_klines_batch([("BTCUSDT", "4h", noop)])

        assert sdk.subscribe_messages == [["btcusdt@kline_4h"]]
        assert len(sdk.callbacks["btcusdt@kline_4h"]) == 1

    asyncio.run(scenario())


def test_health_warning_reports_silent_or_dropped_stream() -> None:
    async def scenario() -> None:
        sdk = FakeSdk(failed_connects=0)
        streams = make_streams(sdk)
        assert streams.health_warning() is None  # nothing subscribed yet
        await streams.subscribe_klines_batch([("BTCUSDT", "4h", noop)])
        assert streams.health_warning() is None  # fresh subscription

        streams._last_message_at = datetime.now(UTC) - timedelta(minutes=3)
        warning = streams.health_warning()
        assert warning is not None and "(stream connected)" in warning

        sdk.failed_connects = 10**6
        sdk.drop()
        warning = streams.health_warning()
        assert warning is not None and "disconnected, reconnecting" in warning

        # Any delivered message clears it.
        sdk.failed_connects = 0
        await wait_recovered(streams)
        sdk.callbacks["btcusdt@kline_4h"][0](None)
        assert streams.health_warning() is None
        await streams.stop()

    asyncio.run(scenario())
