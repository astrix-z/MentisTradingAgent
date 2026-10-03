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

An entry kept UNKNOWN only because some BUY of an order we cannot name filled
after it (a manual purchase, say) is released by the operator's
``release_entry`` command — after the agent proves for itself that the
order is absent (:meth:`OrderManager.release_entry`).
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

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent.exchange.budget import Priority
from agent.exchange.trade import RECV_WINDOW_MS, Outcome, TradeCallError, TradeResult
from agent.execution.admission import release_intent
from agent.execution.attempts import (
    ABSENCE_GRACE,
    NOT_FOUND_CODES,
    AttemptJournal,
    Found,
    Role,
)
from agent.execution.filters import FilterCache
from agent.execution.ledger import FillRecord, PositionApplier, lock_position
from agent.execution.models import (
    AttemptState,
    ExecutionIntent,
    IntentState,
    OrderAttempt,
    Position,
    PositionStatus,
    SignalInbox,
)
from agent.execution.quotes import QuoteProvider, QuoteSnapshot, ReferenceUnverified
from agent.execution.runtime import ExecutionRuntime
from agent.execution.sizing import validate_entry
from agent.strategies.base import ExitPolicy, PositionEvent

logger = logging.getLogger(__name__)

#: Clock difference tolerated between our attempt time and a trade's time.
_TRADE_TIME_SLACK_MS = 5_000
#: Share of an entry's quantity a fill adds to the balance at least (a
#: commission paid in the base asset takes ≤0.1%); more than this held
#: unexplained means the entry may have filled.
_FILL_SHARE = Decimal("0.99")
#: ``error_type`` of the refusal raised when the send deadline has passed.
_EXPIRED = "Expired"


def _expired(signal: SignalInbox, now: datetime) -> bool:
    return signal.valid_until is not None and now >= signal.valid_until


class EntryOutcome(StrEnum):
    FILLED = "filled"
    RELEASED = "released"
    UNKNOWN = "unknown"
    DEFERRED = "deferred"  # not sent: no lock yet, try again later


class ReleaseRefused(Exception):
    """``release_entry`` did not release: the order's absence is not proven."""


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

        def in_time() -> None:
            if self.clock() >= deadline:
                raise TradeCallError(
                    Outcome.REJECTED, "expired before send", error_type=_EXPIRED
                )

        async def place() -> TradeResult[Any]:
            # Last looks at the clock: the journal writes above took time, and
            # the budget may make the request wait; neither may carry the BUY
            # past the signal's validity or any freshness limit of the quote.
            # The second look is after the budget wait, right before the send.
            in_time()
            left = (deadline - self.clock()).total_seconds()
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
                before_send=in_time,
            )

        try:
            result = await self.journal.send(cid, place)
        except TradeCallError as error:
            if error.outcome is Outcome.UNKNOWN:
                await self._mark_unknown(signal_id)
                return EntryResult(EntryOutcome.UNKNOWN, error.message)
            if error.outcome is Outcome.REJECTED and error.error_type != _EXPIRED:
                self.filters.invalidate(intent.network, intent.symbol)
            reason = _refusal(error.as_record())
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
        BUY must not be sent — the signal's ``valid_until`` or the first of the
        quote's freshness limits (its fetch and the last trade/reference it
        carries) — and the quote checked. Time is read after the awaited reads,
        never before them.
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
        deadline = quote.usable_until(
            max_fetch_age=max_fetch_age,
            max_trade_age=timedelta(seconds=settings.quote_max_trade_age_s),
        )
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

        The answer (``acked``, ``rejected``, or ``resolved`` from an UNKNOWN)
        is committed before it is acted on; a crash in between leaves the
        intent SUBMITTED or UNKNOWN with no open attempt.
        """
        return (
            await session.scalars(
                select(OrderAttempt)
                .where(
                    OrderAttempt.signal_id == intent.signal_id,
                    OrderAttempt.role == Role.ENTRY.value,
                    OrderAttempt.state.in_(
                        [
                            AttemptState.ACKED,
                            AttemptState.REJECTED,
                            AttemptState.RESOLVED,
                        ]
                    ),
                )
                .order_by(OrderAttempt.created_at.desc())
            )
        ).first()

    async def _finish_answered(
        self, intent: ExecutionIntent, attempt: OrderAttempt
    ) -> EntryResult:
        """Act on an entry answer that was recorded but never applied."""
        cid = attempt.client_order_id
        if attempt.state == AttemptState.REJECTED:
            # The exchange provably did not act; only the release was lost.
            reason = _refusal(attempt.error)
            await self._release(intent.signal_id, reason)
            return EntryResult(EntryOutcome.RELEASED, reason)
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
        """Whether the entry may have filled although its order is not found.

        Yes when a BUY of an order we cannot name filled since ``attempt``, or
        the balance holds as much as the entry would have bought beyond what
        positions and the visible trades explain. Any doubt (the history or
        the balance cannot be read) counts as yes.
        """
        strangers = await self._strangers(intent, attempt)
        if strangers is None or any(t.is_buyer for t in strangers):
            return True
        return await self._balance_doubt(intent, strangers) is not None

    async def _strangers(
        self, intent: ExecutionIntent, attempt: OrderAttempt
    ) -> list[Any] | None:
        """The trades of orders we cannot name since ``attempt``, both sides.

        The whole history from the attempt's time on, every page, so a trade
        is not missed because many came after it. ``None`` when the history
        cannot be read. A trade the exchange does not show yet is not here
        either — :meth:`_balance_doubt` is what covers that lag.
        """
        trade = self.runtime.trade
        assert trade is not None
        since = int(attempt.created_at.timestamp() * 1000) - _TRADE_TIME_SLACK_MS
        try:
            trades = await trade.my_trades_from_time(
                symbol=intent.symbol, start_ms=since, priority=Priority.RECONCILE
            )
        except TradeCallError:
            return None
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
        return [t for t in trades if t.order_id not in known and (t.time or 0) >= since]

    async def _balance_doubt(
        self, intent: ExecutionIntent, strangers: list[Any]
    ) -> str | None:
        """Why the balance cannot rule out that the entry filled, or ``None``.

        A filled entry adds its quantity (less a commission paid in the base
        asset) to the balance, whether or not its trade is visible yet. The
        base asset held beyond the open positions of the symbol and the net
        of the ``strangers`` trades must be less than that.
        """
        trade = self.runtime.trade
        context = self.runtime.context
        assert trade is not None and context is not None
        assert intent.quantity is not None
        base = self.applier.base_of(intent.symbol)
        try:
            account = (await trade.get_account()).data
        except TradeCallError as error:
            return f"cannot read balances: {error.message}"
        total = sum(
            (
                Decimal(row.free or "0") + Decimal(row.locked or "0")
                for row in account.balances or []
                if row.asset == base
            ),
            Decimal(0),
        )
        async with self.runtime.session() as session:
            held = (
                await session.scalar(
                    select(func.coalesce(func.sum(Position.quantity), 0)).where(
                        Position.account == context.account,
                        Position.network == context.network,
                        Position.symbol == intent.symbol,
                        Position.status != PositionStatus.CLOSED,
                    )
                )
            ) or Decimal(0)
        net = sum(
            (Decimal(t.qty) if t.is_buyer else -Decimal(t.qty) for t in strangers),
            Decimal(0),
        )
        unexplained = total - Decimal(held) - net
        if unexplained >= intent.quantity * _FILL_SHARE:
            return (
                f"{unexplained} {base} held beyond positions and visible trades; "
                f"the BUY of {intent.quantity} may have filled"
            )
        return None

    # --- the operator's release ---------------------------------------------------

    async def release_entry(self, client_order_id: str) -> EntryResult:
        """The ``release_entry`` command: release an entry proven never placed.

        For an open entry attempt of the current context only. The agent
        proves absence itself, whatever the operator saw: the request is older
        than ``recvWindow`` plus a margin, the exchange does not know the
        client id, every BUY of an order we cannot name since the attempt
        (the whole history, every page) belongs to an order with someone
        else's client id, and the balance does not hold the entry's quantity
        beyond what positions and those trades explain — a trade the exchange
        does not show yet still shows in the balance. Any doubt raises
        :class:`ReleaseRefused` and changes nothing.
        """
        runtime = self.runtime
        context = runtime.context
        if context is None or runtime.trade is None or not runtime.may_mutate():
            raise ReleaseRefused("no execution lock")
        async with runtime.session() as session:
            attempt = await session.get(OrderAttempt, client_order_id)
        if (
            attempt is None
            or attempt.role != Role.ENTRY.value
            or (attempt.account, attempt.network) != (context.account, context.network)
        ):
            raise ReleaseRefused(f"no entry attempt {client_order_id} here")
        signal_id = attempt.signal_id
        async with self.lock_for(signal_id):
            async with runtime.session() as session:
                attempt = await session.get(OrderAttempt, client_order_id)
                intent = await session.get(ExecutionIntent, signal_id)
            assert attempt is not None and intent is not None
            if attempt.state not in (
                AttemptState.PREPARED,
                AttemptState.SENT,
                AttemptState.UNKNOWN,
            ) or intent.state not in (IntentState.SUBMITTED, IntentState.UNKNOWN):
                raise ReleaseRefused(
                    f"attempt {attempt.state}, intent {intent.state}: nothing to release"
                )
            await self._prove_absent(intent, attempt)
            await self.journal.resolve(client_order_id, AttemptState.RESOLVED)
            await self._release(intent.signal_id, "released_by_operator")
        alert = runtime.alerts.get("unknown")
        if alert is not None and client_order_id in alert:
            del runtime.alerts["unknown"]
        logger.warning("ENTRY %s released by the operator", client_order_id)
        return EntryResult(EntryOutcome.RELEASED, "released_by_operator")

    async def _prove_absent(
        self, intent: ExecutionIntent, attempt: OrderAttempt
    ) -> None:
        """Raise :class:`ReleaseRefused` unless ``attempt``'s order is proven absent."""
        trade = self.runtime.trade
        assert trade is not None
        cid = attempt.client_order_id
        window = timedelta(milliseconds=RECV_WINDOW_MS) + ABSENCE_GRACE
        wait = attempt.created_at + window - self.clock()
        if wait > timedelta(0):
            raise ReleaseRefused(
                f"too recent; retry in {int(wait.total_seconds()) + 1}s"
            )
        try:
            order = (
                await trade.get_order(
                    symbol=intent.symbol,
                    client_order_id=cid,
                    priority=Priority.RECONCILE,
                )
            ).data
        except TradeCallError as error:
            if error.exchange_code not in NOT_FOUND_CODES:
                raise ReleaseRefused(f"cannot query {cid}: {error.message}") from error
        else:
            raise ReleaseRefused(
                f"{cid} is on the exchange ({getattr(order, 'status', None)}); "
                "the agent settles it"
            )
        strangers = await self._strangers(intent, attempt)
        if strangers is None:
            raise ReleaseRefused(f"cannot read {intent.symbol} trades")
        buys = [t for t in strangers if t.is_buyer]
        for order_id in sorted({t.order_id for t in buys}):
            try:
                other = (
                    await trade.get_order(
                        symbol=intent.symbol,
                        order_id=order_id,
                        priority=Priority.RECONCILE,
                    )
                ).data
            except TradeCallError as error:
                raise ReleaseRefused(
                    f"cannot identify order {order_id}: {error.message}"
                ) from error
            owner = str(getattr(other, "client_order_id", None) or "")
            if not owner or owner == cid or owner.startswith("mta-"):
                raise ReleaseRefused(
                    f"BUY order {order_id} ({owner or 'no client id'}) may be ours"
                )
        doubt = await self._balance_doubt(intent, strangers)
        if doubt is not None:
            raise ReleaseRefused(doubt)

    def _escalate(self, attempt: OrderAttempt) -> None:
        age = self.clock() - attempt.created_at
        if age >= timedelta(seconds=self.runtime.settings.unknown_escalate_s):
            self.runtime.alerts["unknown"] = (
                f"order {attempt.client_order_id} ({attempt.symbol}) outcome unknown "
                f"for {int(age.total_seconds())}s; not retried"
            )


def _refusal(error: dict[str, Any] | None) -> str:
    """The release reason of a refused entry, from its recorded error."""
    error = error or {}
    if error.get("error_type") == _EXPIRED:
        return "expired"
    return f"entry_{error.get('outcome')}:{error.get('exchange_code')}"


def _context(runtime: ExecutionRuntime) -> Any:
    if runtime.context is None:
        raise RuntimeError("no execution context")
    return runtime.context


__all__ = ["EntryOutcome", "EntryResult", "OrderManager", "ReleaseRefused"]
