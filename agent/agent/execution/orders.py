"""OrderManager: from a reserved ENTRY to a protected position (plan §6.1–6.3).

EXEC-08b — the entry. A reserved intent gets one last check right before the
send (the admission check can be minutes old), then one ``LIMIT FOK`` BUY at
the cap price. The answer decides everything:

* ``FILLED`` → the applier opens the position from the fills; the fills in the
  response are only one source — the position's quantity counts as proven
  when the booked fills add up to the order's ``executedQty`` (reading
  ``myTrades`` for that order when the response had none or too few);
* ``EXPIRED`` with nothing executed, or a proven refusal → the intent is
  released: quote, symbol and slot come back in the same transaction;
* anything else → UNKNOWN: the intent keeps everything, nothing is re-sent,
  and :meth:`OrderManager.resolve_entries` finds the order later.

Nothing is sent without the execution lock and a finished reconcile
(:meth:`ExecutionRuntime.may_mutate`).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent.exchange.budget import Priority
from agent.exchange.trade import Outcome, TradeCallError, TradeResult
from agent.execution.admission import release_intent
from agent.execution.attempts import AttemptJournal, Found, Role
from agent.execution.filters import FilterCache
from agent.execution.ledger import FillRecord, PositionApplier, lock_position
from agent.execution.models import (
    AttemptState,
    ExecutionIntent,
    IntentState,
    OrderAttempt,
    Position,
    SignalInbox,
)
from agent.execution.quotes import QuoteProvider, QuoteSnapshot, ReferenceUnverified
from agent.execution.runtime import ExecutionRuntime
from agent.execution.sizing import validate_entry
from agent.strategies.base import ExitPolicy, PositionEvent

logger = logging.getLogger(__name__)

#: Clock difference tolerated between our attempt time and a trade's time.
_TRADE_TIME_SLACK_MS = 5_000
#: ``error_type`` of the refusal raised when the send deadline has passed.
_EXPIRED = "Expired"


def _expired(signal: SignalInbox, now: datetime) -> bool:
    return signal.valid_until is not None and now >= signal.valid_until


class EntryOutcome(StrEnum):
    FILLED = "filled"
    RELEASED = "released"
    UNKNOWN = "unknown"
    DEFERRED = "deferred"  # not sent: no lock yet, try again later


@dataclass(frozen=True)
class EntryResult:
    outcome: EntryOutcome
    reason: str | None = None
    position_id: uuid.UUID | None = None


def _utcnow() -> datetime:
    return datetime.now(UTC)


class OrderManager:
    def __init__(
        self,
        runtime: ExecutionRuntime,
        *,
        applier: PositionApplier,
        filters: FilterCache,
        quotes: Callable[[str], QuoteProvider],
        clock: Callable[[], datetime] = _utcnow,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self.runtime = runtime
        self.applier = applier
        self.filters = filters
        self.quotes = quotes
        self.clock = clock
        self.sleep = sleep
        self.journal = AttemptJournal(runtime.session, clock=clock)
        self._locks: dict[uuid.UUID, asyncio.Lock] = {}
        #: Called with a position id once its entry is filled and booked.
        self.on_filled: Callable[[uuid.UUID], Awaitable[None]] | None = None

    def lock_for(self, key: uuid.UUID) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    # --- the entry ------------------------------------------------------------------

    async def execute_entry(self, signal_id: uuid.UUID) -> EntryResult:
        async with self.lock_for(signal_id):
            return await self._execute_entry(signal_id)

    async def _execute_entry(self, signal_id: uuid.UUID) -> EntryResult:
        runtime = self.runtime
        async with runtime.session() as session:
            intent = await session.get(ExecutionIntent, signal_id)
            signal = await session.get(SignalInbox, signal_id)
        if intent is None or signal is None or intent.state != IntentState.RESERVED:
            return EntryResult(EntryOutcome.DEFERRED, "not reserved")
        if not runtime.may_mutate() or runtime.trade is None:
            return EntryResult(EntryOutcome.DEFERRED, "no execution lock")
        reason, deadline, quote = await self._presubmit(intent, signal)
        if reason is not None:
            await self._release(signal_id, reason)
            return EntryResult(EntryOutcome.RELEASED, reason)
        async with runtime.session() as session, session.begin():
            locked = await session.get(ExecutionIntent, signal_id, with_for_update=True)
            assert locked is not None
            if locked.state != IntentState.RESERVED:
                return EntryResult(EntryOutcome.DEFERRED, "not reserved")
            sequence = await self.journal.next_sequence(
                session, signal_id=signal_id, role=Role.ENTRY
            )
            attempt = await self.journal.prepare(
                session,
                context=_context(runtime),
                symbol=locked.symbol,
                signal_id=signal_id,
                role=Role.ENTRY,
                sequence=sequence,
                request={
                    "side": "BUY",
                    "type": "LIMIT",
                    "timeInForce": "FOK",
                    "quantity": locked.quantity,
                    "price": locked.cap_price,
                    # Audit: the quote the send was checked against.
                    "quote": quote.as_record() if quote is not None else None,
                },
            )
            locked.state = IntentState.SUBMITTED
            cid = attempt.client_order_id
        trade = runtime.trade

        async def place() -> TradeResult[Any]:
            # Last look at the clock: the journal writes above took time, and
            # the budget may make the request wait; neither may carry the BUY
            # past the signal's validity or the quote's freshness.
            left = (deadline - self.clock()).total_seconds()
            if left <= 0:
                raise TradeCallError(
                    Outcome.REJECTED, "expired before send", error_type=_EXPIRED
                )
            return await trade.new_order(
                symbol=intent.symbol,
                side="BUY",
                order_type="LIMIT",
                quantity=intent.quantity,
                price=intent.cap_price,
                time_in_force="FOK",
                client_order_id=cid,
                priority=Priority.ENTRY,
                max_wait=left,
            )

        try:
            result = await self.journal.send(cid, place)
        except TradeCallError as error:
            if error.outcome is Outcome.UNKNOWN:
                await self._mark_unknown(signal_id)
                return EntryResult(EntryOutcome.UNKNOWN, error.message)
            if error.error_type == _EXPIRED:
                await self._release(signal_id, "expired")
                return EntryResult(EntryOutcome.RELEASED, "expired")
            if error.outcome is Outcome.REJECTED:
                self.filters.invalidate(intent.network, intent.symbol)
            reason = f"entry_{error.outcome.value}:{error.exchange_code}"
            await self._release(signal_id, reason)
            return EntryResult(EntryOutcome.RELEASED, reason)
        except asyncio.CancelledError:
            await asyncio.shield(self._mark_unknown(signal_id))
            raise
        return await self._settle(signal_id, cid, result.data, detected_at=self.clock())

    async def _presubmit(
        self, intent: ExecutionIntent, signal: SignalInbox
    ) -> tuple[str | None, datetime, QuoteSnapshot | None]:
        """The checks right before the BUY (plan §6.1).

        Returns a reason to release (or ``None``), the moment after which the
        BUY must not be sent — the signal's ``valid_until`` or the quote's
        freshness limit, whichever is first — and the quote checked. Time is
        read after the awaited reads, never before them.
        """
        runtime = self.runtime
        settings = runtime.settings
        max_fetch_age = timedelta(seconds=settings.quote_max_fetch_age_s)
        blocked = runtime.admission_block()
        if blocked is not None:
            return blocked, self.clock(), None
        if _expired(signal, self.clock()):
            return "expired", self.clock(), None
        policy = ExitPolicy.from_transport(signal.payload["exit_policy"])
        try:
            quote = await self.quotes(intent.network).snapshot(intent.symbol)
            filters = await self.filters.get(intent.network, intent.symbol)
            now = self.clock()
            if _expired(signal, now):
                return "expired", now, quote
            quote.check_fresh(
                now,
                max_fetch_age=max_fetch_age,
                max_trade_age=timedelta(seconds=settings.quote_max_trade_age_s),
            )
            if quote.last_price() <= policy.stop_ref:
                return "stop_above_price", now, quote
        except ReferenceUnverified as exc:
            return f"quote_unverified: {exc}", self.clock(), None
        except Exception as exc:  # noqa: BLE001 - any read failure: do not buy
            return f"market_data_unavailable: {exc}", self.clock(), None
        assert intent.quantity is not None and intent.cap_price is not None
        problems = validate_entry(
            filters,
            quote,
            now,
            settings.sizing(),
            qty=intent.quantity,
            price=intent.cap_price,
        )
        if problems:
            return f"{problems[0].reason.value}: {problems[0].detail}", now, quote
        deadline = quote.fetched_at + max_fetch_age
        if signal.valid_until is not None:
            deadline = min(deadline, signal.valid_until)
        return None, deadline, quote

    # --- outcomes ------------------------------------------------------------------

    async def _release(self, signal_id: uuid.UUID, reason: str) -> None:
        async with self.runtime.session() as session, session.begin():
            intent = await session.get(ExecutionIntent, signal_id, with_for_update=True)
            assert intent is not None
            await release_intent(session, intent, reason=reason)
        logger.info("ENTRY %s released: %s", signal_id, reason)

    async def _mark_unknown(self, signal_id: uuid.UUID) -> None:
        async with self.runtime.session() as session, session.begin():
            intent = await session.get(ExecutionIntent, signal_id, with_for_update=True)
            if intent is not None and intent.state == IntentState.SUBMITTED:
                intent.state = IntentState.UNKNOWN
        logger.warning("ENTRY %s outcome UNKNOWN; resources held", signal_id)

    async def _settle(
        self, signal_id: uuid.UUID, cid: str, order: Any, *, detected_at: datetime
    ) -> EntryResult:
        """Act on a known order state of the entry."""
        status = str(getattr(order, "status", None))
        executed = Decimal(getattr(order, "executed_qty", None) or "0")
        if status == "FILLED" or executed > 0:
            position_id = await self._book_entry(signal_id, cid, order, detected_at)
            return EntryResult(EntryOutcome.FILLED, position_id=position_id)
        if status in ("EXPIRED", "REJECTED", "CANCELED", "EXPIRED_IN_MATCH"):
            await self._release(signal_id, f"fok_{status.lower()}")
            return EntryResult(EntryOutcome.RELEASED, f"fok_{status.lower()}")
        await self._mark_unknown(signal_id)
        return EntryResult(EntryOutcome.UNKNOWN, f"status {status}")

    async def _book_entry(
        self, signal_id: uuid.UUID, cid: str, order: Any, detected_at: datetime
    ) -> uuid.UUID:
        """Open the position from the order's fills, then prove its quantity."""
        async with self.runtime.session() as session:
            signal = await session.get(SignalInbox, signal_id)
        assert signal is not None
        policy = ExitPolicy.from_transport(signal.payload["exit_policy"])
        order_id = int(order.order_id)
        when = (
            datetime.fromtimestamp(order.transact_time / 1000, UTC)
            if getattr(order, "transact_time", None)
            else detected_at
        )
        fills = [
            FillRecord.from_order_fill(
                fill,
                symbol=signal.symbol,
                order_id=order_id,
                side="BUY",
                time=when,
                client_order_id=cid,
            )
            for fill in getattr(order, "fills", None) or []
        ]
        async with self.runtime.session() as session, session.begin():
            intent = await session.get(ExecutionIntent, signal_id, with_for_update=True)
            assert intent is not None
            opened = intent.position_id is None
            result = await self.applier.open_from_entry(
                session,
                intent,
                exit_policy=policy,
                fills=fills,
                detected_at=detected_at,
            )
            position = result.position
        await self.prove_entry(position.id)
        if opened:
            await self.runtime.notify(
                PositionEvent(
                    event_id=f"{position.id}:opened",
                    symbol=position.symbol,
                    position_id=str(position.id),
                    at=detected_at,
                ),
                closed=False,
            )
        if self.on_filled is not None:
            await self.on_filled(position.id)
        return position.id

    async def prove_entry(self, position_id: uuid.UUID) -> bool:
        """Book any fills of the entry order the ledger is missing.

        True when the booked BUY fills add up to the order's ``executedQty``.
        """
        async with self.runtime.session() as session:
            position = await session.get(Position, position_id)
            assert position is not None
            attempt = (
                await session.scalars(
                    select(OrderAttempt).where(
                        OrderAttempt.signal_id == position.signal_id,
                        OrderAttempt.role == Role.ENTRY.value,
                        OrderAttempt.order_id.is_not(None),
                    )
                )
            ).first()
        if attempt is None or attempt.order_id is None or attempt.executed_qty is None:
            return False
        executed = {attempt.order_id: attempt.executed_qty}
        async with self.runtime.session() as session:
            position = await lock_position(session, position_id)
            if await self.applier.proven(session, position, executed):
                return True
        trade = self.runtime.trade
        if trade is None:
            return False
        try:
            rows = (
                await trade.my_trades(
                    symbol=position.symbol,
                    order_id=attempt.order_id,
                    priority=Priority.PROTECT,
                )
            ).data
        except TradeCallError as exc:
            logger.warning("cannot read the entry's trades: %s", exc)
            return False
        async with self.runtime.session() as session, session.begin():
            position = await lock_position(session, position_id)
            await self.applier.apply(
                session, position, [FillRecord.from_my_trade(row) for row in rows]
            )
            return await self.applier.proven(session, position, executed)

    # --- UNKNOWN entries -------------------------------------------------------------

    async def resolve_entries(self) -> list[EntryResult]:
        """Find the outcome of every UNKNOWN (or orphaned prepared) entry."""
        runtime = self.runtime
        context = runtime.context
        if context is None or runtime.trade is None:
            return []
        async with runtime.session() as session:
            rows = (
                await session.scalars(
                    select(ExecutionIntent).where(
                        ExecutionIntent.account == context.account,
                        ExecutionIntent.network == context.network,
                        ExecutionIntent.state.in_(
                            [IntentState.UNKNOWN, IntentState.SUBMITTED]
                        ),
                    )
                )
            ).all()
        results = []
        for intent in rows:
            async with self.lock_for(intent.signal_id):
                results.append(await self._resolve_entry(intent))
        return results

    async def _resolve_entry(self, intent: ExecutionIntent) -> EntryResult:
        trade = self.runtime.trade
        assert trade is not None
        async with self.runtime.session() as session:
            attempts = await self.journal.open_of(
                session, signal_id=intent.signal_id, roles=(Role.ENTRY,)
            )
            answered = None if attempts else await self._answered(session, intent)
        if not attempts:
            if answered is None:
                return EntryResult(EntryOutcome.DEFERRED, "no open attempt")
            return await self._finish_answered(intent, answered)
        attempt = attempts[-1]
        cid = attempt.client_order_id
        lookup = await self.journal.lookup(
            cid,
            lambda: trade.get_order(
                symbol=intent.symbol, client_order_id=cid, priority=Priority.RECONCILE
            ),
        )
        if lookup.found is Found.ABSENT and await self._unexplained_buy(
            intent, attempt
        ):
            # Not found by id, but a BUY nobody accounts for filled since the
            # attempt: an accepted order may be invisible to queries for a
            # while, and recvWindow does not bound that. Stay UNKNOWN.
            self._escalate(attempt)
            return EntryResult(EntryOutcome.UNKNOWN, "not found; unexplained BUY")
        if lookup.found is Found.ABSENT:
            await self.journal.resolve(cid, AttemptState.RESOLVED)
            await self._release(intent.signal_id, "entry_not_placed")
            return EntryResult(EntryOutcome.RELEASED, "entry_not_placed")
        if lookup.found is Found.UNKNOWN:
            self._escalate(attempt)
            return EntryResult(EntryOutcome.UNKNOWN, "still unknown")
        order = lookup.data
        await self.journal.resolve(cid, AttemptState.RESOLVED, data=order)
        return await self._settle(
            intent.signal_id, cid, order, detected_at=self.clock()
        )

    async def _answered(
        self, session: AsyncSession, intent: ExecutionIntent
    ) -> OrderAttempt | None:
        """The last entry attempt with a recorded answer, for an unsettled intent.

        The answer (``acked``, or ``resolved`` from an UNKNOWN) is committed
        before it is acted on; a crash in between leaves the intent SUBMITTED
        or UNKNOWN with no open attempt.
        """
        return (
            await session.scalars(
                select(OrderAttempt)
                .where(
                    OrderAttempt.signal_id == intent.signal_id,
                    OrderAttempt.role == Role.ENTRY.value,
                    OrderAttempt.state.in_([AttemptState.ACKED, AttemptState.RESOLVED]),
                )
                .order_by(OrderAttempt.created_at.desc())
            )
        ).first()

    async def _finish_answered(
        self, intent: ExecutionIntent, attempt: OrderAttempt
    ) -> EntryResult:
        """Act on an entry answer that was recorded but never applied."""
        cid = attempt.client_order_id
        if attempt.state == AttemptState.RESOLVED and attempt.order_id is None:
            # Resolved as proven absent; only the release was lost.
            await self._release(intent.signal_id, "entry_not_placed")
            return EntryResult(EntryOutcome.RELEASED, "entry_not_placed")
        trade = self.runtime.trade
        assert trade is not None
        try:
            order = (
                await trade.get_order(
                    symbol=intent.symbol,
                    client_order_id=cid,
                    priority=Priority.RECONCILE,
                )
            ).data
        except TradeCallError as error:
            # The exchange answered once; a miss now is lag, never absence.
            self._escalate(attempt)
            return EntryResult(EntryOutcome.UNKNOWN, f"answered; {error.message}")
        logger.warning("ENTRY %s answered but not applied; settling", cid)
        return await self._settle(
            intent.signal_id, cid, order, detected_at=self.clock()
        )

    async def _unexplained_buy(
        self, intent: ExecutionIntent, attempt: OrderAttempt
    ) -> bool:
        """Whether a BUY of an order we cannot name filled since ``attempt``.

        Any doubt (the history cannot be read) counts as yes.
        """
        trade = self.runtime.trade
        assert trade is not None
        try:
            trades = (
                await trade.my_trades(symbol=intent.symbol, priority=Priority.RECONCILE)
            ).data
        except TradeCallError:
            return True
        async with self.runtime.session() as session:
            known = set(
                (
                    await session.scalars(
                        select(OrderAttempt.order_id).where(
                            OrderAttempt.account == attempt.account,
                            OrderAttempt.network == attempt.network,
                            OrderAttempt.symbol == intent.symbol,
                            OrderAttempt.order_id.is_not(None),
                        )
                    )
                ).all()
            )
        since = int(attempt.created_at.timestamp() * 1000) - _TRADE_TIME_SLACK_MS
        return any(
            t.is_buyer and t.order_id not in known and (t.time or 0) >= since
            for t in trades
        )

    def _escalate(self, attempt: OrderAttempt) -> None:
        age = self.clock() - attempt.created_at
        if age >= timedelta(seconds=self.runtime.settings.unknown_escalate_s):
            self.runtime.alerts["unknown"] = (
                f"order {attempt.client_order_id} ({attempt.symbol}) outcome unknown "
                f"for {int(age.total_seconds())}s; not retried"
            )


def _context(runtime: ExecutionRuntime) -> Any:
    if runtime.context is None:
        raise RuntimeError("no execution context")
    return runtime.context


__all__ = ["EntryOutcome", "EntryResult", "OrderManager"]
