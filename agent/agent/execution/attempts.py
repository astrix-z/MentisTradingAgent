"""Order attempts: ids, the journal before the send, UNKNOWN (plan §6.8).

Every exchange mutation follows one protocol:

1. **prepare** — the attempt (client id, role, request) is committed *before*
   anything is sent. After a restart a ``prepared`` attempt is treated as
   possibly sent: the process may have died between the send and the commit.
2. **send** — through the trade client; the answer is classified there.
3. **record** — ``acked`` with the SDK's own status, ``rejected`` when the
   exchange provably did not act (a refusal, 429, 418), otherwise ``unknown``.
   A cancelled wait is ``unknown`` too: the request keeps running in its
   worker thread.

**UNKNOWN blocks the logical operation**, not just a client id: no BUY,
emergency SELL or OCO of the same purpose is sent under *any* id until the
outcome is proven. Resolution asks the exchange by client id. A single
not-found proves nothing — a new order may take a moment to become visible.
Absence is accepted only when the order is still not found at least twice and
the request's ``recvWindow`` plus a margin has passed since it was prepared: the
exchange rejects a request older than its ``recvWindow``, so an unprocessed one
cannot appear after that. That does not cover a request the exchange already
accepted whose order is not yet visible to queries, so callers whose mistake
would cost money add their own evidence (an entry also requires that no BUY of
an order we cannot name filled since the attempt, see ``orders.py``). Until
then the attempt stays UNKNOWN; after ``unknown_escalate_s`` it is escalated
(heartbeat), and it is never retried blindly.

Client ids are ``mta-<24 hex of the signal id>-<role><n>``, at most 36
characters; a counter that would not fit is refused, never truncated.
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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent.exchange.trade import (
    MAX_CLIENT_ID,
    RECV_WINDOW_MS,
    Outcome,
    TradeCallError,
    TradeResult,
)
from agent.execution.ledger import record_event
from agent.execution.models import AttemptState, OrderAttempt
from agent.execution.runtime import ExecutionContext

logger = logging.getLogger(__name__)

#: Binance "no such order" answers to a query or a cancel.
NOT_FOUND_CODES = frozenset({-2013, -2011})
#: Margin after ``recvWindow`` before a missing order counts as absent.
ABSENCE_GRACE = timedelta(seconds=60)
MIN_MISSES = 2


class Role(StrEnum):
    ENTRY = "e"
    LIST = "pl"
    TAKE_PROFIT = "pt"
    STOP = "ps"
    EXIT = "x"


class ClientIdOverflow(ValueError):
    """The counter no longer fits a 36-character client id."""


def client_id(signal_id: uuid.UUID, role: Role, sequence: int) -> str:
    if sequence < 1:
        raise ValueError("attempt counters start at 1")
    value = f"mta-{signal_id.hex[:24]}-{role.value}{sequence}"
    if len(value) > MAX_CLIENT_ID:
        raise ClientIdOverflow(f"{value!r} is longer than {MAX_CLIENT_ID}")
    return value


class Found(StrEnum):
    PRESENT = "present"
    ABSENT = "absent"  # proven: it can no longer appear
    UNKNOWN = "unknown"  # not found yet, not proven either


@dataclass(frozen=True)
class Lookup:
    found: Found
    data: Any = None  # the SDK model of the order or list when present


def _utcnow() -> datetime:
    return datetime.now(UTC)


class AttemptJournal:
    """Writes attempts and runs the send-and-record protocol."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession] | Callable[[], AsyncSession],
        *,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._sessions = sessions
        self._clock = clock

    async def prepare(
        self,
        session: AsyncSession,
        *,
        context: ExecutionContext,
        symbol: str,
        signal_id: uuid.UUID,
        role: Role,
        sequence: int,
        request: dict[str, Any],
        position_id: uuid.UUID | None = None,
        list_client_order_id: str | None = None,
        cid: str | None = None,
    ) -> OrderAttempt:
        """Add a ``prepared`` attempt to the caller's transaction."""
        attempt = OrderAttempt(
            client_order_id=cid or client_id(signal_id, role, sequence),
            account=context.account,
            network=context.network,
            symbol=symbol,
            signal_id=signal_id,
            position_id=position_id,
            role=role.value,
            sequence=sequence,
            list_client_order_id=list_client_order_id,
            request=_jsonable(request),
            state=AttemptState.PREPARED,
            created_at=self._clock(),
        )
        session.add(attempt)
        return attempt

    async def send[T](
        self, attempt_id: str, call: Callable[[], Awaitable[TradeResult[T]]]
    ) -> TradeResult[T]:
        """Send and record the answer; raises :class:`TradeCallError` as-is.

        The record is written even if the waiting coroutine is cancelled: the
        request may still be executing, so the attempt becomes UNKNOWN.
        """
        await self._mark(attempt_id, sent=True)
        try:
            result = await call()
        except TradeCallError as error:
            state = (
                AttemptState.UNKNOWN
                if error.outcome is Outcome.UNKNOWN
                else AttemptState.REJECTED
            )
            await _shielded(self._record(attempt_id, state, error=error.as_record()))
            raise
        except asyncio.CancelledError:
            await _shielded(
                self._record(
                    attempt_id,
                    AttemptState.UNKNOWN,
                    error={"outcome": "unknown", "message": "wait cancelled"},
                )
            )
            raise
        await _shielded(self._record_answer(attempt_id, result))
        return result

    async def _mark(self, attempt_id: str, *, sent: bool) -> None:
        async with self._sessions() as session:
            attempt = await session.get(OrderAttempt, attempt_id, with_for_update=True)
            assert attempt is not None
            if sent and attempt.state == AttemptState.PREPARED:
                attempt.state = AttemptState.SENT
                attempt.sent_at = self._clock()
            await session.commit()

    async def _record(
        self, attempt_id: str, state: AttemptState, *, error: dict[str, Any]
    ) -> None:
        async with self._sessions() as session:
            attempt = await session.get(OrderAttempt, attempt_id, with_for_update=True)
            assert attempt is not None
            attempt.state = state
            attempt.error = error
            attempt.answered_at = self._clock()
            await record_event(
                session,
                account=attempt.account,
                network=attempt.network,
                kind=f"attempt_{state.value}",
                payload={"client_order_id": attempt_id, "error": error},
            )
            await session.commit()

    async def _record_answer(self, attempt_id: str, result: TradeResult[Any]) -> None:
        data = result.data
        async with self._sessions() as session:
            attempt = await session.get(OrderAttempt, attempt_id, with_for_update=True)
            assert attempt is not None
            attempt.state = AttemptState.ACKED
            attempt.answered_at = self._clock()
            attempt.exchange_status = _status(data)
            attempt.order_id = getattr(data, "order_id", None)
            attempt.order_list_id = getattr(data, "order_list_id", None)
            executed = getattr(data, "executed_qty", None)
            attempt.executed_qty = Decimal(executed) if executed is not None else None
            attempt.response = _model_dict(data)
            await record_event(
                session,
                account=attempt.account,
                network=attempt.network,
                kind="attempt_acked",
                payload={
                    "client_order_id": attempt_id,
                    "status": attempt.exchange_status,
                },
            )
            await session.commit()

    # --- UNKNOWN -------------------------------------------------------------------

    async def lookup(
        self,
        attempt_id: str,
        query: Callable[[], Awaitable[TradeResult[Any]]],
    ) -> Lookup:
        """Ask the exchange about an attempt; prove absence only per the rule above."""
        try:
            result = await query()
        except TradeCallError as error:
            if error.exchange_code not in NOT_FOUND_CODES:
                return Lookup(Found.UNKNOWN)
            return await self._missed(attempt_id)
        return Lookup(Found.PRESENT, result.data)

    async def _missed(self, attempt_id: str) -> Lookup:
        now = self._clock()
        async with self._sessions() as session:
            attempt = await session.get(OrderAttempt, attempt_id, with_for_update=True)
            assert attempt is not None
            misses = dict(attempt.error or {}).get("not_found", 0) + 1
            attempt.error = {**(attempt.error or {}), "not_found": misses}
            await session.commit()
            window = timedelta(milliseconds=RECV_WINDOW_MS) + ABSENCE_GRACE
            if misses >= MIN_MISSES and now >= attempt.created_at + window:
                return Lookup(Found.ABSENT)
        return Lookup(Found.UNKNOWN)

    async def resolve(
        self, attempt_id: str, state: AttemptState, *, data: Any = None
    ) -> None:
        """Record the proven outcome of an UNKNOWN (or prepared) attempt."""
        async with self._sessions() as session:
            attempt = await session.get(OrderAttempt, attempt_id, with_for_update=True)
            assert attempt is not None
            attempt.state = state
            attempt.resolved_at = self._clock()
            if data is not None:
                attempt.exchange_status = _status(data)
                attempt.order_id = getattr(data, "order_id", attempt.order_id)
                attempt.order_list_id = getattr(
                    data, "order_list_id", attempt.order_list_id
                )
                executed = getattr(data, "executed_qty", None)
                if executed is not None:
                    attempt.executed_qty = Decimal(executed)
                attempt.response = _model_dict(data)
            await record_event(
                session,
                account=attempt.account,
                network=attempt.network,
                kind="attempt_resolved",
                payload={
                    "client_order_id": attempt_id,
                    "state": state.value,
                    "status": attempt.exchange_status,
                },
            )
            await session.commit()

    async def open_of(
        self, session: AsyncSession, *, signal_id: uuid.UUID, roles: tuple[Role, ...]
    ) -> list[OrderAttempt]:
        """Attempts of these roles whose outcome is not proven yet."""
        return list(
            (
                await session.scalars(
                    select(OrderAttempt)
                    .where(
                        OrderAttempt.signal_id == signal_id,
                        OrderAttempt.role.in_([r.value for r in roles]),
                        OrderAttempt.state.in_(
                            [
                                AttemptState.PREPARED,
                                AttemptState.SENT,
                                AttemptState.UNKNOWN,
                            ]
                        ),
                    )
                    .order_by(OrderAttempt.created_at)
                )
            ).all()
        )

    async def unapplied_lists(
        self, session: AsyncSession, *, signal_id: uuid.UUID
    ) -> list[OrderAttempt]:
        """OCO attempts the exchange answered with a list that was never applied.

        The answer is committed before the list is associated with the
        position (or proven finished); a crash in between leaves a list that
        may be live and hold the asset. Until applied, no other OCO or market
        sale may be sized for the position.
        """
        return list(
            (
                await session.scalars(
                    select(OrderAttempt)
                    .where(
                        OrderAttempt.signal_id == signal_id,
                        OrderAttempt.role == Role.LIST.value,
                        OrderAttempt.state.in_(
                            [AttemptState.ACKED, AttemptState.RESOLVED]
                        ),
                        OrderAttempt.order_list_id.is_not(None),
                        OrderAttempt.applied_at.is_(None),
                    )
                    .order_by(OrderAttempt.created_at)
                )
            ).all()
        )

    async def mark_applied(self, session: AsyncSession, attempt_id: str) -> None:
        """Record, in the caller's transaction, that the answer took effect."""
        attempt = await session.get(OrderAttempt, attempt_id, with_for_update=True)
        if attempt is not None and attempt.applied_at is None:
            attempt.applied_at = self._clock()

    async def next_sequence(
        self, session: AsyncSession, *, signal_id: uuid.UUID, role: Role
    ) -> int:
        rows = (
            await session.scalars(
                select(OrderAttempt.sequence).where(
                    OrderAttempt.signal_id == signal_id, OrderAttempt.role == role.value
                )
            )
        ).all()
        return max(rows, default=0) + 1


async def _shielded(write: Awaitable[None]) -> None:
    """Finish a journal write even if the caller is being cancelled."""
    task = asyncio.ensure_future(write)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


def _status(data: Any) -> str | None:
    for name in ("status", "list_order_status"):
        value = getattr(data, name, None)
        if value is not None:
            return str(getattr(value, "value", value))
    return None


def _model_dict(data: Any) -> dict[str, Any] | None:
    to_dict = getattr(data, "to_dict", None)
    if to_dict is None:
        return None
    result: dict[str, Any] = to_dict()
    return result


def _jsonable(request: dict[str, Any]) -> dict[str, Any]:
    return {
        key: str(value)
        if isinstance(value, Decimal)
        else getattr(value, "value", value)
        for key, value in request.items()
    }


__all__ = [
    "ABSENCE_GRACE",
    "AttemptJournal",
    "ClientIdOverflow",
    "Found",
    "Lookup",
    "NOT_FOUND_CODES",
    "Role",
    "client_id",
]
