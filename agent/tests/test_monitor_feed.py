"""EXEC-10: StreamHub, MonitorFeed and the watermark.

The hub runs over the real ``MarketDataStreams`` adapter on the fake SDK client
of ``test_market_streams`` (module-global stream map and all).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from binance_sdk_spot.websocket_streams.models import (
    KlineResponse,
    KlineResponseK,
    TradeResponse,
)
from pg import scratch_database
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_market_streams import STREAM_MAP, FakeSdk, isolate_sdk_state  # noqa: F401

from agent.core.migrate import migrate
from agent.exchange.hub import NetworkBusy, StreamHub
from agent.exchange.streams import MarketDataStreams
from agent.execution.models import MonitorWatermark, Position
from agent.execution.monitor import Followed, MonitorFeed, commit_watermark

M15 = 900_000
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
NOW_MS = int(NOW.timestamp() * 1000)


class Sdks:
    """One fake SDK client per network, remembered for inspection."""

    def __init__(self) -> None:
        self.by_network: dict[str, FakeSdk] = {}

    def factory(self, network: Any) -> MarketDataStreams:
        streams = MarketDataStreams(network)
        sdk = FakeSdk()
        self.by_network[network] = sdk
        streams._client = sdk  # type: ignore[assignment]
        return streams


def kline(open_time: int, *, closed: bool = True, low: str = "99") -> KlineResponse:
    return KlineResponse(
        k=KlineResponseK(
            t=open_time,
            T=open_time + M15 - 1,
            o="100",
            h="101",
            l=low,
            c="100",
            v="1",
            x=closed,
        )
    )


def deliver(sdk: FakeSdk, stream: str, message: Any) -> None:
    for callback in sdk.callbacks[stream]:
        callback(message)


# --- the hub ---------------------------------------------------------------------------


def test_strategy_stop_start_and_reconnect_never_cut_the_monitor() -> None:
    async def scenario() -> None:
        sdks = Sdks()
        hub = StreamHub(factory=sdks.factory)
        strategy_bars: list[Any] = []
        monitor_bars: list[Any] = []
        strategy = await hub.attach("live", "strategy")
        await strategy.start()
        await strategy.subscribe_klines_batch(
            [("BTCUSDT", "15m", strategy_bars.append)]
        )
        monitor = await hub.attach("live", "monitor")
        await monitor.subscribe_klines_batch([("BTCUSDT", "15m", monitor_bars.append)])
        sdk = sdks.by_network["live"]
        assert sdk.subscribe_messages == [
            ["btcusdt@kline_15m"]
        ]  # one stream, two owners
        deliver(sdk, "btcusdt@kline_15m", "bar-1")
        assert strategy_bars == monitor_bars == ["bar-1"]

        await strategy.stop()  # Stop of the strategy
        assert sdk.unsubscribe_messages == []
        deliver(sdk, "btcusdt@kline_15m", "bar-2")
        assert monitor_bars == ["bar-1", "bar-2"]

        again = await hub.attach("live", "strategy")  # Start again
        await again.subscribe_klines_batch([("BTCUSDT", "15m", strategy_bars.append)])
        assert sdk.subscribe_messages == [["btcusdt@kline_15m"]]

        reconnected: list[str] = []
        monitor.on_reconnect(lambda: reconnected.append("monitor"))
        again.on_reconnect(lambda: reconnected.append("strategy"))
        sdk.drop()
        streams = hub._streams
        assert streams is not None and streams._recovery is not None
        await streams._recovery
        assert sorted(reconnected) == ["monitor", "strategy"]
        deliver(sdk, "btcusdt@kline_15m", "bar-3")
        assert monitor_bars[-1] == "bar-3" and strategy_bars[-1] == "bar-3"

    asyncio.run(scenario())


def test_a_second_network_is_refused_until_the_first_is_released() -> None:
    async def scenario() -> None:
        sdks = Sdks()
        hub = StreamHub(factory=sdks.factory)
        received: list[Any] = []
        monitor = await hub.attach("live", "monitor")
        await monitor.subscribe_klines_batch([("BTCUSDT", "15m", received.append)])
        with pytest.raises(NetworkBusy):
            await hub.attach("testnet", "strategy")
        live = sdks.by_network["live"]
        deliver(live, "btcusdt@kline_15m", "still-live")
        assert received == ["still-live"] and "testnet" not in sdks.by_network

        await monitor.stop()
        testnet = await hub.attach("testnet", "strategy")
        assert live.connections == [] and "btcusdt@kline_15m" not in STREAM_MAP
        other: list[Any] = []
        await testnet.subscribe_klines_batch([("BTCUSDT", "15m", other.append)])
        deliver(sdks.by_network["testnet"], "btcusdt@kline_15m", "testnet-bar")
        assert other == ["testnet-bar"] and received == ["still-live"]

    asyncio.run(scenario())


# --- the feed ------------------------------------------------------------------------------


class Loader:
    """REST klines as arrays; ``bars`` maps open time → low."""

    def __init__(self, bars: dict[int, str]) -> None:
        self.bars = bars
        self.calls: list[int | None] = []
        self.gate: asyncio.Event | None = None

    async def __call__(
        self, symbol: str, interval: str, start: int | None, limit: int
    ) -> list[list[Any]]:
        self.calls.append(start)
        if self.gate is not None:
            await self.gate.wait()
        return [
            [t, "100", "101", low, "100", "1", t + M15 - 1]
            for t, low in sorted(self.bars.items())
            if start is None or t >= start
        ]


def feed_for(hub: StreamHub, loader: Loader) -> MonitorFeed:
    return MonitorFeed(hub, loader, clock=lambda: NOW)


def times(*offsets: int) -> list[int]:
    base = NOW_MS // M15 * M15
    return [base - M15 * k for k in offsets]


def test_cold_start_backfills_context_and_hides_the_forming_bar() -> None:
    async def scenario() -> None:
        sdks = Sdks()
        hub = StreamHub(factory=sdks.factory)
        t3, t2, t1, forming = times(3, 2, 1, 0)
        loader = Loader({t3: "97", t2: "98", t1: "99", forming: "50"})
        feed = feed_for(hub, loader)
        position = uuid.uuid4()
        await feed.follow("live", [Followed(position, "BTCUSDT", "15m", 2, None)])
        await asyncio.gather(*feed._backfills.values())
        bars = feed.bars_after(position, None)
        assert [b.open_time for b in bars] == [t3, t2, t1]  # forming bar excluded
        assert [b.low for b in feed.window("BTCUSDT", "15m", t1, 2) or []] == [
            Decimal(98),
            Decimal(99),
        ]
        sdk = sdks.by_network["live"]
        deliver(sdk, "btcusdt@kline_15m", kline(t1, low="42"))  # duplicate replaces
        deliver(sdk, "btcusdt@kline_15m", kline(forming, closed=False))
        assert [b.open_time for b in feed.bars_after(position, t2)] == [t1]
        assert feed.window("BTCUSDT", "15m", t1, 4) is None  # gap: no window

    asyncio.run(scenario())


def test_bars_are_withheld_until_the_backfill_closes_the_gap() -> None:
    async def scenario() -> None:
        sdks = Sdks()
        hub = StreamHub(factory=sdks.factory)
        t2, t1 = times(2, 1)
        loader = Loader({t2: "98"})
        loader.gate = asyncio.Event()
        feed = feed_for(hub, loader)
        position = uuid.uuid4()
        await feed.follow("live", [Followed(position, "BTCUSDT", "15m", 2, t2)])
        deliver(sdks.by_network["live"], "btcusdt@kline_15m", kline(t1))
        assert feed.bars_after(position, t2) == []  # buffered, not handed out
        loader.gate.set()
        await asyncio.gather(*feed._backfills.values())
        assert [b.open_time for b in feed.bars_after(position, t2)] == [t1]
        # The watermark position asked for bars from trail_n before it.
        assert loader.calls == [t2 - 2 * M15]

    asyncio.run(scenario())


def test_reconnect_reloads_every_missed_bar_in_order() -> None:
    async def scenario() -> None:
        sdks = Sdks()
        hub = StreamHub(factory=sdks.factory)
        t4, t3, t2, t1 = times(4, 3, 2, 1)
        loader = Loader({t4: "97"})
        feed = feed_for(hub, loader)
        position = uuid.uuid4()
        await feed.follow("live", [Followed(position, "BTCUSDT", "15m", 2, t4)])
        await asyncio.gather(*feed._backfills.values())
        sdk = sdks.by_network["live"]
        sdk.drop()
        loader.bars.update({t3: "96", t2: "95", t1: "94"})  # closed while down
        loader.gate = asyncio.Event()
        assert hub._streams is not None and hub._streams._recovery is not None
        await hub._streams._recovery
        assert not feed.ready("BTCUSDT", "15m")
        assert feed.bars_after(position, t4) == []
        loader.gate.set()
        await asyncio.gather(*feed._backfills.values())
        assert [b.open_time for b in feed.bars_after(position, t4)] == [t3, t2, t1]

    asyncio.run(scenario())


class FlakyLoader(Loader):
    """Fails the first ``failures`` calls, then answers like :class:`Loader`."""

    def __init__(self, bars: dict[int, str], failures: int) -> None:
        super().__init__(bars)
        self.failures = failures

    async def __call__(
        self, symbol: str, interval: str, start: int | None, limit: int
    ) -> list[list[Any]]:
        if self.failures > 0:
            self.failures -= 1
            self.calls.append(start)
            raise TimeoutError("klines read timed out")
        return await super().__call__(symbol, interval, start, limit)


def test_failed_backfill_is_retried_with_backoff_until_it_recovers() -> None:
    # Review 2026-09-30 (EXEC-10): the loader failed once, then recovered, but
    # four more follow() rounds never called it again: not ready for good.
    async def scenario() -> None:
        sdks = Sdks()
        hub = StreamHub(factory=sdks.factory)
        t2, t1 = times(2, 1)
        loader = FlakyLoader({t2: "98", t1: "99"}, failures=2)
        clock = [NOW]
        feed = MonitorFeed(hub, loader, clock=lambda: clock[0])
        position = uuid.uuid4()
        followed = [Followed(position, "BTCUSDT", "15m", 2, t2)]

        async def round_(advance: float = 0) -> None:
            clock[0] += timedelta(seconds=advance)
            await feed.follow("live", followed)
            await asyncio.gather(*feed._backfills.values())

        await round_()
        assert len(loader.calls) == 1 and not feed.ready("BTCUSDT", "15m")
        assert feed.degraded()["BTCUSDT"] == "bars not backfilled"
        await round_(1)  # inside the first backoff (5 s): no new call
        assert len(loader.calls) == 1
        await round_(5)  # second try, fails again; backoff doubles to 10 s
        assert len(loader.calls) == 2 and not feed.ready("BTCUSDT", "15m")
        await round_(5)
        assert len(loader.calls) == 2
        await round_(6)  # recovers
        assert len(loader.calls) == 3 and feed.ready("BTCUSDT", "15m")
        assert [b.open_time for b in feed.bars_after(position, t2)] == [t1]
        await round_(60)  # ready: not reloaded every round
        assert len(loader.calls) == 3

    asyncio.run(scenario())


def test_trades_give_the_guard_its_price_and_silence_is_degraded() -> None:
    async def scenario() -> None:
        sdks = Sdks()
        hub = StreamHub(factory=sdks.factory)
        feed = feed_for(hub, Loader({}))
        position = uuid.uuid4()
        await feed.follow("live", [Followed(position, "ETHUSDT", "15m", 2, None)])
        await asyncio.gather(*feed._backfills.values())
        assert feed.degraded() == {"ETHUSDT": "no recent trades"}
        trade = TradeResponse(s="ETHUSDT", p="2500.5", T=NOW_MS - 1000)
        deliver(sdks.by_network["live"], "ethusdt@trade", trade)
        last = feed.last_trade("ETHUSDT")
        assert last is not None and last.price == Decimal("2500.5")
        assert feed.degraded() == {}
        await feed.follow("live", [])  # position closed
        assert hub._streams is None  # last subscriber gone

    asyncio.run(scenario())


def test_watermark_moves_only_forward_and_only_with_the_decision() -> None:
    async def scenario() -> None:
        async with scratch_database() as url:
            engine = create_async_engine(url)
            await migrate(engine)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            position_id = uuid.uuid4()
            async with sessions() as session:
                session.add(
                    Position(
                        id=position_id,
                        account="1",
                        network="live",
                        symbol="BTCUSDT",
                        strategy="s",
                        signal_id=uuid.uuid4(),
                        status="protected",
                        exit_policy={},
                        quantity=Decimal(1),
                    )
                )
                await session.commit()
            t2, t1 = times(2, 1)

            async def stored() -> int | None:
                async with sessions() as session:
                    row = (await session.scalars(select(MonitorWatermark))).first()
                    return int(row.bar_open_time.timestamp() * 1000) if row else None

            # Crash before commit: the bar will be processed again.
            with pytest.raises(RuntimeError):
                async with sessions() as session, session.begin():
                    await commit_watermark(session, position_id, "15m", t2)
                    raise RuntimeError("crash with the decision")
            assert await stored() is None
            async with sessions() as session, session.begin():
                await commit_watermark(session, position_id, "15m", t1)
            async with sessions() as session, session.begin():
                await commit_watermark(session, position_id, "15m", t2)  # older
            assert await stored() == t1
            await engine.dispose()

    asyncio.run(scenario())


def test_positions_outside_the_strategy_are_followed_too() -> None:
    async def scenario() -> None:
        sdks = Sdks()
        hub = StreamHub(factory=sdks.factory)
        strategy = await hub.attach("live", "strategy")
        await strategy.subscribe_klines_batch([("BTCUSDT", "15m", lambda m: None)])
        feed = feed_for(hub, Loader({}))
        await feed.follow("live", [Followed(uuid.uuid4(), "SOLUSDT", "15m", 2, None)])
        assert ["solusdt@kline_15m"] in sdks.by_network["live"].subscribe_messages
        await strategy.stop()
        assert "solusdt@kline_15m" in STREAM_MAP

    asyncio.run(scenario())
