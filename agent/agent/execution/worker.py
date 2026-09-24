"""PositionWorker: every change to a protected position, one at a time (§6.7).

A protected position changes only through one serialized protocol, whatever
asked for it (trailing, time exit, guard, manual close):

1. **record** the operation, its new version and ``unprotected_since`` in the
   database *before* anything is sent — protection is about to go away;
2. **cancel** the list (``delete_order_list``);
3. **barrier**: both old legs terminal, the booked fills of each leg equal its
   ``executedQty`` (read from the exchange per leg, not inferred from the
   list's ``ALL_DONE``), commissions included; until then nothing is sent;
4. from the proven remainder: a **new OCO** (the same path as the first
   protection, with the new stop) or a **market exit**; nothing sellable left
   → the position is closed.

An UNKNOWN cancel stops the protocol at step 2 — the old list may still be
live and protecting — until it is resolved. The deadline of plan §6.3 counts
from the ``unprotected_since`` recorded at step 1: when it passes the position
becomes ``protection_overdue`` (alert, priority reconcile), but nothing is
sold until the barrier has proven the remainder. A restart resumes the
operation from the database.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from enum import StrEnum
from typing import Any

from agent.exchange.budget import Priority
from agent.exchange.trade import Outcome, TradeCallError
from agent.execution.ledger import lock_position, record_event
from agent.execution.models import Position, PositionStatus
from agent.execution.outcome import ExitMechanism, ExitReason
from agent.execution.protection import TERMINAL, Protector, Step, StepResult
from agent.execution.sizing import sellable

logger = logging.getLogger(__name__)

NOT_FOUND = frozenset({-2011, -2013})


class Operation(StrEnum):
    REPRICE = "reprice"
    EXIT = "exit"


class PositionWorker:
    def __init__(self, protector: Protector) -> None:
        self.protector = protector
        self.orders = protector.orders

    @property
    def runtime(self) -> Any:
        return self.orders.runtime

    # --- entry points ----------------------------------------------------------------

    async def reprice(self, position_id: uuid.UUID, stop: Decimal) -> StepResult:
        """Move the exchange stop to ``stop`` (cancel → barrier → new OCO)."""
        return await self._run(position_id, Operation.REPRICE, {"stop": str(stop)})

    async def close(
        self, position_id: uuid.UUID, reason: ExitReason, mechanism: ExitMechanism
    ) -> StepResult:
        """Sell the position at market (cancel → barrier → MARKET SELL)."""
        return await self._run(
            position_id,
            Operation.EXIT,
            {"reason": reason.value, "mechanism": mechanism.value},
        )

    async def resume(self, position_id: uuid.UUID) -> StepResult:
        """Continue an operation a previous process recorded but did not finish."""
        position = await self.protector._position(position_id)
        if position.operation is None or position.op_state is None:
            return StepResult(Step.WAITING, "nothing to resume")
        async with self.orders.lock_for(position_id):
            return await self._continue(position_id)

    # --- the protocol ---------------------------------------------------------------------

    async def _run(
        self, position_id: uuid.UUID, operation: Operation, target: dict[str, Any]
    ) -> StepResult:
        async with self.orders.lock_for(position_id):
            if not self.runtime.may_mutate():
                return StepResult(Step.WAITING, "no execution lock")
            started = await self._record(position_id, operation, target)
            if started is not None:
                return started
            return await self._continue(position_id)

    async def _record(
        self, position_id: uuid.UUID, operation: Operation, target: dict[str, Any]
    ) -> StepResult | None:
        """Step 1: operation and loss of protection are durable before a send."""
        async with self.runtime.session() as session, session.begin():
            position = await lock_position(session, position_id)
            if position.status == PositionStatus.CLOSED:
                return StepResult(Step.CLOSED)
            if position.operation is not None and position.operation != operation:
                # An exit outranks a reprice already in flight; a second
                # reprice while one runs just updates its target.
                if operation is Operation.REPRICE:
                    return StepResult(Step.WAITING, f"{position.operation} in progress")
            if position.unprotected_since is None:
                position.unprotected_since = self.orders.clock()
            position.operation = operation.value
            position.op_version += 1
            position.op_state = {
                **target,
                "list": position.protection_list_id,
                "cancel": (position.op_state or {}).get("cancel"),
            }
            if position.status in (PositionStatus.PROTECTED, PositionStatus.REPRICING):
                position.status = (
                    PositionStatus.REPRICING
                    if operation is Operation.REPRICE
                    else PositionStatus.CLOSING
                )
            await record_event(
                session,
                account=position.account,
                network=position.network,
                kind="operation_started",
                payload={
                    "operation": operation.value,
                    "version": position.op_version,
                    **target,
                },
                position=position,
            )
        return None

    async def _continue(self, position_id: uuid.UUID) -> StepResult:
        position = await self.protector._position(position_id)
        state = dict(position.op_state or {})
        list_id = state.get("list")
        if list_id:
            cancelled = await self._cancel(position, list_id, state)
            if cancelled is not None:
                await self.protector.check_deadline(position_id)
                return cancelled
            proven = await self._barrier(position_id, list_id)
            if proven is not None:
                await self.protector.check_deadline(position_id)
                return proven
        position = await self.protector._position(position_id)
        if position.status == PositionStatus.CLOSED:
            return StepResult(Step.CLOSED)
        operation = Operation(position.operation or Operation.EXIT)
        if operation is Operation.EXIT:
            result = await self.protector._exit(
                position_id,
                ExitReason(state.get("reason", ExitReason.MANUAL.value)),
                ExitMechanism(state.get("mechanism", ExitMechanism.MANUAL_CLOSE.value)),
            )
        else:
            await self._set_protecting(position_id)
            if await self.protector.check_deadline(position_id):
                result = await self.protector._exit(
                    position_id, ExitReason.EMERGENCY, ExitMechanism.MARKET_UNPROTECTED
                )
            else:
                placed = await self.protector._place(
                    await self.protector._position(position_id),
                    stop_at=Decimal(state["stop"]),
                )
                result = placed or StepResult(Step.WAITING, "replacement pending")
        if result.step in (Step.PROTECTED, Step.CLOSED):
            await self._clear(position_id)
        return result

    async def _cancel(
        self, position: Position, list_id: str, state: dict[str, Any]
    ) -> StepResult | None:
        """Step 2: cancel the old list; ``None`` once its fate is known."""
        if state.get("cancel") == "done":
            return None
        trade = self.protector._trade()
        try:
            await trade.delete_order_list(
                symbol=position.symbol, list_client_order_id=list_id
            )
        except TradeCallError as error:
            if error.outcome is Outcome.UNKNOWN:
                await self._note(position.id, cancel="unknown")
                return StepResult(Step.WAITING, "cancel outcome unknown")
            if error.exchange_code not in NOT_FOUND:
                return StepResult(Step.WAITING, f"cancel refused: {error.message}")
            # Unknown list: it already finished (a leg filled) — the barrier
            # below reads what happened.
        await self._note(position.id, cancel="done")
        return None

    async def _barrier(self, position_id: uuid.UUID, list_id: str) -> StepResult | None:
        """Step 3: both legs terminal and fully booked, or wait."""
        position = await self.protector._position(position_id)
        trade = self.protector._trade()
        try:
            order_list = (
                await trade.get_order_list(
                    list_client_order_id=list_id, priority=Priority.PROTECT
                )
            ).data
        except TradeCallError as exc:
            return StepResult(Step.WAITING, f"list not readable: {exc.message}")
        executed: dict[int, Decimal] = {}
        for leg in order_list.orders or []:
            try:
                order = (
                    await trade.get_order(
                        symbol=position.symbol,
                        client_order_id=str(leg.client_order_id),
                        priority=Priority.PROTECT,
                    )
                ).data
            except TradeCallError as exc:
                return StepResult(Step.WAITING, f"leg not readable: {exc.message}")
            if str(order.status) not in TERMINAL:
                return StepResult(
                    Step.WAITING, f"leg {leg.client_order_id} still {order.status}"
                )
            if order.order_id is None:
                return StepResult(Step.WAITING, "leg without an order id")
            executed[order.order_id] = Decimal(order.executed_qty or "0")
        filled_type = await self.protector._book_orders(
            position_id, [order_id for order_id, qty in executed.items() if qty > 0]
        )
        async with self.runtime.session() as session:
            locked = await lock_position(session, position_id)
            proven = await self.orders.applier.proven(session, locked, executed)
        if not proven:
            return StepResult(Step.WAITING, "leg fills not proven yet")
        position = await self.protector._position(position_id)
        filters, quote = await self.protector._market(position, strict=False)
        price = quote.last_price() if quote is not None else position.fill_vwap
        if (
            filters is not None
            and price is not None
            and not sellable(filters, position.quantity, price)
        ):
            above = filled_type == "LIMIT_MAKER"
            await self.protector._finalize(
                position_id,
                ExitReason.TP if above else ExitReason.STOP,
                ExitMechanism.EXCHANGE_TP if above else ExitMechanism.EXCHANGE_STOP,
            )
            await self._clear(position_id)
            return StepResult(Step.CLOSED)
        return None

    # --- bookkeeping -------------------------------------------------------------------------

    async def _note(self, position_id: uuid.UUID, **values: Any) -> None:
        async with self.runtime.session() as session, session.begin():
            position = await lock_position(session, position_id)
            position.op_state = {**(position.op_state or {}), **values}

    async def _set_protecting(self, position_id: uuid.UUID) -> None:
        """The old list is gone for good; the position waits for its new one."""
        async with self.runtime.session() as session, session.begin():
            position = await lock_position(session, position_id)
            if position.status in (PositionStatus.REPRICING, PositionStatus.PROTECTED):
                position.status = PositionStatus.UNPROTECTED
            position.protection_list_id = None
            position.exchange_stop = None

    async def _clear(self, position_id: uuid.UUID) -> None:
        async with self.runtime.session() as session, session.begin():
            position = await lock_position(session, position_id)
            position.operation = None
            position.op_state = None


__all__ = ["Operation", "PositionWorker"]
