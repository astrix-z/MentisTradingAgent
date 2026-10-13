"""EXEC-09b: trailing to the ceiling, decisions committed with the watermark."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from execution_world import Stack, World, run
from sqlalchemy import select

import agent.execution.trailing as trailing_module
from agent.exchange.hub import StreamHub
from agent.execution.models import MonitorWatermark, Position
from agent.execution.monitor import Bar, Followed, MonitorFeed
from agent.execution.outcome import ExitMechanism, ExitReason
from agent.execution.quotes import LastTrade
from agent.execution.trailing import Trailer

M15 = 900_000


def feed_with(
    state: World, position_id: uuid.UUID, opened_ms: int, lows: dict[int, str]
) -> MonitorFeed:
    """A feed already holding closed bars at ``k`` intervals from the fill."""

    async def no_rest(*_: Any) -> list[list[Any]]:
        return []

    feed = MonitorFeed(StreamHub(), no_rest, clock=state.clock.now)
    feed._followed[position_id] = Followed(position_id, "BTCUSDT", "15m", 2, None)
    feed._ready.add(("BTCUSDT", "15m"))
    add_bars(feed, opened_ms, lows)
    return feed


def add_bars(feed: MonitorFeed, opened_ms: int, lows: dict[int, str]) -> None:
    base = opened_ms // M15 * M15
    feed._add(
        "BTCUSDT",
        "15m",
        [
            Bar(
                open_time=base + k * M15,
                close_time=base + (k + 1) * M15 - 1,
                low=Decimal(low),
                high=Decimal(low) + 1,
                close=Decimal(low) + 1,
            )
            for k, low in lows.items()
        ],
    )


async def setup(state: World) -> tuple[Stack, uuid.UUID, int]:
    stack = await state.stack()
    position_id = await state.protected(stack)
    position = await state.position(stack, position_id)
    assert position.opened_at is not None and position.exchange_stop == Decimal(98)
    return stack, position_id, int(position.opened_at.timestamp() * 1000)


def trade(feed: MonitorFeed, state: World, price: str) -> None:
    feed._trades["BTCUSDT"] = LastTrade(Decimal(price), state.clock.now())


LOWS = {-1: "99", 0: "100", 1: "101", 2: "103", 3: "104.5", 4: "106", 5: "107"}


async def watermark(stack: Stack) -> int | None:
    async with stack.runtime.session() as session:
        at = await session.scalar(select(MonitorWatermark.bar_open_time))
    return int(at.timestamp() * 1000) if at else None


def test_trails_up_to_the_ceiling_and_stops_there() -> None:
    async def scenario(state: World) -> None:
        stack, position_id, opened = await setup(state)
        feed = feed_with(state, position_id, opened, LOWS)
        trade(feed, state, "108")
        state.fake.set_price("BTCUSDT", Decimal(108))
        trailer = Trailer(stack.worker, feed)
        result = await trailer.on_bars(position_id)
        assert result is not None and result.step.value == "protected"
        position = await state.position(stack, position_id)
        assert position.desired_stop == position.exchange_stop == Decimal("104.00")
        assert await watermark(stack) == opened // M15 * M15 + 5 * M15
        lists = len(state.fake.lists)
        add_bars(feed, opened, {6: "110", 7: "111"})
        assert await trailer.on_bars(position_id) is None  # at the ceiling
        assert len(state.fake.lists) == lists

    run(scenario)


def test_the_ceiling_comes_from_the_positions_own_policy() -> None:
    async def scenario(state: World) -> None:
        stack, position_id, opened = await setup(state)
        async with stack.runtime.session() as session, session.begin():
            row = await session.get(Position, position_id, with_for_update=True)
            assert row is not None
            row.exit_policy = {**row.exit_policy, "trail_cap_pct": "2"}
        feed = feed_with(state, position_id, opened, LOWS)
        trade(feed, state, "108")
        state.fake.set_price("BTCUSDT", Decimal(108))
        await Trailer(stack.worker, feed).on_bars(position_id)
        position = await state.position(stack, position_id)
        assert position.exchange_stop == Decimal("102.00")

    run(scenario)


def test_crash_before_the_decision_is_stored_replays_the_bars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario(state: World) -> None:
        stack, position_id, opened = await setup(state)
        feed = feed_with(state, position_id, opened, {-1: "99", 0: "100"})
        trade(feed, state, "108")
        state.fake.set_price("BTCUSDT", Decimal(108))
        original = getattr(trailing_module, "commit_watermark")  # noqa: B009

        async def crash(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("process died with the bar")

        monkeypatch.setattr(trailing_module, "commit_watermark", crash)
        with pytest.raises(RuntimeError):
            await Trailer(stack.worker, feed).on_bars(position_id)
        position = await state.position(stack, position_id)
        assert position.desired_stop == Decimal(98) and await watermark(stack) is None
        monkeypatch.setattr(trailing_module, "commit_watermark", original)
        await Trailer(stack.worker, feed).on_bars(position_id)
        assert (await state.position(stack, position_id)).exchange_stop == Decimal(
            "98.90"
        )

    run(scenario)


def test_crash_after_the_decision_is_finished_by_catch_up() -> None:
    async def scenario(state: World) -> None:
        stack, position_id, opened = await setup(state)
        feed = feed_with(state, position_id, opened, LOWS)
        trade(feed, state, "108")
        state.fake.set_price("BTCUSDT", Decimal(108))

        async def crash(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("process died before the reprice")

        stack.worker.reprice = crash  # type: ignore[method-assign]
        with pytest.raises(RuntimeError):
            await Trailer(stack.worker, feed).on_bars(position_id)
        position = await state.position(stack, position_id)
        assert position.desired_stop == Decimal("104.00")
        assert position.exchange_stop == Decimal(98)
        await state.kill(stack)
        restarted = await state.stack()
        result = await Trailer(restarted.worker, feed).catch_up(position_id)
        assert result is not None and result.step.value == "protected"
        assert (await state.position(restarted, position_id)).exchange_stop == Decimal(
            "104.00"
        )

    run(scenario)


def test_moves_below_one_tick_only_advance_the_watermark() -> None:
    async def scenario(state: World) -> None:
        stack, position_id, opened = await setup(state)
        # candidate 98.0049 → 98.00: not a tick above the current 98.
        feed = feed_with(state, position_id, opened, {-1: "98.103", 0: "98.2"})
        trade(feed, state, "100")
        assert await Trailer(stack.worker, feed).on_bars(position_id) is None
        assert await watermark(stack) == opened // M15 * M15
        assert len(state.fake.lists) == 1

    run(scenario)


def test_a_new_stop_the_price_already_crossed_exits_at_market() -> None:
    async def scenario(state: World) -> None:
        stack, position_id, opened = await setup(state)
        feed = feed_with(state, position_id, opened, LOWS)
        trade(feed, state, "103")  # below the new 104 stop
        state.fake.set_price("BTCUSDT", Decimal(103))
        result = await Trailer(stack.worker, feed).on_bars(position_id)
        assert result is not None and result.step.value == "closed"
        position = await state.position(stack, position_id)
        assert position.outcome is not None
        assert (position.outcome["reason"], position.outcome["mechanism"]) == (
            ExitReason.TRAIL.value,
            ExitMechanism.MARKET_TRAIL.value,
        )

    run(scenario)
