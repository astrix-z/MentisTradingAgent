"""``release_entry``: the operator releases an entry the agent proves never placed.

An entry stays UNKNOWN while a BUY of an order the agent cannot name filled
after it (runbook §1). The command releases it only after the agent's own
check; whatever looks like it could be our order refuses and changes nothing.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from execution_world import Stack, World, run

from agent.common.commands import HANDLERS, AgentCommand, CommandContext
from agent.execution.models import AttemptState, IntentState
from agent.execution.orders import EntryOutcome, ReleaseRefused


async def unknown_entry(
    state: World, stack: Stack, fault: str
) -> tuple[uuid.UUID, str]:
    """An entry left UNKNOWN by ``fault`` on its BUY; its id and client id."""
    signal_id = await state.reserve(stack)
    state.fake.fault("new_order", fault)
    assert (await stack.orders.execute_entry(signal_id)).outcome is EntryOutcome.UNKNOWN
    [attempt] = await state.attempts(stack, signal_id)
    return signal_id, attempt.client_order_id


def manual_buy(state: World) -> None:
    """The owner buys BTC by hand (any client id that is not ``mta-``)."""
    state.fake.new_order(
        symbol="BTCUSDT",
        side="BUY",
        type="LIMIT",
        time_in_force="FOK",
        quantity="0.001",
        price=str(state.fake.last["BTCUSDT"] * 2),
        new_client_order_id="web_manual_1",
    )


async def still_held(state: World, stack: Stack, signal_id: uuid.UUID) -> None:
    intent = await state.intent(stack, signal_id)
    assert intent.state == IntentState.UNKNOWN and intent.quote_reserved > 0


def test_manual_buy_keeps_the_entry_unknown_until_released() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        signal_id, cid = await unknown_entry(state, stack, "timeout_before")
        manual_buy(state)
        with pytest.raises(ReleaseRefused, match="too recent"):
            await stack.orders.release_entry(cid)
        for _ in range(3):
            state.clock.advance(60)
            await stack.orders.resolve_entries()
        await still_held(state, stack, signal_id)  # the manual BUY holds it
        assert "unknown" in stack.runtime.alerts

        result = await stack.orders.release_entry(cid)
        assert result.outcome is EntryOutcome.RELEASED
        intent = await state.intent(stack, signal_id)
        assert intent.state == IntentState.NO_FILL
        assert intent.reason == "released_by_operator"
        assert intent.quote_reserved == 0
        [attempt] = await state.attempts(stack, signal_id)
        assert attempt.state == AttemptState.RESOLVED
        assert "unknown" not in stack.runtime.alerts
        await stack.orders.resolve_entries()  # nothing left to do
        assert (await state.intent(stack, signal_id)).state == IntentState.NO_FILL
        with pytest.raises(ReleaseRefused, match="nothing to release"):
            await stack.orders.release_entry(cid)

    run(scenario)


def test_our_invisible_order_is_not_released() -> None:
    # The BUY was accepted and filled; the exchange does not show it yet.
    async def scenario(state: World) -> None:
        stack = await state.stack()
        signal_id, cid = await unknown_entry(state, stack, "timeout_after")
        state.fake.fault("get_order", "not_found", times=1000)
        state.clock.advance(120)
        with pytest.raises(ReleaseRefused, match="cannot identify order"):
            await stack.orders.release_entry(cid)
        await still_held(state, stack, signal_id)

    run(scenario)


def test_our_visible_order_is_left_to_the_agent() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        signal_id, cid = await unknown_entry(state, stack, "timeout_after")
        state.clock.advance(120)
        with pytest.raises(ReleaseRefused, match="is on the exchange"):
            await stack.orders.release_entry(cid)
        await still_held(state, stack, signal_id)

    run(scenario)


def test_release_entry_command_reports_the_outcome() -> None:
    async def scenario(state: World) -> None:
        stack = await state.stack()
        signal_id, cid = await unknown_entry(state, stack, "timeout_before")
        manual_buy(state)
        state.clock.advance(120)
        stack.runtime.release_entry = stack.orders.release_entry
        engine = Mock(execution=stack.runtime, publish_heartbeat=AsyncMock())
        redis = Mock(pipeline=Mock())
        reports: list[dict[str, Any]] = []

        async def command(payload: dict[str, Any]) -> None:
            ctx = CommandContext(
                engine,
                redis,
                AgentCommand("c", "release_entry", payload, state.clock.now()),
            )
            ctx.report = AsyncMock(side_effect=lambda **kw: reports.append(kw))  # type: ignore[method-assign]
            await HANDLERS["release_entry"](ctx, payload)

        await command({})
        assert reports[-1] == {
            "ok": False,
            "message": "client_order_id is required",
        }
        await command({"client_order_id": "mta-000000000000000000000000-e1"})
        assert reports[-1]["ok"] is False
        assert "no entry attempt" in reports[-1]["message"]
        await command({"client_order_id": cid})
        assert reports[-1]["ok"] is True
        assert reports[-1]["data"] == {
            "client_order_id": cid,
            "reason": "released_by_operator",
        }
        assert (await state.intent(stack, signal_id)).state == IntentState.NO_FILL

    run(scenario)


# --- review 2026-10-03: hidden or paged-out trades are not absence ---------------


def test_lagging_history_is_not_proof_of_absence() -> None:
    # Our BUY filled, its answer was lost, the exchange does not find it by
    # client id and does not show its trades yet: the balance still does.
    async def scenario(state: World) -> None:
        stack = await state.stack()
        signal_id, cid = await unknown_entry(state, stack, "timeout_after")
        [buy] = state.fake.orders_of("BTCUSDT", "BUY")
        state.fake.hide_trades(buy.order_id)
        state.fake.fault("get_order", "not_found", times=1000)
        for _ in range(4):
            state.clock.advance(60)
            await stack.orders.resolve_entries()
        await still_held(state, stack, signal_id)
        with pytest.raises(ReleaseRefused, match="may have filled"):
            await stack.orders.release_entry(cid)
        await still_held(state, stack, signal_id)

    run(scenario)


def test_our_trade_paged_out_by_later_manual_buys_is_still_found() -> None:
    # 1001 manual BUYs after ours: our trade is not among the last 1000, but
    # the history is read from the attempt's time on, every page.
    async def scenario(state: World) -> None:
        stack = await state.stack()
        signal_id, cid = await unknown_entry(state, stack, "timeout_after")
        for n in range(1001):
            state.fake.new_order(
                symbol="BTCUSDT",
                side="BUY",
                type="LIMIT",
                time_in_force="FOK",
                quantity="0.00001",
                price=str(state.fake.last["BTCUSDT"] * 2),
                new_client_order_id=f"web_manual_{n}",
            )
        state.fake.fault("get_order", "not_found", times=1)  # by client id only
        state.clock.advance(120)
        with pytest.raises(ReleaseRefused, match="may be ours"):
            await stack.orders.release_entry(cid)
        await still_held(state, stack, signal_id)
        # Auto-recovery: two misses by client id, then the paged history
        # shows a BUY nobody accounts for (ours) — UNKNOWN, not released.
        state.fake.fault("get_order", "not_found", times=2)
        for _ in range(2):
            await stack.orders.resolve_entries()
        await still_held(state, stack, signal_id)

    run(scenario, usdt="10000")


# --- re-review 2026-10-03: a foreign BUY's base fee is not ours --------------------


def test_a_foreign_buys_base_fee_does_not_hide_our_hidden_buy() -> None:
    # Commission in the base asset. Our BUY filled, its answer was lost and
    # its trades are hidden; then the owner tops up USDT and buys 20 times our
    # quantity by hand. Counted gross, that BUY's fee (2% of ours) would eat
    # the 1% margin of our BUY in the balance and look like absence.
    async def scenario(state: World) -> None:
        stack = await state.stack()
        signal_id, cid = await unknown_entry(state, stack, "timeout_after")
        [buy] = state.fake.orders_of("BTCUSDT", "BUY")
        state.fake.hide_trades(buy.order_id)
        qty = buy.executed
        price = state.fake.last["BTCUSDT"]
        state.fake._move("USDT", qty * 20 * price * 2)
        state.fake.new_order(
            symbol="BTCUSDT",
            side="BUY",
            type="LIMIT",
            time_in_force="FOK",
            quantity=str(qty * 20),
            price=str(price * 2),
            new_client_order_id="web_manual_big",
        )
        state.fake.fault("get_order", "not_found", times=1)  # ours, by client id
        state.clock.advance(120)
        with pytest.raises(ReleaseRefused, match="may have filled"):
            await stack.orders.release_entry(cid)
        await still_held(state, stack, signal_id)

    run(scenario, commission_asset="base")
