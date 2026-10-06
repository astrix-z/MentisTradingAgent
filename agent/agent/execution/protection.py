"""Protection and market exits of a position (plan §6.2, §6.3, §6.7).

EXEC-08c — the first protection after a fill:

* the deadline runs from ``unprotected_since`` (first detection of the fill),
  stored with the position and never moved: not by a restart, not by a
  repeated reconcile, not by fills that arrive late;
* OCO attempts start no earlier than 0, 1, 3, 7 and 15 s after it, and only
  once the previous attempt's outcome is proven — an UNKNOWN OCO is looked up
  first, never sent around;
* protection is computed only from a **proven** ledger (the barrier);
* a deterministic refusal (filters, price) goes straight to an exit;
* at the deadline the position becomes ``protection_overdue`` (alert, entries
  closed) — :meth:`Protector.check_deadline` does that on its own clock, even
  while a request is in flight. The emergency ``MARKET SELL`` follows only
  when nothing can still be protecting the position (no OCO of unknown
  outcome) and only for the proven remainder.

:meth:`Protector.exit_market` is the one way a position is sold at market (gap,
emergency, time exit, trailing, manual close): it resolves earlier exit
attempts first, sells only the proven remainder, allows further SELLs only for
a confirmed remainder after a partial one, and finalizes the position when
nothing sellable is left.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import select

from agent.exchange.budget import Priority
from agent.exchange.trade import Outcome, TradeCallError, TradeClient
from agent.execution.attempts import Found, Role, client_id
from agent.execution.filters import SymbolFilters, round_down, round_up
from agent.execution.ledger import (
    FillRecord,
    Finalizer,
    lock_position,
    record_event,
    transaction_time,
)
from agent.execution.models import AttemptState, OrderAttempt, Position, PositionStatus
from agent.execution.orders import OrderManager
from agent.execution.outcome import ExitMechanism, ExitReason
from agent.execution.quotes import QuoteSnapshot
from agent.execution.runtime import NOT_LEADER, ExecutionContext, ExecutionRuntime
from agent.execution.sizing import (
    Skip,
    sellable,
    stop_limit_price,
    stop_order_type,
    validate_market_sell,
    validate_protection,
)
from agent.strategies.base import ExitPolicy, PositionEvent

logger = logging.getLogger(__name__)

#: Earliest start of the n-th OCO attempt after ``unprotected_since`` (s).
ATTEMPT_OFFSETS = (0, 1, 3, 7, 15)
#: How old a quote a market exit still accepts (see ``_exit``).
EXIT_QUOTE_AGE = timedelta(minutes=10)
#: Refusals that will not change by trying again with the same legs.
DETERMINISTIC_CODES = frozenset({-1013, -1111, -1100, -2010})
TERMINAL = ("FILLED", "CANCELED", "EXPIRED", "REJECTED", "EXPIRED_IN_MATCH")


class Step(StrEnum):
    PROTECTED = "protected"
    CLOSED = "closed"
    WAITING = "waiting"  # blocked for now (unknown outcome, fills lag, limits)
    OVERDUE = "overdue"


@dataclass(frozen=True)
class LegsProof:
    """Whether a finished list's legs are terminal and fully booked."""

    proven: bool
    reason: str = ""
    filled_type: str | None = None  # the type of the leg that sold, if any


@dataclass(frozen=True)
class StepResult:
    step: Step
    reason: str | None = None


def _insufficient(error: TradeCallError) -> bool:
    return "insufficient balance" in error.message.lower()


class Protector:
    def __init__(self, orders: OrderManager, finalizer: Finalizer) -> None:
        self.orders = orders
        self.finalizer = finalizer

    @property
    def runtime(self) -> ExecutionRuntime:
        return self.orders.runtime

    # --- protection -----------------------------------------------------------------

    async def protect(self, position_id: uuid.UUID) -> StepResult:
        async with self.orders.lock_for(position_id):
            return await self._protect(position_id)

    async def _protect(self, position_id: uuid.UUID) -> StepResult:
        for _ in range(len(ATTEMPT_OFFSETS) + 20):
            position = await self._position(position_id)
            if position.status == PositionStatus.CLOSED:
                return StepResult(Step.CLOSED)
            if position.status == PositionStatus.PROTECTED:
                return StepResult(Step.PROTECTED)
            if not self.runtime.may_mutate():
                return StepResult(Step.WAITING, "no execution lock")
            if position.operation is not None:
                # The worker owns the position until its operation is done:
                # an old list's legs may still be unproven (PositionWorker).
                await self.check_deadline(position_id)
                return StepResult(Step.WAITING, f"{position.operation} in progress")
            await self.check_deadline(position_id)
            unapplied = await self._unapplied_lists(position)
            if unapplied:
                return await self._apply_answered(position, unapplied[0])
            pending = await self._open_attempts(position, (Role.LIST,))
            if pending:
                resolved = await self._resolve_list(position, pending[-1])
                if resolved is not None:
                    return resolved
                continue
            if await self._open_attempts(position, (Role.EXIT,)):
                return await self._exit(
                    position_id, ExitReason.EMERGENCY, ExitMechanism.MARKET_UNPROTECTED
                )
            if not await self.orders.prove_entry(position_id) or not await self._proven(
                position_id
            ):
                return StepResult(Step.WAITING, "fills not proven")
            position = await self._position(position_id)
            if position.status == PositionStatus.PROTECTION_OVERDUE:
                return await self._exit(
                    position_id, ExitReason.EMERGENCY, ExitMechanism.MARKET_UNPROTECTED
                )
            waited = await self._wait_for_slot(position)
            if waited is not None:
                return waited
            result = await self._place(position)
            if result is not None:
                return result
        return StepResult(Step.WAITING, "protection still pending")

    async def _wait_for_slot(self, position: Position) -> StepResult | None:
        """Sleep until the next attempt may start; ``None`` when it may now."""
        async with self.runtime.session() as session:
            sequence = await self.orders.journal.next_sequence(
                session, signal_id=position.signal_id, role=Role.LIST
            )
        assert position.unprotected_since is not None
        deadline = self._deadline(position)
        now = self.orders.clock()
        if sequence > len(ATTEMPT_OFFSETS):
            if now < deadline:
                await self.orders.sleep((deadline - now).total_seconds())
            await self.check_deadline(position.id)
            return (
                None if now >= deadline else StepResult(Step.WAITING, "attempts used")
            )
        earliest = position.unprotected_since + timedelta(
            seconds=ATTEMPT_OFFSETS[sequence - 1]
        )
        if now < earliest:
            await self.orders.sleep((min(earliest, deadline) - now).total_seconds())
        return None

    async def _place(
        self, position: Position, *, stop_at: Decimal | None = None
    ) -> StepResult | None:
        """Compute the legs from the proven ledger and send one OCO.

        ``stop_at`` replaces the initial stop (a trailing reprice); the
        take-profit always comes from the policy and the fill.
        """
        runtime = self.runtime
        filters, quote = await self._market(position)
        if filters is None or quote is None:
            return StepResult(Step.WAITING, "market data unavailable")
        policy = ExitPolicy.from_transport(position.exit_policy)
        assert position.fill_vwap is not None
        qty = round_down(position.quantity, filters.step)
        initial = policy.initial_stop(position.fill_vwap)
        if stop_at is None and position.desired_stop is not None:
            # Replacing a lost list: the ratchet never goes back down.
            stop_at = max(position.desired_stop, initial)
        trailing = stop_at is not None and stop_at > initial
        stop = round_down(stop_at if stop_at is not None else initial, filters.tick)
        tp = round_up(policy.take_profit(position.fill_vwap), filters.tick)
        stop_type = stop_order_type(filters)
        limit = (
            stop_limit_price(filters, stop, runtime.settings.sizing())
            if stop_type == "STOP_LOSS_LIMIT"
            else None
        )
        last = quote.last_price()
        if not sellable(filters, qty, last):
            return await self._close_dust(position.id)
        if last <= stop:
            if trailing:  # a trailing stop the price already crossed
                return await self._exit(
                    position.id, ExitReason.TRAIL, ExitMechanism.MARKET_TRAIL
                )
            return await self._exit(
                position.id, ExitReason.STOP, ExitMechanism.MARKET_GAP
            )
        problems = validate_protection(
            filters,
            quote,
            self.orders.clock(),
            runtime.settings.sizing(),
            qty=qty,
            take_profit=tp,
            stop=stop,
            limit_price=limit,
            last=last,
        )
        if any(p.reason is Skip.REFERENCE_UNVERIFIED for p in problems):
            return StepResult(Step.WAITING, problems[0].detail)
        if problems:
            logger.error("protection of %s impossible: %s", position.symbol, problems)
            return await self._exit(
                position.id, ExitReason.EMERGENCY, ExitMechanism.MARKET_UNPROTECTED
            )
        # The market reads were awaited: the lock may be gone meanwhile, and
        # its flag only drops when someone asks the database (EXEC-05).
        if not await runtime.confirm_lead():
            return StepResult(Step.WAITING, "no execution lock")
        async with runtime.session() as session, session.begin():
            sequence = await self.orders.journal.next_sequence(
                session, signal_id=position.signal_id, role=Role.LIST
            )
            list_id = client_id(position.signal_id, Role.LIST, sequence)
            above = client_id(position.signal_id, Role.TAKE_PROFIT, sequence)
            below = client_id(position.signal_id, Role.STOP, sequence)
            await self.orders.journal.prepare(
                session,
                context=self._context(),
                symbol=position.symbol,
                signal_id=position.signal_id,
                position_id=position.id,
                role=Role.LIST,
                sequence=sequence,
                cid=list_id,
                list_client_order_id=list_id,
                request={
                    "quantity": qty,
                    "take_profit": tp,
                    "stop": stop,
                    "stop_limit": limit,
                    "above": above,
                    "below": below,
                },
            )
        trade = self._trade()
        try:
            await self.orders.journal.send(
                list_id,
                lambda: trade.order_list_oco(
                    symbol=position.symbol,
                    quantity=qty,
                    take_profit=tp,
                    stop=stop,
                    stop_limit_price=limit,
                    list_client_order_id=list_id,
                    above_client_order_id=above,
                    below_client_order_id=below,
                ),
            )
        except TradeCallError as error:
            if error.error_type == NOT_LEADER:
                return StepResult(Step.WAITING, "no execution lock")
            if error.outcome is Outcome.REJECTED and _insufficient(error):
                # Something else holds the asset — maybe an OCO we lost track
                # of. Never a proof of protection: reconcile decides.
                return StepResult(Step.WAITING, "insufficient balance; reconcile")
            if (
                error.outcome is Outcome.REJECTED
                and error.exchange_code in DETERMINISTIC_CODES
            ):
                self.orders.filters.invalidate(position.network, position.symbol)
                return await self._exit(
                    position.id, ExitReason.EMERGENCY, ExitMechanism.MARKET_UNPROTECTED
                )
            return None  # UNKNOWN or throttled: the next round decides
        await self._mark_protected(
            position.id, stop=stop, take_profit=tp, list_id=list_id
        )
        return StepResult(Step.PROTECTED)

    async def _resolve_list(
        self, position: Position, attempt: OrderAttempt
    ) -> StepResult | None:
        """Settle an OCO attempt of unproven outcome before anything else."""
        trade = self._trade()
        cid = attempt.client_order_id
        lookup = await self.orders.journal.lookup(
            cid,
            lambda: trade.get_order_list(
                list_client_order_id=cid, priority=Priority.PROTECT
            ),
        )
        if lookup.found is Found.ABSENT:
            await self.orders.journal.resolve(cid, AttemptState.RESOLVED)
            return None
        if lookup.found is Found.UNKNOWN:
            self.orders._escalate(attempt)
            await self.check_deadline(position.id)
            return StepResult(Step.WAITING, "OCO outcome unknown")
        return await self._apply_list(position, attempt, lookup.data, resolve=True)

    async def _apply_answered(
        self, position: Position, attempt: OrderAttempt
    ) -> StepResult:
        """Act on an OCO answer that was recorded but never applied.

        The exchange accepted the list; a crash before it was associated left
        it untracked, and it may be live and hold the asset. It is read again
        and applied exactly like a list found for an UNKNOWN attempt. A miss
        now is lag, never absence: the position waits (and its deadline runs).
        """
        trade = self._trade()
        cid = attempt.client_order_id
        try:
            order_list = (
                await trade.get_order_list(
                    list_client_order_id=cid, priority=Priority.PROTECT
                )
            ).data
        except TradeCallError as error:
            self.orders._escalate(attempt)
            await self.check_deadline(position.id)
            return StepResult(
                Step.WAITING, f"answered list {cid} not readable: {error.message}"
            )
        logger.warning("OCO %s answered but not applied; applying it", cid)
        return await self._apply_list(position, attempt, order_list, resolve=False)

    async def _apply_list(
        self,
        position: Position,
        attempt: OrderAttempt,
        order_list: Any,
        *,
        resolve: bool,
    ) -> StepResult:
        """Make a list the exchange has the position's protection, or, if it
        already finished, prove its legs first. ``resolve`` also records the
        outcome of an attempt that was UNKNOWN."""
        cid = attempt.client_order_id
        if str(order_list.list_order_status) != "EXECUTING":
            # The list finished (a leg filled, or it was cancelled) before we
            # knew of it. The attempt blocks every new list until both legs
            # are terminal and fully booked.
            proof = await self.prove_legs(position.id, order_list)
            if not proof.proven:
                await self.check_deadline(position.id)
                return StepResult(Step.WAITING, proof.reason)
            if resolve:
                await self.orders.journal.resolve(
                    cid, AttemptState.RESOLVED, data=order_list
                )
            async with self.runtime.session() as session, session.begin():
                await self.orders.journal.mark_applied(session, cid)
            return await self._after_list(position.id, proof.filled_type)
        if resolve:
            await self.orders.journal.resolve(
                cid, AttemptState.RESOLVED, data=order_list
            )
        request = attempt.request
        await self._mark_protected(
            position.id,
            stop=Decimal(request["stop"]),
            take_profit=Decimal(request["take_profit"]),
            list_id=cid,
        )
        return StepResult(Step.PROTECTED)

    async def prove_legs(self, position_id: uuid.UUID, order_list: Any) -> LegsProof:
        """Both legs of a finished list terminal and fully in the ledger.

        Each leg is read from the exchange (``ALL_DONE`` of the list says
        nothing about how much a leg sold), its trades are booked, and the
        booked quantity must equal its ``executedQty``, commissions included.
        Until then no new order may be sized from the remainder.
        """
        position = await self._position(position_id)
        trade = self._trade()
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
                return LegsProof(False, f"leg not readable: {exc.message}")
            if str(order.status) not in TERMINAL:
                return LegsProof(
                    False, f"leg {leg.client_order_id} still {order.status}"
                )
            if order.order_id is None:
                return LegsProof(False, "leg without an order id")
            executed[order.order_id] = Decimal(order.executed_qty or "0")
        filled_type = await self._book_orders(
            position_id, [order_id for order_id, qty in executed.items() if qty > 0]
        )
        async with self.runtime.session() as session:
            locked = await lock_position(session, position_id)
            proven = await self.orders.applier.proven(session, locked, executed)
        if not proven:
            return LegsProof(False, "leg fills not proven yet")
        return LegsProof(True, "", filled_type)

    async def _after_list(
        self, position_id: uuid.UUID, filled_type: str | None
    ) -> StepResult:
        """A finished, proven list: closed if it sold everything."""
        position = await self._position(position_id)
        if await self.nothing_to_sell(position):
            above = filled_type == "LIMIT_MAKER"
            return await self._finalize(
                position_id,
                ExitReason.TP if above else ExitReason.STOP,
                ExitMechanism.EXCHANGE_TP if above else ExitMechanism.EXCHANGE_STOP,
            )
        return StepResult(Step.WAITING, "list finished with a remainder")

    async def nothing_to_sell(self, position: Position) -> bool:
        """Whether the booked remainder is zero or dust (plan §8.4).

        Zero needs no market data. Otherwise the symbol's rules decide, at the
        freshest price there is — the fill price when no quote can be had, so
        a quiet tape never keeps a sold-out position open.
        """
        if position.quantity <= 0:
            return True
        try:
            filters = await self.orders.filters.get(position.network, position.symbol)
        except Exception:  # noqa: BLE001 - no rules: cannot call it dust
            return False
        _, quote = await self._market(position, strict=False)
        price = quote.last_price() if quote is not None else position.fill_vwap
        return price is not None and not sellable(filters, position.quantity, price)

    # --- deadline -------------------------------------------------------------------

    def _deadline(self, position: Position) -> datetime:
        assert position.unprotected_since is not None
        return position.unprotected_since + timedelta(
            seconds=self.runtime.settings.unprotected_deadline_s
        )

    async def check_deadline(self, position_id: uuid.UUID) -> bool:
        """Mark the position overdue once its deadline passed; True if overdue.

        Covers every unfinished operation that took the list down: a close
        (cancel → barrier → exit) as much as a reprice (plan §6.7). The
        operation and its state are kept: overdue only alerts and closes
        entries, it never lets an unproven remainder be sold.
        """
        async with self.runtime.session() as session, session.begin():
            position = await lock_position(session, position_id)
            if position.status not in (
                PositionStatus.PROTECTING,
                PositionStatus.UNPROTECTED,
                PositionStatus.REPRICING,
                PositionStatus.CLOSING,
            ):
                return position.status == PositionStatus.PROTECTION_OVERDUE
            if (
                position.unprotected_since is None
                or self.orders.clock() < self._deadline(position)
            ):
                return False
            position.status = PositionStatus.PROTECTION_OVERDUE
            await record_event(
                session,
                account=position.account,
                network=position.network,
                kind="protection_overdue",
                payload={"since": position.unprotected_since},
                position=position,
            )
        self.runtime.alerts[f"overdue:{position.symbol}"] = (
            f"{position.symbol} unprotected past "
            f"{self.runtime.settings.unprotected_deadline_s}s; reconciling"
        )
        logger.error("%s protection overdue", position.symbol)
        return True

    async def _mark_protected(
        self,
        position_id: uuid.UUID,
        *,
        stop: Decimal,
        take_profit: Decimal,
        list_id: str,
    ) -> None:
        async with self.runtime.session() as session, session.begin():
            position = await lock_position(session, position_id)
            position.status = PositionStatus.PROTECTED
            position.exchange_stop = stop
            if position.desired_stop is None or position.desired_stop < stop:
                position.desired_stop = stop
            position.take_profit = take_profit
            position.protection_list_id = list_id
            position.unprotected_since = None
            position.operation = None
            await self.orders.journal.mark_applied(session, list_id)
            await record_event(
                session,
                account=position.account,
                network=position.network,
                kind="protected",
                payload={"list": list_id, "stop": stop, "take_profit": take_profit},
                position=position,
            )
        self.runtime.alerts.pop(f"overdue:{position.symbol}", None)
        logger.info("%s protected stop=%s tp=%s", position.symbol, stop, take_profit)

    # --- market exits ---------------------------------------------------------------

    async def exit_market(
        self, position_id: uuid.UUID, reason: ExitReason, mechanism: ExitMechanism
    ) -> StepResult:
        async with self.orders.lock_for(position_id):
            return await self._exit(position_id, reason, mechanism)

    async def _exit(
        self, position_id: uuid.UUID, reason: ExitReason, mechanism: ExitMechanism
    ) -> StepResult:
        for _ in range(5):
            position = await self._position(position_id)
            if position.status == PositionStatus.CLOSED:
                return StepResult(Step.CLOSED)
            if not self.runtime.may_mutate():
                return StepResult(Step.WAITING, "no execution lock")
            for attempt in await self._open_attempts(position, (Role.EXIT,)):
                if not await self._resolve_exit(position, attempt):
                    return StepResult(Step.WAITING, "exit outcome unknown")
            if await self._unapplied_lists(position) or await self._open_attempts(
                position, (Role.LIST,)
            ):
                # A list that may be live holds the asset: a sale would be
                # refused, or sell what the list still might. Settle it first.
                return StepResult(Step.WAITING, "a protection list is unsettled")
            if not await self._proven(position_id):
                return StepResult(Step.WAITING, "fills not proven")
            position = await self._position(position_id)
            filters, quote = await self._market(position, strict=False)
            if filters is None or quote is None:
                return StepResult(Step.WAITING, "market data unavailable")
            qty = round_down(position.quantity, filters.step)
            if not sellable(filters, qty, quote.last_price()):
                return await self._finalize(position_id, reason, mechanism)
            # Getting out must not wait for a busy tape: an older reference is
            # fine for a market sell, which the exchange validates anyway.
            params = replace(
                self.runtime.settings.sizing(), max_trade_age=EXIT_QUOTE_AGE
            )
            problems = validate_market_sell(
                filters, quote, self.orders.clock(), params, qty=qty
            )
            if problems:
                return StepResult(Step.WAITING, problems[0].detail)
            sold = await self._sell(position, qty, reason)
            if sold is not None:
                return sold
        return StepResult(Step.WAITING, "exit still pending")

    async def _sell(
        self, position: Position, qty: Decimal, reason: ExitReason
    ) -> StepResult | None:
        runtime = self.runtime
        if not await runtime.confirm_lead():
            return StepResult(Step.WAITING, "no execution lock")
        async with runtime.session() as session, session.begin():
            sequence = await self.orders.journal.next_sequence(
                session, signal_id=position.signal_id, role=Role.EXIT
            )
            attempt = await self.orders.journal.prepare(
                session,
                context=self._context(),
                symbol=position.symbol,
                signal_id=position.signal_id,
                position_id=position.id,
                role=Role.EXIT,
                sequence=sequence,
                request={
                    "side": "SELL",
                    "type": "MARKET",
                    "quantity": qty,
                    "reason": reason,
                },
            )
            cid = attempt.client_order_id
        trade = self._trade()
        try:
            result = await self.orders.journal.send(
                cid,
                lambda: trade.new_order(
                    symbol=position.symbol,
                    side="SELL",
                    order_type="MARKET",
                    quantity=qty,
                    client_order_id=cid,
                    priority=Priority.PROTECT,
                ),
            )
        except TradeCallError as error:
            if error.outcome is Outcome.UNKNOWN:
                return StepResult(Step.WAITING, "exit outcome unknown")
            if error.error_type == NOT_LEADER:
                return StepResult(Step.WAITING, "no execution lock")
            if _insufficient(error):
                return StepResult(Step.WAITING, "insufficient balance; reconcile")
            return StepResult(Step.WAITING, f"exit refused: {error.message}")
        order = result.data
        await self._book_response(position, order, cid)
        return None  # loop: finalize or sell the confirmed remainder

    async def _resolve_exit(self, position: Position, attempt: OrderAttempt) -> bool:
        trade = self._trade()
        cid = attempt.client_order_id
        lookup = await self.orders.journal.lookup(
            cid,
            lambda: trade.get_order(
                symbol=position.symbol, client_order_id=cid, priority=Priority.PROTECT
            ),
        )
        if lookup.found is Found.UNKNOWN:
            self.orders._escalate(attempt)
            return False
        if lookup.found is Found.ABSENT:
            await self.orders.journal.resolve(cid, AttemptState.RESOLVED)
            return True
        await self.orders.journal.resolve(cid, AttemptState.RESOLVED, data=lookup.data)
        await self._book_orders(position.id, [int(lookup.data.order_id)])
        return True

    async def _book_response(self, position: Position, order: Any, cid: str) -> None:
        when = transaction_time(order, self.orders.clock())
        fills = [
            FillRecord.from_order_fill(
                fill,
                symbol=position.symbol,
                order_id=int(order.order_id),
                side="SELL",
                time=when,
                client_order_id=cid,
            )
            for fill in getattr(order, "fills", None) or []
        ]
        async with self.runtime.session() as session, session.begin():
            locked = await lock_position(session, position.id)
            await self.orders.applier.apply(session, locked, fills)

    async def _book_orders(
        self, position_id: uuid.UUID, order_ids: list[int]
    ) -> str | None:
        """Book every trade of these orders from myTrades.

        Returns the type of the last of our list legs that had trades, so a
        finished list tells whether the take-profit or the stop sold it.
        """
        trade = self._trade()
        position = await self._position(position_id)
        fills: list[FillRecord] = []
        filled_type: str | None = None
        for order_id in order_ids:
            rows = (
                await trade.my_trades(
                    symbol=position.symbol, order_id=order_id, priority=Priority.PROTECT
                )
            ).data
            fills.extend(FillRecord.from_my_trade(row) for row in rows)
            if rows:
                filled_type = await self._order_type(position, order_id) or filled_type
        async with self.runtime.session() as session, session.begin():
            locked = await lock_position(session, position_id)
            result = await self.orders.applier.apply(session, locked, fills)
            if result.retimed:
                await self.finalizer.retime(session, locked)
        return filled_type

    async def _order_type(self, position: Position, order_id: int) -> str | None:
        """The type of one of our list legs, from the attempt that placed it."""
        async with self.runtime.session() as session:
            attempts = (
                await session.scalars(
                    select(OrderAttempt).where(
                        OrderAttempt.position_id == position.id,
                        OrderAttempt.role == Role.LIST.value,
                    )
                )
            ).all()
        for attempt in attempts:
            for report in (attempt.response or {}).get("orderReports") or []:
                if report.get("orderId") == order_id:
                    return str(report.get("type"))
            for order in (attempt.response or {}).get("orders") or []:
                if order.get("orderId") == order_id:
                    cid = str(order.get("clientOrderId"))
                    return "LIMIT_MAKER" if "-pt" in cid else "STOP_LOSS"
        return None

    async def _proven(self, position_id: uuid.UUID) -> bool:
        """Every terminal order of the position is fully in the ledger."""
        position = await self._position(position_id)
        async with self.runtime.session() as session:
            attempts = (
                await session.scalars(
                    select(OrderAttempt).where(
                        OrderAttempt.signal_id == position.signal_id,
                        OrderAttempt.order_id.is_not(None),
                        OrderAttempt.executed_qty.is_not(None),
                        OrderAttempt.role.in_([Role.ENTRY.value, Role.EXIT.value]),
                    )
                )
            ).all()
        executed = {
            int(a.order_id): a.executed_qty
            for a in attempts
            if a.order_id is not None
            and a.executed_qty is not None
            and (a.exchange_status in TERMINAL)
        }
        async with self.runtime.session() as session:
            locked = await lock_position(session, position_id)
            if await self.orders.applier.proven(session, locked, executed):
                return True
        await self._book_orders(position_id, list(executed))
        async with self.runtime.session() as session:
            locked = await lock_position(session, position_id)
            return await self.orders.applier.proven(session, locked, executed)

    # --- closing ----------------------------------------------------------------------

    async def _close_dust(self, position_id: uuid.UUID) -> StepResult:
        return await self._finalize(
            position_id, ExitReason.EMERGENCY, ExitMechanism.DUST
        )

    async def _finalize(
        self, position_id: uuid.UUID, reason: ExitReason, mechanism: ExitMechanism
    ) -> StepResult:
        async with self.runtime.session() as session, session.begin():
            position = await lock_position(session, position_id)
            attempts = (
                await session.scalars(
                    select(OrderAttempt.client_order_id).where(
                        OrderAttempt.position_id == position_id
                    )
                )
            ).all()
            outcome = await self.finalizer.close(
                session,
                position,
                reason=reason,
                mechanism=mechanism,
                order_ids=attempts,
            )
        if outcome is not None:
            self.runtime.alerts.pop(f"overdue:{outcome.symbol}", None)
            policy = ExitPolicy.from_transport(outcome.exit_policy)
            await self.runtime.notify(
                PositionEvent(
                    event_id=f"{position_id}:closed",
                    symbol=outcome.symbol,
                    position_id=str(position_id),
                    at=outcome.closed_at,
                    cooldown_until=outcome.closed_at
                    + timedelta(minutes=policy.cooldown_minutes),
                ),
                closed=True,
            )
            logger.info(
                "%s closed reason=%s mechanism=%s pnl=%s",
                outcome.symbol,
                reason.value,
                mechanism.value,
                outcome.realized_pnl,
            )
        return StepResult(Step.CLOSED)

    # --- reads --------------------------------------------------------------------------

    def _trade(self) -> TradeClient:
        trade = self.runtime.trade
        if trade is None:
            raise RuntimeError("no trade client")
        return trade

    def _context(self) -> ExecutionContext:
        context = self.runtime.context
        if context is None:
            raise RuntimeError("no execution context")
        return context

    async def _position(self, position_id: uuid.UUID) -> Position:
        async with self.runtime.session() as session:
            position = await session.get(Position, position_id)
            assert position is not None
            return position

    async def _open_attempts(
        self, position: Position, roles: tuple[Role, ...]
    ) -> list[OrderAttempt]:
        async with self.runtime.session() as session:
            return await self.orders.journal.open_of(
                session, signal_id=position.signal_id, roles=roles
            )

    async def _unapplied_lists(self, position: Position) -> list[OrderAttempt]:
        async with self.runtime.session() as session:
            return await self.orders.journal.unapplied_lists(
                session, signal_id=position.signal_id
            )

    async def _market(
        self, position: Position, *, strict: bool = True
    ) -> tuple[SymbolFilters | None, QuoteSnapshot | None]:
        """Rules and a quote; ``strict`` also requires a recent last trade."""
        try:
            filters = await self.orders.filters.get(position.network, position.symbol)
            quote = await self.orders.quotes(position.network).snapshot(position.symbol)
            if strict:
                quote.check_fresh(self.orders.clock())
            else:
                quote.check_fresh(self.orders.clock(), max_trade_age=EXIT_QUOTE_AGE)
        except Exception as exc:  # noqa: BLE001 - no data: no decision
            logger.warning("market data for %s unavailable: %s", position.symbol, exc)
            return None, None
        return filters, quote


__all__ = ["ATTEMPT_OFFSETS", "Protector", "Step", "StepResult"]
