"""Reconciler: our books against the exchange's trades (plan §9.2, §9.6).

Runs at start, after a reconnect, every ``reconcile_interval`` and on every
UNKNOWN. It never edits a position itself — fills go through the position
applier, closes through the finalizer, protection through the protector.

**Trades.** For every symbol with an open position the account's trades are
read from the stored cursor on (``fromId`` is inclusive, so the boundary trade
comes back and is deduplicated). Each page is booked in **one transaction**:
the fills, their application to the positions they belong to, and the new
cursor — a crash leaves either all of it or none. A trade belongs to a
position through the order it came from: our entry and exit attempts, and the
legs of our lists.

**Protection.** A position recorded as protected whose list is no longer live
had a leg execute while nobody watched: its fills are booked and the position
closes by the leg that sold it, or, with a remainder left, it becomes
unprotected and goes back to the protection protocol. A position with an
operation left half-done resumes it.

**Quarantine and divergence** (dedicated account, Q16). An open order with our
``mta-`` prefix that no attempt knows is quarantined — reported, never
touched. Any other open order, a trade of an order we did not place, or a
balance of a traded asset that no position explains is a divergence. The
agent does not act on either; it keeps admission closed and says so.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from agent.exchange.budget import Priority
from agent.exchange.trade import TradeCallError
from agent.execution.attempts import Role
from agent.execution.ledger import FillRecord, lock_position, record_event
from agent.execution.models import (
    OrderAttempt,
    Position,
    PositionStatus,
    TradeCursor,
)
from agent.execution.protection import Protector, Step
from agent.execution.runtime import ExecutionContext, ExecutionRuntime
from agent.execution.worker import PositionWorker

logger = logging.getLogger(__name__)

PAGE = 1000


@dataclass
class ReconcileReport:
    fills: int = 0
    closed: list[str] = field(default_factory=list)
    unprotected: list[str] = field(default_factory=list)
    quarantined: list[str] = field(default_factory=list)
    divergence: list[str] = field(default_factory=list)


class Reconciler:
    def __init__(
        self,
        worker: PositionWorker,
        *,
        prices: Callable[[str], Awaitable[Decimal | None]] | None = None,
        page: int = PAGE,
    ) -> None:
        self.worker = worker
        self.protector: Protector = worker.protector
        self._prices = prices
        self.page = page

    @property
    def runtime(self) -> ExecutionRuntime:
        return self.worker.orders.runtime

    async def run(self) -> ReconcileReport:
        report = ReconcileReport()
        context = self.runtime.context
        trade = self.runtime.trade
        if context is None or trade is None:
            return report
        await self.worker.orders.resolve_entries()
        symbols = await self._symbols(context)
        for symbol in symbols:
            try:
                report.fills += await self.sync_trades(context, symbol)
            except TradeCallError as exc:
                logger.warning("reconcile trades of %s failed: %s", symbol, exc.message)
        for position in await self._open_positions(context):
            await self._check_protection(position, report)
        await self._divergence(context, symbols, report)
        return report

    # --- trades -------------------------------------------------------------------------

    async def sync_trades(self, context: ExecutionContext, symbol: str) -> int:
        """Book every trade of ``symbol`` from the cursor on; returns new fills."""
        trade = self.runtime.trade
        assert trade is not None
        booked = 0
        cursor = await self._cursor(context, symbol)
        while True:
            page = (
                await trade.my_trades(
                    symbol=symbol,
                    from_id=cursor,
                    limit=self.page,
                    priority=Priority.RECONCILE,
                )
            ).data
            fresh = [t for t in page if t.id is not None and t.id >= cursor]
            if not fresh:
                return booked
            booked += await self._book_page(context, symbol, fresh)
            last = max(t.id for t in fresh if t.id is not None)
            if len(page) < self.page:
                return booked
            cursor = last + 1

    async def _book_page(
        self, context: ExecutionContext, symbol: str, trades: list[Any]
    ) -> int:
        owners = await self._owners(context, symbol)
        by_position: dict[uuid.UUID, list[FillRecord]] = {}
        strangers: list[int] = []
        for row in trades:
            position_id = owners.get(int(row.order_id))
            if position_id is None:
                strangers.append(int(row.id))
                continue
            by_position.setdefault(position_id, []).append(
                FillRecord.from_my_trade(row)
            )
        applied = 0
        last = max(int(t.id) for t in trades)
        async with self.runtime.session() as session, session.begin():
            for position_id in sorted(by_position):  # a fixed lock order
                position = await lock_position(session, position_id)
                result = await self.worker.orders.applier.apply(
                    session, position, by_position[position_id]
                )
                applied += len(result.new_fills)
            await session.execute(
                insert(TradeCursor)
                .values(
                    account=context.account,
                    network=context.network,
                    symbol=symbol,
                    last_trade_id=last,
                )
                .on_conflict_do_update(
                    index_elements=["account", "network", "symbol"],
                    set_={"last_trade_id": last},
                    where=TradeCursor.last_trade_id < last,
                )
            )
        if strangers:
            self.runtime.alerts[f"divergence:{symbol}"] = (
                f"trades of orders not placed by the agent: {strangers[:5]}"
            )
        return applied

    async def _cursor(self, context: ExecutionContext, symbol: str) -> int:
        """Where to read from: the stored cursor, else the first order of ours."""
        async with self.runtime.session() as session:
            stored = await session.scalar(
                select(TradeCursor.last_trade_id).where(
                    TradeCursor.account == context.account,
                    TradeCursor.network == context.network,
                    TradeCursor.symbol == symbol,
                )
            )
        if stored is not None:
            return int(stored)
        # No cursor yet: start at the first trade of our earliest order here.
        orders = sorted(await self._owners(context, symbol))
        if not orders:
            return 0
        trade = self.runtime.trade
        assert trade is not None
        rows = (
            await trade.my_trades(
                symbol=symbol, order_id=orders[0], priority=Priority.RECONCILE
            )
        ).data
        ids = [int(r.id) for r in rows if r.id is not None]
        return min(ids) if ids else 0

    async def _owners(
        self, context: ExecutionContext, symbol: str
    ) -> dict[int, uuid.UUID]:
        """Order id → position, for every order of ours on ``symbol``."""
        async with self.runtime.session() as session:
            attempts = (
                await session.scalars(
                    select(OrderAttempt).where(
                        OrderAttempt.account == context.account,
                        OrderAttempt.network == context.network,
                        OrderAttempt.symbol == symbol,
                    )
                )
            ).all()
            positions = {
                p.signal_id: p.id
                for p in (
                    await session.scalars(
                        select(Position).where(
                            Position.account == context.account,
                            Position.network == context.network,
                            Position.symbol == symbol,
                        )
                    )
                ).all()
            }
        owners: dict[int, uuid.UUID] = {}
        for attempt in attempts:
            position_id = attempt.position_id or positions.get(attempt.signal_id)
            if position_id is None:
                continue
            if attempt.order_id is not None:
                owners[attempt.order_id] = position_id
            if attempt.role == Role.LIST.value:
                for leg in _list_orders(attempt.response):
                    owners[leg] = position_id
        return owners

    # --- protection ---------------------------------------------------------------------

    async def _check_protection(
        self, position: Position, report: ReconcileReport
    ) -> None:
        if position.operation is not None:
            result = await self.worker.resume(position.id)
            if result.step is Step.CLOSED:
                report.closed.append(position.symbol)
            return
        if (
            position.status != PositionStatus.PROTECTED
            or position.protection_list_id is None
        ):
            return
        trade = self.runtime.trade
        assert trade is not None
        try:
            order_list = (
                await trade.get_order_list(
                    list_client_order_id=position.protection_list_id,
                    priority=Priority.RECONCILE,
                )
            ).data
        except TradeCallError as exc:
            logger.warning(
                "cannot read the list of %s: %s", position.symbol, exc.message
            )
            return
        if str(order_list.list_order_status) == "EXECUTING":
            return
        async with self.worker.orders.lock_for(position.id):
            result = await self.protector.settle_list(position.id, order_list)
        if result.step is Step.CLOSED:
            report.closed.append(position.symbol)
            return
        await self._unprotect(position.id)
        report.unprotected.append(position.symbol)

    async def _unprotect(self, position_id: uuid.UUID) -> None:
        """The list is gone with a remainder left: back to the protection loop."""
        async with self.runtime.session() as session, session.begin():
            position = await lock_position(session, position_id)
            if position.status != PositionStatus.PROTECTED:
                return
            position.status = PositionStatus.UNPROTECTED
            position.protection_list_id = None
            position.exchange_stop = None
            if position.unprotected_since is None:
                position.unprotected_since = self.worker.orders.clock()
            await record_event(
                session,
                account=position.account,
                network=position.network,
                kind="protection_lost",
                payload={"reason": "list finished with a remainder"},
                position=position,
            )

    # --- quarantine and divergence --------------------------------------------------------

    async def _divergence(
        self, context: ExecutionContext, symbols: list[str], report: ReconcileReport
    ) -> None:
        trade = self.runtime.trade
        assert trade is not None
        async with self.runtime.session() as session:
            known = set(
                (
                    await session.scalars(
                        select(OrderAttempt.client_order_id).where(
                            OrderAttempt.account == context.account,
                            OrderAttempt.network == context.network,
                        )
                    )
                ).all()
            )
            legs: set[str] = set()
            for response in (
                await session.scalars(
                    select(OrderAttempt.response).where(
                        OrderAttempt.role == Role.LIST.value,
                        OrderAttempt.account == context.account,
                    )
                )
            ).all():
                legs |= _list_client_ids(response)
            known |= legs
            known |= {
                attempt.list_client_order_id
                for attempt in (
                    await session.scalars(
                        select(OrderAttempt).where(OrderAttempt.role == Role.LIST.value)
                    )
                ).all()
                if attempt.list_client_order_id
            }
        for symbol in symbols:
            try:
                orders = (await trade.open_orders(symbol=symbol)).data
            except TradeCallError as exc:
                logger.warning("open orders of %s unavailable: %s", symbol, exc.message)
                continue
            for order in orders:
                cid = str(
                    order.get("clientOrderId")
                    if isinstance(order, dict)
                    else order.client_order_id
                )
                if cid in known:
                    continue
                if cid.startswith("mta-"):
                    report.quarantined.append(cid)
                    self.runtime.alerts[f"quarantine:{cid}"] = (
                        f"{symbol} order {cid} is ours by name but unknown; not touched"
                    )
                else:
                    report.divergence.append(cid)
                    self.runtime.alerts[f"divergence:{symbol}"] = (
                        f"{symbol} has an order the agent did not place ({cid})"
                    )
        await self._balances(context, report)

    async def _balances(
        self, context: ExecutionContext, report: ReconcileReport
    ) -> None:
        """A traded asset held beyond what the open positions explain."""
        trade = self.runtime.trade
        if trade is None or self._prices is None:
            return
        try:
            account = (await trade.get_account()).data
        except TradeCallError:
            return
        held = {p.symbol: p.quantity for p in await self._open_positions(context)}
        for row in account.balances or []:
            asset = str(row.asset)
            if asset in ("USDT", "BNB"):
                continue
            amount = Decimal(row.free or "0") + Decimal(row.locked or "0")
            symbol = f"{asset}USDT"
            extra = amount - held.get(symbol, Decimal(0))
            if extra <= 0:
                continue
            price = await self._prices(symbol)
            if price is None or extra * price < Decimal(5):
                continue  # dust (plan §8.4)
            report.divergence.append(symbol)
            self.runtime.alerts[f"divergence:{symbol}"] = (
                f"{extra} {asset} held without a position"
            )

    # --- reads ------------------------------------------------------------------------------

    async def _symbols(self, context: ExecutionContext) -> list[str]:
        async with self.runtime.session() as session:
            rows = (
                await session.scalars(
                    select(Position.symbol)
                    .where(
                        Position.account == context.account,
                        Position.network == context.network,
                        Position.status != PositionStatus.CLOSED,
                    )
                    .distinct()
                )
            ).all()
        return sorted(rows)

    async def _open_positions(self, context: ExecutionContext) -> list[Position]:
        async with self.runtime.session() as session:
            return list(
                (
                    await session.scalars(
                        select(Position).where(
                            Position.account == context.account,
                            Position.network == context.network,
                            Position.status != PositionStatus.CLOSED,
                        )
                    )
                ).all()
            )


def _list_orders(response: dict[str, Any] | None) -> list[int]:
    if not response:
        return []
    items = (response.get("orders") or []) + (response.get("orderReports") or [])
    return [int(o["orderId"]) for o in items if o.get("orderId") is not None]


def _list_client_ids(response: dict[str, Any] | None) -> set[str]:
    if not response:
        return set()
    items = (response.get("orders") or []) + (response.get("orderReports") or [])
    return {str(o["clientOrderId"]) for o in items if o.get("clientOrderId")}


__all__ = ["PAGE", "ReconcileReport", "Reconciler"]
