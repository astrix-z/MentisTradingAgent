"""The one writer of fills, ledgers, positions and closes (plan §8.3, §8.5).

``PositionApplier`` is used by the order manager, the position worker and the
reconciler alike; none of them edits a position or a fill directly. Every
method runs inside the caller's transaction, so the change and its audit event
commit together (the event table is also the outbox).

Guarantees:

* **Idempotent.** A fill is inserted with ``ON CONFLICT DO NOTHING`` on
  ``(account, network, symbol, trade_id)``; only a fill that was actually new
  moves the ledger. Replaying a page of trades, or applying the same fill from
  an order response and later from ``myTrades``, changes nothing twice.
* **Serialized per position.** The position row is locked (``FOR UPDATE``)
  before it is read, so concurrent appliers — two coroutines, or two
  processes — take turns, and each sees the other's result.
* **Proven quantity.** :meth:`PositionApplier.proven` says whether the fills
  of a set of confirmed orders are all in the ledger (sum per order equals the
  exchange's ``executedQty``). Protection and exits are computed only from a
  proven ledger (plan §6.3 barrier).

``Finalizer.close`` closes a position exactly once, in one transaction: status
and outcome, the absolute cooldown, the daily losing-trade count (Q19: net
result below zero, dated by the last closing fill in UTC) and the
``position_closed`` event. Strategy feedback happens after commit.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from agent.execution.models import (
    ExecutionEvent,
    ExecutionIntent,
    Fill,
    IntentState,
    Position,
    PositionStatus,
    RiskState,
    SymbolCooldown,
)
from agent.execution.outcome import ExecutionOutcome, ExitMechanism, ExitReason
from agent.strategies.base import ExitPolicy, transport_value

logger = logging.getLogger(__name__)


def _utc_ms(ms: int | None) -> datetime:
    if ms is None:
        raise ValueError("trade without a time")
    return datetime.fromtimestamp(ms / 1000, UTC)


@dataclass(frozen=True)
class FillRecord:
    """One exchange trade in our terms, whatever it was read from."""

    symbol: str
    trade_id: int
    order_id: int
    side: str  # "BUY" / "SELL"
    price: Decimal
    quantity: Decimal
    quote_quantity: Decimal
    commission: Decimal
    commission_asset: str
    is_maker: bool
    time: datetime
    source: str
    client_order_id: str | None = None

    @classmethod
    def from_my_trade(cls, trade: Any, *, source: str = "my_trades") -> FillRecord:
        """From an SDK ``MyTradesResponse`` row."""
        return cls(
            symbol=str(trade.symbol),
            trade_id=int(trade.id),
            order_id=int(trade.order_id),
            side="BUY" if trade.is_buyer else "SELL",
            price=Decimal(trade.price),
            quantity=Decimal(trade.qty),
            quote_quantity=Decimal(trade.quote_qty),
            commission=Decimal(trade.commission),
            commission_asset=str(trade.commission_asset),
            is_maker=bool(trade.is_maker),
            time=_utc_ms(trade.time),
            source=source,
        )

    @classmethod
    def from_order_fill(
        cls,
        fill: Any,
        *,
        symbol: str,
        order_id: int,
        side: str,
        time: datetime,
        client_order_id: str | None,
    ) -> FillRecord:
        """From an SDK ``NewOrderResponseFillsInner`` of a FULL order response.

        The response carries no per-fill time; the order's transaction time
        stands in for it until ``myTrades`` confirms the same trade id.
        """
        price, qty = Decimal(fill.price), Decimal(fill.qty)
        return cls(
            symbol=symbol,
            trade_id=int(fill.trade_id),
            order_id=order_id,
            side=side,
            price=price,
            quantity=qty,
            quote_quantity=price * qty,
            commission=Decimal(fill.commission),
            commission_asset=str(fill.commission_asset),
            is_maker=False,
            time=time,
            source="response",
            client_order_id=client_order_id,
        )


#: Converts a commission to quote: ``(asset, amount, fill) -> value or None``.
CommissionPrice = Callable[[str, datetime], Awaitable[Decimal | None]]


class CommissionConverter:
    """Commission in quote currency at the fill's time (Q19).

    Quote-asset commissions are taken as is, base-asset ones at the fill price,
    anything else (BNB) at ``price(asset, fill.time)`` — the asset's price in
    the quote currency at that time. ``None`` when no price can be found; the
    finalizer then falls back to the current price and marks the result as
    estimated.
    """

    def __init__(
        self, quote: str, base_of: Callable[[str], str], price: CommissionPrice
    ) -> None:
        self.quote = quote
        self._base_of = base_of
        self._price = price

    async def convert(self, fill: FillRecord) -> Decimal | None:
        if fill.commission == 0:
            return Decimal(0)
        if fill.commission_asset == self.quote:
            return fill.commission
        if fill.commission_asset == self._base_of(fill.symbol):
            return fill.commission * fill.price
        price = await self._price(fill.commission_asset, fill.time)
        return fill.commission * price if price is not None else None


@dataclass(frozen=True)
class ApplyResult:
    new_fills: list[FillRecord]
    position: Position


async def lock_position(session: AsyncSession, position_id: uuid.UUID) -> Position:
    """The position row, locked for the rest of the transaction."""
    position = (
        await session.execute(
            select(Position)
            .where(Position.id == position_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return position


async def record_event(
    session: AsyncSession,
    *,
    account: str,
    network: str,
    kind: str,
    payload: Mapping[str, Any],
    position: Position | None = None,
) -> ExecutionEvent:
    """Append an audit/outbox event in the caller's transaction.

    A position's events are numbered without gaps: the caller holds the
    position row lock, so ``aggregate_seq`` cannot race.
    """
    seq = None
    if position is not None:
        position.aggregate_seq += 1
        seq = position.aggregate_seq
    event = ExecutionEvent(
        event_id=uuid.uuid4(),
        account=account,
        network=network,
        position_id=position.id if position is not None else None,
        aggregate_seq=seq,
        type=kind,
        payload=transport_value(dict(payload)),
    )
    session.add(event)
    return event


class PositionApplier:
    def __init__(
        self, converter: CommissionConverter, base_of: Callable[[str], str]
    ) -> None:
        self._converter = converter
        self._base_of = base_of

    async def open_from_entry(
        self,
        session: AsyncSession,
        intent: ExecutionIntent,
        *,
        exit_policy: ExitPolicy,
        fills: Iterable[FillRecord],
        detected_at: datetime | None = None,
    ) -> ApplyResult:
        """Turn a filled ENTRY into a position (or find the one it already made).

        In the same transaction the intent's quote reserve is paid off and its
        symbol and slot pass to the position (plan §7.5). ``detected_at`` is
        when the fill was first confirmed or found: the protection deadline
        runs from it and is never moved later (plan §6.3).
        """
        if intent.position_id is not None:
            position = await lock_position(session, intent.position_id)
        else:
            position = Position(
                id=uuid.uuid4(),
                account=intent.account,
                network=intent.network,
                symbol=intent.symbol,
                strategy=intent.strategy,
                signal_id=intent.signal_id,
                status=PositionStatus.PROTECTING,
                exit_policy=exit_policy.transport(),
                quantity=Decimal(0),
                bought_qty=Decimal(0),
                sold_qty=Decimal(0),
                base_commission=Decimal(0),
                bought_quote=Decimal(0),
                sold_quote=Decimal(0),
                commission_quote=Decimal(0),
                op_version=0,
                aggregate_seq=0,
                unprotected_since=detected_at or datetime.now(UTC),
            )
            session.add(position)
            await session.flush()
            intent.position_id = position.id
            intent.state = IntentState.FILLED
            intent.quote_reserved = Decimal(0)
            intent.resolved_at = datetime.now(UTC)
            await record_event(
                session,
                account=position.account,
                network=position.network,
                kind="position_opened",
                payload={"signal_id": str(intent.signal_id), "symbol": intent.symbol},
                position=position,
            )
        result = await self.apply(session, position, fills)
        return result

    async def apply(
        self, session: AsyncSession, position: Position, fills: Iterable[FillRecord]
    ) -> ApplyResult:
        """Add the fills that are new to the position's ledger.

        ``position`` must be locked by this transaction (see
        :func:`lock_position`, or just created in it).
        """
        applied: list[FillRecord] = []
        base = self._base_of(position.symbol)
        for fill in fills:
            if fill.symbol != position.symbol:
                raise ValueError(
                    f"fill of {fill.symbol} for position in {position.symbol}"
                )
            commission_quote = await self._converter.convert(fill)
            inserted = await session.execute(
                insert(Fill)
                .values(
                    account=position.account,
                    network=position.network,
                    symbol=fill.symbol,
                    trade_id=fill.trade_id,
                    order_id=fill.order_id,
                    client_order_id=fill.client_order_id,
                    position_id=position.id,
                    side=fill.side,
                    price=fill.price,
                    quantity=fill.quantity,
                    quote_quantity=fill.quote_quantity,
                    commission=fill.commission,
                    commission_asset=fill.commission_asset,
                    commission_quote=commission_quote,
                    is_maker=fill.is_maker,
                    trade_time=fill.time,
                    source=fill.source,
                )
                .on_conflict_do_nothing()
                .returning(Fill.trade_id)
            )
            if inserted.scalar_one_or_none() is None:
                continue  # already in the ledger
            applied.append(fill)
            if fill.side == "BUY":
                position.bought_qty += fill.quantity
                position.bought_quote += fill.quote_quantity
                if position.opened_at is None or fill.time < position.opened_at:
                    position.opened_at = fill.time
            else:
                position.sold_qty += fill.quantity
                position.sold_quote += fill.quote_quantity
            if fill.commission_asset == base:
                position.base_commission += fill.commission
            if commission_quote is not None:
                position.commission_quote += commission_quote
        if applied:
            position.quantity = max(
                position.bought_qty - position.sold_qty - position.base_commission,
                Decimal(0),
            )
            if position.bought_qty > 0:
                position.fill_vwap = position.bought_quote / position.bought_qty
            await record_event(
                session,
                account=position.account,
                network=position.network,
                kind="fills_applied",
                payload={
                    "trade_ids": [f.trade_id for f in applied],
                    "quantity": position.quantity,
                },
                position=position,
            )
        return ApplyResult(applied, position)

    async def proven(
        self, session: AsyncSession, position: Position, executed: Mapping[int, Decimal]
    ) -> bool:
        """Whether every confirmed order's fills are all in the ledger.

        ``executed`` maps order id → the exchange's ``executedQty`` for orders
        whose status is terminal. Commissions are part of each fill row, so a
        complete set of fills is also a complete set of commissions.
        """
        if not executed:
            return True
        rows = await session.execute(
            select(Fill.order_id, func.sum(Fill.quantity))
            .where(
                Fill.account == position.account,
                Fill.network == position.network,
                Fill.symbol == position.symbol,
                Fill.order_id.in_(list(executed)),
            )
            .group_by(Fill.order_id)
        )
        booked = {order_id: total for order_id, total in rows.all()}
        return all(
            booked.get(order_id, Decimal(0)) == qty
            for order_id, qty in executed.items()
        )


class Finalizer:
    """Closes a position exactly once, with everything that goes with it."""

    def __init__(self, fallback_price: CommissionPrice | None = None) -> None:
        self._fallback_price = fallback_price

    async def close(
        self,
        session: AsyncSession,
        position: Position,
        *,
        reason: ExitReason,
        mechanism: ExitMechanism,
        order_ids: Iterable[str] = (),
    ) -> ExecutionOutcome | None:
        """Close ``position`` (locked by the caller); ``None`` if already closed."""
        if position.status == PositionStatus.CLOSED:
            return None
        estimated, pending = await self._fill_missing_commissions(session, position)
        last_sell = await session.scalar(
            select(func.max(Fill.trade_time)).where(
                Fill.position_id == position.id, Fill.side == "SELL"
            )
        )
        closed_at: datetime = last_sell or datetime.now(UTC)
        pnl = position.sold_quote - position.bought_quote - position.commission_quote
        exit_vwap = (
            position.sold_quote / position.sold_qty if position.sold_qty > 0 else None
        )
        policy = ExitPolicy.from_transport(position.exit_policy)
        outcome = ExecutionOutcome(
            position_id=str(position.id),
            signal_id=str(position.signal_id),
            account=position.account,
            network=position.network,
            symbol=position.symbol,
            strategy=position.strategy,
            reason=reason,
            mechanism=mechanism,
            opened_at=position.opened_at or closed_at,
            closed_at=closed_at,
            quantity=position.bought_qty,
            entry_vwap=position.fill_vwap or Decimal(0),
            exit_vwap=exit_vwap,
            realized_pnl=pnl,
            commission_quote=position.commission_quote,
            order_ids=tuple(order_ids),
            exit_policy=position.exit_policy,
        )
        # A fee without a price is not zero: the result stays provisional and,
        # since a profit cannot be proven, counts as losing until revalued.
        losing = outcome.losing or bool(pending)
        position.status = PositionStatus.CLOSED
        position.closed_at = closed_at
        position.realized_pnl = pnl
        position.operation = None
        position.outcome = {
            **outcome.transport(),
            "commission_estimated": estimated,
            "commission_pending": pending,
            "counted_losing": losing,
        }
        cooldown_until = closed_at + timedelta(minutes=policy.cooldown_minutes)
        await session.execute(
            insert(SymbolCooldown)
            .values(
                account=position.account,
                network=position.network,
                symbol=position.symbol,
                cooldown_until=cooldown_until,
                position_id=position.id,
                updated_at=func.now(),
            )
            .on_conflict_do_update(
                index_elements=["account", "network", "symbol"],
                set_={
                    "cooldown_until": func.greatest(
                        SymbolCooldown.cooldown_until, cooldown_until
                    ),
                    "position_id": position.id,
                    "updated_at": func.now(),
                },
            )
        )
        if losing:
            await _count_losing(session, position, closed_at, 1)
        await record_event(
            session,
            account=position.account,
            network=position.network,
            kind="position_closed",
            payload={**position.outcome, "cooldown_until": cooldown_until},
            position=position,
        )
        return outcome

    async def revalue(self, session: AsyncSession, position: Position) -> bool:
        """Value the fees a closed position still lacks; True once none is left.

        Recomputes the result and, if its losing/not-losing verdict changes,
        moves the count of the day it closed on. ``position`` is locked by the
        caller.
        """
        if position.status != PositionStatus.CLOSED or position.outcome is None:
            return True
        _, pending = await self._fill_missing_commissions(session, position)
        outcome = dict(position.outcome)
        if pending == outcome.get("commission_pending"):
            return not pending
        pnl = position.sold_quote - position.bought_quote - position.commission_quote
        losing = pnl < 0 or bool(pending)
        closed_at = position.closed_at or datetime.now(UTC)
        if losing != bool(outcome.get("counted_losing")):
            await _count_losing(session, position, closed_at, 1 if losing else -1)
        position.realized_pnl = pnl
        position.outcome = {
            **outcome,
            "realized_pnl": transport_value(pnl),
            "commission_quote": transport_value(position.commission_quote),
            "commission_estimated": True,
            "commission_pending": pending,
            "counted_losing": losing,
        }
        await record_event(
            session,
            account=position.account,
            network=position.network,
            kind="position_revalued",
            payload={"realized_pnl": pnl, "pending": pending, "losing": losing},
            position=position,
        )
        return not pending

    async def _fill_missing_commissions(
        self, session: AsyncSession, position: Position
    ) -> tuple[bool, list[int]]:
        """Convert commissions that had no price when they were booked.

        Priced at their own trade time. Returns whether any was converted now
        and the trade ids that still have no price.
        """
        missing = (
            await session.scalars(
                select(Fill)
                .where(Fill.position_id == position.id, Fill.commission_quote.is_(None))
                .order_by(Fill.trade_id)
            )
        ).all()
        estimated = False
        pending: list[int] = []
        for fill in missing:
            try:
                price = (
                    await self._fallback_price(fill.commission_asset, fill.trade_time)
                    if self._fallback_price is not None
                    else None
                )
            except Exception as exc:  # noqa: BLE001 - unpriced, retried later
                logger.warning("commission price of %s failed: %s", fill.trade_id, exc)
                price = None
            if price is None:
                logger.warning(
                    "commission of trade %s (%s %s) has no quote price yet",
                    fill.trade_id,
                    fill.commission,
                    fill.commission_asset,
                )
                pending.append(int(fill.trade_id))
                continue
            fill.commission_quote = fill.commission * price
            position.commission_quote += fill.commission_quote
            estimated = True
        return estimated, pending


async def _count_losing(
    session: AsyncSession, position: Position, closed_at: datetime, delta: int
) -> None:
    """Move the losing-trade count of the UTC day ``closed_at`` falls on."""
    await session.execute(
        insert(RiskState)
        .values(
            account=position.account,
            network=position.network,
            utc_date=closed_at.astimezone(UTC).date(),
            losing_trades=max(delta, 0),
            updated_at=func.now(),
        )
        .on_conflict_do_update(
            index_elements=["account", "network", "utc_date"],
            set_={
                "losing_trades": func.greatest(RiskState.losing_trades + delta, 0),
                "updated_at": func.now(),
            },
        )
    )


async def losing_trades_today(
    session: AsyncSession, account: str, network: str, now: datetime
) -> int:
    value = await session.scalar(
        select(RiskState.losing_trades).where(
            RiskState.account == account,
            RiskState.network == network,
            RiskState.utc_date == now.astimezone(UTC).date(),
        )
    )
    return int(value or 0)


__all__ = [
    "ApplyResult",
    "CommissionConverter",
    "FillRecord",
    "Finalizer",
    "PositionApplier",
    "lock_position",
    "losing_trades_today",
    "record_event",
]
