"""EXEC-11b: startup integration — reconcile before admission, restored entries."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from execution_world import World, run

from agent.exchange.budget import RequestBudget
from agent.exchange.rest import ExchangeRest
from agent.exchange.trade import TradeClient
from agent.execution.admission import Admission
from agent.execution.filters import FilterCache
from agent.execution.models import IntentState, PositionStatus
from agent.execution.orders import EntryOutcome
from agent.execution.quotes import QuoteProvider
from agent.execution.runtime import ExecutionRuntime
from agent.execution.service import ExecutionService


def service(state: World) -> ExecutionService:
    """A fresh process: runtime, admission and service, not yet booted."""
    fake, clock = state.fake, state.clock
    runtime = ExecutionRuntime(
        db=state.db,
        db_url=state.url,
        schema_ready=True,
        trade_factory=lambda network: TradeClient(
            "live", fake.spot(), budget=RequestBudget()
        ),
        clock=clock.now,
    )
    rest = ExchangeRest("live", fake.spot(), RequestBudget())

    async def fetch(network: str, symbol: str) -> Any:
        return await rest.exchange_info(symbol)

    async def price(network: str, symbol: str) -> Decimal | None:
        return fake.last.get(symbol)

    async def bnb(asset: str, at: Any) -> Decimal | None:
        return fake.bnb_price

    filters = FilterCache(fetch)
    admission = Admission(
        runtime,
        quotes=lambda network: QuoteProvider(rest, clock=clock.now),
        filters=filters,
        prices=price,
        clock=clock.now,
    )
    runtime.set_admission_wake(admission.wake)
    built = ExecutionService(
        runtime,
        admission=admission,
        market=lambda network: rest,
        hub=None,
        filters=filters,
        clock=clock.now,
        sleep=clock.sleep,
        commission_price=bnb,
    )
    runtime.settings = state.settings
    return built


def test_buy_filled_before_the_crash_is_opened_and_protected_after_restart() -> None:
    async def scenario(state: World) -> None:
        first = await state.stack()
        signal_id = await state.reserve(first)
        state.fake.fault("new_order", "timeout_after")  # BUY fills, answer lost
        assert (
            await first.orders.execute_entry(signal_id)
        ).outcome is EntryOutcome.UNKNOWN
        await state.kill(first)  # dies before any position was written

        restarted = service(state)
        seen: list[bool] = []
        original = restarted.reconciler.run

        async def watched() -> Any:
            seen.append(restarted.runtime.admission_block() is None)
            return await original()

        restarted.reconciler.run = watched  # type: ignore[method-assign]
        await restarted.runtime.boot()  # lock → startup reconcile → mutations
        assert seen == [False]  # admission was closed while reconciling
        intent = await state.intent(first, signal_id)
        assert intent.state == IntentState.FILLED
        position = await state.position_of(first, signal_id)
        assert position is not None and position.status == PositionStatus.PROTECTING
        await restarted.tick()
        position = await state.position_of(first, signal_id)
        assert position is not None and position.status == PositionStatus.PROTECTED
        assert len(state.fake.orders_of("BTCUSDT", "BUY")) == 1
        await restarted.stop()
        await restarted.runtime.shutdown()

    run(scenario)


def test_restart_in_the_middle_of_a_reprice_finishes_it() -> None:
    async def scenario(state: World) -> None:
        first = await state.stack()
        position_id = await state.protected(first)
        state.fake.fault("order_list_oco", "timeout_before")  # replacement lost
        await first.worker.reprice(position_id, Decimal("99.5"))
        await state.kill(first)
        state.clock.advance(70)  # the lost OCO is provably absent now
        state.fake.set_price("BTCUSDT", Decimal(100))
        restarted = service(state)
        await restarted.runtime.boot()
        for _ in range(3):
            await restarted.tick()
        position = await state.position(first, position_id)
        # 70 s without protection is past the 30 s deadline: once the lost
        # replacement is proven absent, the proven remainder is sold.
        assert position.status == PositionStatus.CLOSED and position.outcome is not None
        assert position.outcome["mechanism"] == "market_unprotected"
        assert position.operation is None
        sells = [
            o for o in state.fake.orders_of("BTCUSDT", "SELL") if o.type == "MARKET"
        ]
        assert len(sells) == 1
        await restarted.stop()
        await restarted.runtime.shutdown()

    run(scenario)


def test_repeated_reconcile_does_not_book_twice() -> None:
    async def scenario(state: World) -> None:
        first = await state.stack()
        position_id = await state.protected(first)
        await state.kill(first)
        restarted = service(state)
        await restarted.runtime.boot()
        before = await state.position(first, position_id)
        await restarted.reconciler.run()
        await restarted.reconciler.run()
        after = await state.position(first, position_id)
        assert (
            after.bought_qty == before.bought_qty and after.sold_qty == before.sold_qty
        )
        assert after.status == PositionStatus.PROTECTED
        await restarted.stop()
        await restarted.runtime.shutdown()

    run(scenario)


def test_reserved_entry_left_by_a_dead_process_is_executed_once() -> None:
    async def scenario(state: World) -> None:
        first = await state.stack()
        signal_id = await state.reserve(first)  # reserved, never sent
        await state.kill(first)
        restarted = service(state)
        await restarted.runtime.boot()
        restarted.runtime.open_admission("tripple_screen")
        await restarted.tick()
        await restarted.tick()
        intent = await state.intent(first, signal_id)
        assert intent.state == IntentState.FILLED
        position = await state.position_of(first, signal_id)
        assert position is not None and position.status == PositionStatus.PROTECTED
        assert len(state.fake.orders_of("BTCUSDT", "BUY")) == 1
        await restarted.stop()
        await restarted.runtime.shutdown()

    run(scenario)
