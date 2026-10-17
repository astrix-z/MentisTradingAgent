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

A symbol that does not trade (``HALT``, ``BREAK``, delisted) keeps its list:
an operation that would cancel it is not started — nothing could be sold or
placed in its stead, and the position would stay without a stop until trading
resumes. The operation is asked for again on every round (time exit, trailing)
and runs once the symbol trades. One recorded before the halt whose cancel was
never sent is withdrawn the same way. Only a ``TRADING`` read just now lets a
list be cancelled; a status that cannot be read keeps it too.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from enum import StrEnum
from typing import Any

from agent.exchange.budget import Priority
from agent.exchange.trade import Outcome, TradeCallError
from agent.execution.attempts import Role
from agent.execution.ledger import lock_position, record_event
from agent.execution.models import Position, PositionStatus
from agent.execution.outcome import ExitMechanism, ExitReason
from agent.execution.protection import Protector, Step, StepResult
from agent.execution.runtime import NOT_LEADER

logger = logging.getLogger(__name__)

NOT_FOUND = frozenset({-2011, -2013})
#: Alert of a symbol whose list is kept because it does not trade.
HALTED = "halted:"
#: Seconds a known halt may be old when it keeps a list; ``TRADING`` that lets
#: a list be cancelled is always read anew.
STATUS_MAX_AGE = 60.0


class StatusUnknown(RuntimeError):
    """The symbol's status could not be read: nothing may rely on it."""


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
        """Continue an operation a previous process recorded but did not finish.

        Like every operation step it cancels and sends, so it waits for the
        execution lock and a finished reconcile: the startup reconcile itself
        only reads and records, and the first round after it resumes.
        """
        position = await self.protector._position(position_id)
        if position.operation is None or position.op_state is None:
            return StepResult(Step.WAITING, "nothing to resume")
        async with self.orders.lock_for(position_id):
            if not self.runtime.may_mutate():
                return StepResult(Step.WAITING, "no execution lock")
            return await self._continue(position_id)

    # --- the protocol ---------------------------------------------------------------------

    async def not_trading(
        self, position: Position, *, fresh: bool = False
    ) -> str | None:
        """Why the position's symbol takes no orders now, or ``None``.

        ``None`` (it trades) is always read from the exchange just now. A halt
        already alerted may come from the cache, up to ``STATUS_MAX_AGE``
        seconds old, unless ``fresh``. Sets or clears the ``halted:`` alert;
        raises :class:`StatusUnknown` when the status cannot be read.
        """
        alert = f"{HALTED}{position.symbol}"
        known_halt = alert in self.runtime.alerts and not fresh
        try:
            filters = await self.orders.filters.get(
                position.network,
                position.symbol,
                max_age=STATUS_MAX_AGE if known_halt else 0,
            )
            if filters.tradable and known_halt:
                filters = await self.orders.filters.get(
                    position.network, position.symbol, max_age=0
                )
        except LookupError:
            status = "not listed"
        except Exception as exc:
            raise StatusUnknown(f"status of {position.symbol} unknown: {exc}") from exc
        else:
            if filters.tradable:
                if self.runtime.alerts.pop(alert, None) is not None:
                    logger.info("%s trades again", position.symbol)
                return None
            status = filters.status
        if alert not in self.runtime.alerts:
            logger.warning(
                "%s is %s: its list stays, exits wait for trading",
                position.symbol,
                status,
            )
        self.runtime.alerts[alert] = (
            f"{position.symbol} is {status}: protection kept on the exchange, "
            "exits wait for trading"
        )
        return status

    async def _halted(self, position: Position) -> StepResult | None:
        """WAITING when cancelling the live list now would leave it unprotected."""
        state = position.op_state or {}
        if position.protection_list_id is None or (
            state.get("list") == position.protection_list_id
            and state.get("cancel") is not None
        ):
            return None
        try:
            status = await self.not_trading(position)
        except StatusUnknown as exc:
            logger.warning("%s: list kept", exc)
            return StepResult(
                Step.WAITING, f"{position.symbol} status unknown: list kept"
            )
        if status is None:
            return None
        return StepResult(Step.WAITING, f"{position.symbol} is {status}: list kept")

    async def _run(
        self, position_id: uuid.UUID, operation: Operation, target: dict[str, Any]
    ) -> StepResult:
        async with self.orders.lock_for(position_id):
            if not self.runtime.may_mutate():
                return StepResult(Step.WAITING, "no execution lock")
            halted = await self._halted(await self.protector._position(position_id))
            if halted is not None:
                return halted
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
            previous = position.op_state or {}
            same_list = previous.get("list") == position.protection_list_id
            position.op_state = {
                **target,
                "list": position.protection_list_id,
                # A cancel already done counts only for the list it was for.
                "cancel": previous.get("cancel") if same_list else None,
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

    async def _continue(
        self, position_id: uuid.UUID, *, again: bool = True
    ) -> StepResult:
        position = await self.protector._position(position_id)
        state = dict(position.op_state or {})
        list_id = state.get("list")
        if list_id and state.get("cancel") is None:
            halted = await self._halted(position)
            if halted is not None:
                await self._withdraw(position_id, list_id)
                return halted
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
        replacement = await self._settle_replacement(position_id)
        if replacement is not None:
            if replacement.step is Step.PROTECTED and operation is Operation.EXIT:
                # The replacement OCO is live after all: the exit now has to
                # cancel it (and prove its legs) before it may sell.
                await self._record(position_id, Operation.EXIT, _exit_target(state))
                if again:
                    return await self._continue(position_id, again=False)
                return StepResult(Step.WAITING, "exit restarted on the live list")
            if replacement.step is Step.PROTECTED:
                await self._clear(position_id)
            else:
                await self.protector.check_deadline(position_id)
            return replacement
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

    async def _settle_replacement(self, position_id: uuid.UUID) -> StepResult | None:
        """Resolve a replacement OCO of unproven outcome before anything is sent.

        A previous round may have sent the new list and lost the answer, or
        had the answer and crashed before applying it. It
        may be live and hold the asset: neither another OCO nor a market sale
        may go out until it is proven absent (``None``), live (PROTECTED) or
        finished (its fills booked).
        """
        position = await self.protector._position(position_id)
        for answered in await self.protector._unapplied_lists(position):
            # Accepted, answer committed, never associated: it may be live.
            return await self.protector._apply_answered(position, answered)
        for attempt in await self.protector._open_attempts(position, (Role.LIST,)):
            resolved = await self.protector._resolve_list(position, attempt)
            if resolved is not None:
                return resolved
            position = await self.protector._position(position_id)
        return None

    async def _cancel(
        self, position: Position, list_id: str, state: dict[str, Any]
    ) -> StepResult | None:
        """Step 2: cancel the old list; ``None`` once its fate is known."""
        if state.get("cancel") == "done":
            return None
        # Recording the operation was awaited: ask the database, not the flag.
        if not await self.runtime.confirm_lead():
            return StepResult(Step.WAITING, "no execution lock")
        trade = self.protector._trade()
        try:
            await trade.delete_order_list(
                symbol=position.symbol, list_client_order_id=list_id
            )
        except TradeCallError as error:
            if error.outcome is Outcome.UNKNOWN:
                await self._note(position.id, cancel="unknown")
                return StepResult(Step.WAITING, "cancel outcome unknown")
            if error.error_type == NOT_LEADER:
                return StepResult(Step.WAITING, "no execution lock")
            if error.exchange_code not in NOT_FOUND:
                return StepResult(Step.WAITING, f"cancel refused: {error.message}")
            # Unknown list: it already finished (a leg filled) — the barrier
            # below reads what happened.
        await self._note(position.id, cancel="done")
        return None

    async def adopt_finished_list(
        self, position_id: uuid.UUID, list_id: str, stop: Decimal | None
    ) -> None:
        """A protecting list found finished (by the reconciler): make its legs
        an operation of this worker, so they are proven before anything else.

        Recorded as a reprice to ``stop`` whose cancel is moot. The caller
        holds the position's lock.
        """
        await self._record(position_id, Operation.REPRICE, {"stop": str(stop)})
        await self._note(position_id, list=list_id, cancel="done")

    async def barrier(self, position_id: uuid.UUID, list_id: str) -> StepResult | None:
        """:meth:`_barrier` for a caller that holds the position's lock."""
        return await self._barrier(position_id, list_id)

    async def _barrier(self, position_id: uuid.UUID, list_id: str) -> StepResult | None:
        """Step 3: both legs terminal and fully booked, or wait."""
        trade = self.protector._trade()
        try:
            order_list = (
                await trade.get_order_list(
                    list_client_order_id=list_id, priority=Priority.PROTECT
                )
            ).data
        except TradeCallError as exc:
            return StepResult(Step.WAITING, f"list not readable: {exc.message}")
        proof = await self.protector.prove_legs(position_id, order_list)
        if not proof.proven:
            return StepResult(Step.WAITING, proof.reason)
        position = await self.protector._position(position_id)
        if await self.protector.nothing_to_sell(position):
            above = proof.filled_type == "LIMIT_MAKER"
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

    async def _withdraw(self, position_id: uuid.UUID, list_id: str) -> None:
        """Drop an operation whose cancel was never sent: its list still protects."""
        async with self.runtime.session() as session, session.begin():
            position = await lock_position(session, position_id)
            state = position.op_state or {}
            if (
                position.operation is None
                or state.get("list") != list_id
                or state.get("cancel") is not None
                or position.protection_list_id != list_id
            ):
                return
            await record_event(
                session,
                account=position.account,
                network=position.network,
                kind="operation_withdrawn",
                payload={"operation": position.operation, "list": list_id},
                position=position,
            )
            position.operation = None
            position.op_state = None
            position.unprotected_since = None
            if position.status in (
                PositionStatus.REPRICING,
                PositionStatus.CLOSING,
                PositionStatus.PROTECTION_OVERDUE,
            ):
                position.status = PositionStatus.PROTECTED
            symbol = position.symbol
        self.runtime.alerts.pop(f"overdue:{symbol}", None)

    async def _clear(self, position_id: uuid.UUID) -> None:
        async with self.runtime.session() as session, session.begin():
            position = await lock_position(session, position_id)
            position.operation = None
            position.op_state = None


def _exit_target(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "reason": state.get("reason", ExitReason.MANUAL.value),
        "mechanism": state.get("mechanism", ExitMechanism.MANUAL_CLOSE.value),
    }


__all__ = ["HALTED", "Operation", "PositionWorker", "StatusUnknown"]
