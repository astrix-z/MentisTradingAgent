"""Durable inbox and admission (plan §4.1, §5, §6.1, §7.4–7.5; R7, R8).

**Inbox.** Every signal the strategy emits is committed to ``signal_inbox``
before anything else sees it; that commit *is* a successful emit. An ENTRY
carries its deterministic id (plan §5), so a replay of a consumed epoch — a
restart re-deciding the same bar — hits the unique index and is recognised as
already recorded instead of being admitted twice. HOLD and EXIT are kept for
the audit only.

**Admission** turns an ENTRY into an intent. The decision is recorded either
way (the reason is the audit), and resources are taken only when the entry
may go ahead: quote reserve, symbol and slot, in **one transaction**, under a
transaction-level advisory lock of the context so that two admissions never
look at the same free slot or the same free USDT. Network reads (account,
quotes, filters) happen before that transaction and are re-checked inside it
only through database state.

**Intent outcomes** (plan §7.5) are written here too, since the intent has one
writer: :func:`release_intent` for a proven zero-fill or reject frees quote,
symbol and slot together with the outcome. A filled entry is taken over by the
position applier (EXEC-15), and an UNKNOWN keeps everything held.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from agent.exchange.rest import ExchangeError
from agent.exchange.trade import TradeCallError
from agent.execution.filters import FilterCache
from agent.execution.ledger import losing_trades_today, record_event
from agent.execution.lock import lock_key
from agent.execution.models import (
    INTENT_HOLDING_STATES,
    ExecutionIntent,
    IntentState,
    Position,
    PositionStatus,
    SignalInbox,
    SymbolCooldown,
)
from agent.execution.quotes import QuoteProvider, QuoteSnapshot, ReferenceUnverified
from agent.execution.runtime import ExecutionContext, ExecutionRuntime
from agent.execution.sizing import EntryPlan, EntryRejected, Skip, equity, plan_entry
from agent.strategies.base import ExitPolicy, Signal

logger = logging.getLogger(__name__)

POLL_SECONDS = 5.0

#: Position states in which protection is not confirmed; entries stay closed.
#: ``protecting`` counts too: the first seconds after a fill are unprotected.
UNSAFE_STATES = (
    PositionStatus.PROTECTING,
    PositionStatus.UNPROTECTED,
    PositionStatus.PROTECTION_OVERDUE,
    PositionStatus.UNKNOWN,
)


async def unprotected_positions(
    session: AsyncSession, context: ExecutionContext
) -> int:
    """Open positions whose protection the exchange does not confirm now.

    Besides :data:`UNSAFE_STATES`: a reprice or a close takes the list down
    before its replacement or sale is proven — ``unprotected_since`` is set
    while the status still reads ``repricing``/``closing`` (review 2026-10-05).
    """
    count = await session.scalar(
        select(func.count()).where(
            Position.account == context.account,
            Position.network == context.network,
            or_(
                Position.status.in_(list(UNSAFE_STATES)),
                and_(
                    Position.status != PositionStatus.CLOSED,
                    Position.unprotected_since.is_not(None),
                ),
            ),
        )
    )
    return int(count or 0)


def admission_lock_key(context: ExecutionContext) -> int:
    """Transaction lock for reservations; distinct from the leader lock."""
    return lock_key(context.account, f"{context.network}|admission")


# --- inbox -----------------------------------------------------------------------


async def record_signal(
    session: AsyncSession,
    signal: Signal,
    *,
    context: ExecutionContext,
    strategy: str,
    with_it: Callable[[AsyncSession], Awaitable[None]] | None = None,
) -> bool:
    """Commit ``signal`` to the inbox; ``False`` if this ENTRY was already there.

    ``with_it`` writes in the same transaction, only when the signal is new:
    what must exist exactly when the signal does (the tracker's watch).
    """
    payload = signal.transport()
    inserted = await session.execute(
        insert(SignalInbox)
        .values(
            signal_id=uuid.UUID(signal.signal_id),
            account=context.account,
            network=context.network,
            strategy=strategy,
            symbol=signal.symbol,
            type=signal.type,
            epoch_id=signal.epoch_id,
            emitted_at=signal.emitted_at,
            valid_until=signal.valid_until,
            payload=payload,
            received_at=datetime.now(UTC),
            # Only an ENTRY is admitted; the rest is audit.
            processed_at=None if signal.type == "ENTRY" else datetime.now(UTC),
        )
        .on_conflict_do_nothing()
        .returning(SignalInbox.signal_id)
    )
    recorded = inserted.scalar_one_or_none() is not None
    if recorded and with_it is not None:
        await with_it(session)
    await session.commit()
    return recorded


# --- intent outcomes ----------------------------------------------------------------


async def release_intent(
    session: AsyncSession, intent: ExecutionIntent, *, reason: str
) -> bool:
    """A proven zero-fill or reject: free quote, symbol and slot (plan §7.5).

    Runs in the caller's transaction with ``intent`` locked. Idempotent: an
    intent already resolved is left alone and ``False`` returned.
    """
    if intent.state not in INTENT_HOLDING_STATES:
        return False
    intent.state = IntentState.NO_FILL
    intent.reason = reason
    released = intent.quote_reserved
    intent.quote_reserved = Decimal(0)
    intent.resolved_at = datetime.now(UTC)
    await record_event(
        session,
        account=intent.account,
        network=intent.network,
        kind="intent_released",
        payload={
            "signal_id": str(intent.signal_id),
            "symbol": intent.symbol,
            "reason": reason,
            "quote_released": released,
        },
    )
    return True


# --- admission ----------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    signal_id: uuid.UUID
    state: IntentState
    reason: str | None
    plan: EntryPlan | None = None


class Admission:
    """Consumes ENTRY signals from the inbox and decides on each."""

    def __init__(
        self,
        runtime: ExecutionRuntime,
        *,
        quotes: Callable[[str], QuoteProvider],
        filters: FilterCache,
        prices: Callable[[str, str], Awaitable[Decimal | None]],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._runtime = runtime
        self._quotes = quotes
        self._filters = filters
        self._prices = prices
        self._clock = clock
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        #: Called with each reserved intent's signal id (the order manager).
        self.on_reserved: Callable[[uuid.UUID], Awaitable[None]] | None = None

    def wake(self) -> None:
        self._wake.set()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=POLL_SECONDS)
            except TimeoutError:
                pass
            self._wake.clear()
            try:
                await self.drain()
            except Exception:  # noqa: BLE001 - the worker must survive
                logger.exception("admission round failed")

    async def drain(self) -> list[Decision]:
        """Decide every ENTRY still waiting in the inbox of the context."""
        context = self._runtime.context
        if context is None:
            return []
        async with self._lock:
            async with self._runtime.session() as session:
                pending = (
                    await session.scalars(
                        select(SignalInbox)
                        .where(
                            SignalInbox.account == context.account,
                            SignalInbox.network == context.network,
                            SignalInbox.type == "ENTRY",
                            SignalInbox.processed_at.is_(None),
                        )
                        .order_by(SignalInbox.received_at)
                    )
                ).all()
            decisions = []
            for signal in pending:
                decision = await self.decide(signal, context)
                decisions.append(decision)
                if decision.state == IntentState.RESERVED and self.on_reserved:
                    await self.on_reserved(decision.signal_id)
            return decisions

    async def decide(self, signal: SignalInbox, context: ExecutionContext) -> Decision:
        now = self._clock()
        if signal.valid_until is not None and now >= signal.valid_until:
            return await self._record(signal, context, IntentState.EXPIRED, "expired")
        blocked = self._runtime.admission_block()
        if blocked is not None:
            return await self._record(signal, context, IntentState.REJECTED, blocked)
        reason = await self._database_block(signal, context, now)
        if reason is not None:
            if reason == "daily_loss_limit":
                self._runtime.alerts["daily_limit"] = (
                    f"{self._runtime.settings.daily_losing_trades_limit} losing trades "
                    "today; entries paused until 00:00 UTC"
                )
            return await self._record(signal, context, IntentState.REJECTED, reason)
        self._runtime.alerts.pop("daily_limit", None)
        try:
            plan, free, quote = await self._plan(signal, context, now)
        except EntryRejected as exc:
            return await self._record(
                signal,
                context,
                IntentState.REJECTED,
                exc.reason.value,
                detail=exc.detail,
            )
        except ReferenceUnverified as exc:
            return await self._record(
                signal,
                context,
                IntentState.REJECTED,
                "quote_unverified",
                detail=str(exc),
            )
        except (ExchangeError, TradeCallError, LookupError) as exc:
            return await self._record(
                signal,
                context,
                IntentState.REJECTED,
                "market_data_unavailable",
                detail=str(exc),
            )
        # Planning awaited the exchange; the signal may have run out meanwhile.
        now = self._clock()
        if signal.valid_until is not None and now >= signal.valid_until:
            return await self._record(signal, context, IntentState.EXPIRED, "expired")
        return await self._reserve(signal, context, plan, free, now, quote)

    # --- checks -----------------------------------------------------------------

    async def _database_block(
        self, signal: SignalInbox, context: ExecutionContext, now: datetime
    ) -> str | None:
        async with self._runtime.session() as session:
            return await risk_block(
                session,
                context,
                signal.symbol,
                now,
                limit=self._runtime.settings.daily_losing_trades_limit,
            )

    async def _plan(
        self, signal: SignalInbox, context: ExecutionContext, now: datetime
    ) -> tuple[EntryPlan, Decimal, QuoteSnapshot]:
        """Size the entry from fresh exchange data.

        Returns the plan, the free quote balance and the quote it was sized on.
        """
        payload = signal.payload
        policy = ExitPolicy.from_transport(payload["exit_policy"])
        entry_ref = Decimal(payload["entry_ref"])
        settings = self._runtime.settings
        quote = await self._quotes(context.network).snapshot(signal.symbol)
        quote.check_fresh(
            now,
            max_fetch_age=_seconds(settings.quote_max_fetch_age_s),
            max_trade_age=_seconds(settings.quote_max_trade_age_s),
        )
        last = quote.last_price()
        if last <= policy.stop_ref:
            raise EntryRejected(
                Skip.STOP_ABOVE_PRICE, f"last trade {last} ≤ stop_ref {policy.stop_ref}"
            )
        filters = await self._filters.get(context.network, signal.symbol)
        account = await self._account()
        async with self._runtime.session() as session:
            occupied, reserved, positions = await _occupancy(session, context)
        values = []
        for symbol, qty in positions:
            price = await self._prices(context.network, symbol)
            if price is None:
                raise LookupError(f"no price for open position {symbol}")
            values.append((qty, price))
        free, locked = _balance(account, filters.quote_asset)
        return (
            plan_entry(
                filters=filters,
                quote=quote,
                now=now,
                params=settings.sizing(),
                entry_ref=entry_ref,
                policy=policy,
                total_equity=equity(free, locked, values),
                free_quote=free,
                reserved_quote=reserved,
                occupied_slots=occupied,
                account_permissions=frozenset(account.permissions or []),
            ),
            free,
            quote,
        )

    async def _account(self) -> Any:
        trade = self._runtime.trade
        if trade is None:
            raise LookupError("no trade client")
        return (await trade.get_account()).data

    # --- writing ----------------------------------------------------------------------

    async def _record(
        self,
        signal: SignalInbox,
        context: ExecutionContext,
        state: IntentState,
        reason: str,
        *,
        detail: str | None = None,
    ) -> Decision:
        async with self._runtime.session() as session, session.begin():
            await _consume(session, signal.signal_id)
            session.add(
                ExecutionIntent(
                    signal_id=signal.signal_id,
                    account=context.account,
                    network=context.network,
                    strategy=signal.strategy,
                    symbol=signal.symbol,
                    state=state,
                    reason=reason,
                    plan={"detail": detail} if detail else None,
                    quote_reserved=Decimal(0),
                    resolved_at=self._clock(),
                )
            )
            await record_event(
                session,
                account=context.account,
                network=context.network,
                kind="intent_decided",
                payload={
                    "signal_id": str(signal.signal_id),
                    "symbol": signal.symbol,
                    "state": state.value,
                    "reason": reason,
                    "detail": detail,
                },
            )
        logger.info(
            "ENTRY %s %s not executed: %s%s",
            signal.symbol,
            signal.signal_id,
            reason,
            f" ({detail})" if detail else "",
        )
        return Decision(signal.signal_id, state, reason)

    async def _reserve(
        self,
        signal: SignalInbox,
        context: ExecutionContext,
        plan: EntryPlan,
        free: Decimal,
        now: datetime,
        quote: QuoteSnapshot,
    ) -> Decision:
        """Take quote, symbol and slot, re-checking occupancy under the lock.

        ``free`` is the quote balance read before the transaction; inside it
        only database state is re-read, so no exchange call holds the lock.
        """
        # The plan is stored with the quote it was sized on (spread included).
        record = {**plan.as_record(), "quote": quote.as_record()}
        async with self._runtime.session() as session, session.begin():
            await session.execute(
                text("select pg_advisory_xact_lock(:key)"),
                {"key": admission_lock_key(context)},
            )
            reason = await _symbol_block(session, context, signal.symbol, now)
            occupied, reserved, _ = await _occupancy(session, context)
            if reason is None and occupied >= plan.slots.slots:
                reason = "slots_full"
            if reason is None and plan.quote_cost > free - reserved:
                reason = "insufficient_balance"
            await _consume(session, signal.signal_id)
            state = IntentState.RESERVED if reason is None else IntentState.REJECTED
            session.add(
                ExecutionIntent(
                    signal_id=signal.signal_id,
                    account=context.account,
                    network=context.network,
                    strategy=signal.strategy,
                    symbol=signal.symbol,
                    state=state,
                    reason=reason,
                    plan=record,
                    quantity=plan.quantity,
                    cap_price=plan.cap_price,
                    quote_reserved=plan.quote_cost if reason is None else Decimal(0),
                    resolved_at=None if reason is None else now,
                )
            )
            await record_event(
                session,
                account=context.account,
                network=context.network,
                kind="intent_decided",
                payload={
                    "signal_id": str(signal.signal_id),
                    "symbol": signal.symbol,
                    "state": state.value,
                    "reason": reason,
                    "plan": record,
                },
            )
        if reason is None:
            logger.info(
                "ENTRY %s reserved qty=%s cap=%s cost=%s",
                signal.symbol,
                plan.quantity,
                plan.cap_price,
                plan.quote_cost,
            )
        return Decision(
            signal.signal_id, state, reason, plan if reason is None else None
        )


async def _consume(session: AsyncSession, signal_id: uuid.UUID) -> None:
    signal = await session.get(SignalInbox, signal_id, with_for_update=True)
    if signal is None or signal.processed_at is not None:
        raise RuntimeError(f"signal {signal_id} is already decided")
    signal.processed_at = datetime.now(UTC)


async def risk_block(
    session: AsyncSession,
    context: ExecutionContext,
    symbol: str,
    now: datetime,
    *,
    limit: int,
    own: uuid.UUID | None = None,
) -> str | None:
    """Why the database forbids a new ENTRY of ``symbol`` now, or ``None``.

    Asked at admission and again right before an already reserved BUY is sent
    (``own`` is that intent): a reservation is no licence — a fifth losing
    trade, another entry gone UNKNOWN or a list taken down since then closes
    it as well (review 2026-10-05, F1).
    """
    if (
        await losing_trades_today(session, context.account, context.network, now)
        >= limit
    ):
        return "daily_loss_limit"
    if await unprotected_positions(session, context):
        return "unprotected_position"
    unknown = select(func.count()).where(
        ExecutionIntent.account == context.account,
        ExecutionIntent.network == context.network,
        ExecutionIntent.state == IntentState.UNKNOWN,
    )
    if own is not None:
        unknown = unknown.where(ExecutionIntent.signal_id != own)
    if await session.scalar(unknown):
        return "unknown_entry"
    return await _symbol_block(session, context, symbol, now, own=own)


async def _symbol_block(
    session: AsyncSession,
    context: ExecutionContext,
    symbol: str,
    now: datetime,
    *,
    own: uuid.UUID | None = None,
) -> str | None:
    open_position = await session.scalar(
        select(func.count()).where(
            Position.account == context.account,
            Position.network == context.network,
            Position.symbol == symbol,
            Position.status != PositionStatus.CLOSED,
        )
    )
    if open_position:
        return "position_open"
    holding = select(func.count()).where(
        ExecutionIntent.account == context.account,
        ExecutionIntent.network == context.network,
        ExecutionIntent.symbol == symbol,
        ExecutionIntent.state.in_(list(INTENT_HOLDING_STATES)),
    )
    if own is not None:
        holding = holding.where(ExecutionIntent.signal_id != own)
    if await session.scalar(holding):
        return "entry_pending"
    cooldown = await session.scalar(
        select(SymbolCooldown.cooldown_until).where(
            SymbolCooldown.account == context.account,
            SymbolCooldown.network == context.network,
            SymbolCooldown.symbol == symbol,
        )
    )
    if cooldown is not None and now < cooldown:
        return "cooldown"
    return None


async def _occupancy(
    session: AsyncSession, context: ExecutionContext
) -> tuple[int, Decimal, list[tuple[str, Decimal]]]:
    """Slots in use, quote held by entries, and open positions' quantities."""
    positions = (
        await session.execute(
            select(Position.symbol, Position.quantity).where(
                Position.account == context.account,
                Position.network == context.network,
                Position.status != PositionStatus.CLOSED,
            )
        )
    ).all()
    holding = (
        await session.execute(
            select(
                func.count(), func.coalesce(func.sum(ExecutionIntent.quote_reserved), 0)
            ).where(
                ExecutionIntent.account == context.account,
                ExecutionIntent.network == context.network,
                ExecutionIntent.state.in_(list(INTENT_HOLDING_STATES)),
            )
        )
    ).one()
    count, reserved = int(holding[0]), Decimal(holding[1])
    return len(positions) + count, reserved, [(s, q) for s, q in positions]


def _balance(account: Any, asset: str) -> tuple[Decimal, Decimal]:
    for row in account.balances or []:
        if row.asset == asset:
            return Decimal(row.free or "0"), Decimal(row.locked or "0")
    return Decimal(0), Decimal(0)


def _seconds(value: int) -> timedelta:
    return timedelta(seconds=value)


__all__ = [
    "Admission",
    "Decision",
    "UNSAFE_STATES",
    "admission_lock_key",
    "record_signal",
    "release_intent",
]
