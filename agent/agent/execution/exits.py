"""Exits that are not a stop or a take-profit on the exchange (plan §6.5, §6.6, §9.4).

* **Time exit** — a position open for ``max_hold_hours`` (from its own policy,
  counted from ``opened_at`` in the database) is sold at market
  (``time`` / ``market_time``). The timer is the stored time, so a position
  whose hold ran out while the agent was down is sold right after reconcile.
* **Guard** — only when the stop leg is a ``STOP_LOSS_LIMIT`` (the fallback on
  symbols without market stops): its limit may be skipped by a gap and leave
  the position unsold below its stop. The guard watches the last trade from
  the position's own trade stream; when it is at or below the stop for longer
  than ``guard_grace_s`` and the leg did not execute, the position is sold at
  market (``stop`` / ``guard``). A stale trade price is no reason to act: the
  position is reported degraded instead.
* **Manual close** — the ``close_position`` command (``manual`` /
  ``manual_close``).
* **Write-off** — the ``write_off_position`` command, for a position whose
  symbol no longer trades (delisted): closed in the database without an order
  (``delisted`` / ``write_off``), with the quote the owner got for the asset, if
  any. Refused while the symbol trades, while the exchange shows an open order
  on it, or while an order of the position has an unproven outcome.

All three go through the position worker's protocol and run whether or not the
strategy is running.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from agent.exchange.rest import INVALID_SYMBOL
from agent.exchange.trade import TradeCallError
from agent.execution.attempts import Role
from agent.execution.models import OrderAttempt, Position, PositionStatus
from agent.execution.monitor import MonitorFeed
from agent.execution.outcome import ExitMechanism, ExitReason
from agent.execution.protection import Step, StepResult
from agent.execution.runtime import ExecutionRuntime
from agent.execution.worker import HALTED, PositionWorker, StatusUnknown
from agent.strategies.base import ExitPolicy

logger = logging.getLogger(__name__)

OPEN = (
    PositionStatus.PROTECTED,
    PositionStatus.REPRICING,
    PositionStatus.PROTECTING,
    PositionStatus.UNPROTECTED,
    PositionStatus.PROTECTION_OVERDUE,
)


class NoSuchPosition(LookupError):
    pass


class SymbolNotTrading(LookupError):
    """``close_position`` on a symbol that takes no orders: the list stays."""


class WriteOffRefused(ValueError):
    """``write_off_position`` may not close this position (yet)."""


class Exits:
    def __init__(
        self,
        worker: PositionWorker,
        feed: MonitorFeed | None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.worker = worker
        self.feed = feed
        self._clock = clock or worker.orders.clock
        self._breach_since: dict[uuid.UUID, datetime] = {}

    @property
    def runtime(self) -> ExecutionRuntime:
        return self.worker.orders.runtime

    # --- time -------------------------------------------------------------------------

    async def check_time(self, position_id: uuid.UUID) -> StepResult | None:
        position = await self._position(position_id)
        if position.status not in OPEN or position.opened_at is None:
            return None
        policy = ExitPolicy.from_transport(position.exit_policy)
        due = position.opened_at + timedelta(hours=float(policy.max_hold_hours))
        if self._clock() < due:
            return None
        if f"{HALTED}{position.symbol}" not in self.runtime.alerts:
            logger.info(
                "%s held since %s: time exit", position.symbol, position.opened_at
            )
        return await self.worker.close(
            position_id, ExitReason.TIME, ExitMechanism.MARKET_TIME
        )

    # --- guard --------------------------------------------------------------------------

    async def check_guard(self, position_id: uuid.UUID) -> StepResult | None:
        position = await self._position(position_id)
        if (
            position.status != PositionStatus.PROTECTED
            or position.exchange_stop is None
            or not await self._limit_stop(position)
        ):
            self._breach_since.pop(position_id, None)
            return None
        last = self.feed.last_trade(position.symbol) if self.feed else None
        now = self._clock()
        max_age = timedelta(seconds=self.runtime.settings.quote_max_trade_age_s)
        if last is None or now - last.time > max_age:
            self.runtime.alerts[f"guard:{position.symbol}"] = (
                f"no fresh trades for {position.symbol}; guard cannot act"
            )
            return None
        self.runtime.alerts.pop(f"guard:{position.symbol}", None)
        if last.price > position.exchange_stop:
            self._breach_since.pop(position_id, None)
            return None
        since = self._breach_since.setdefault(position_id, now)
        if now - since < timedelta(seconds=self.runtime.settings.guard_grace_s):
            return None
        logger.warning(
            "%s last %s below stop %s for %ss; guard exit",
            position.symbol,
            last.price,
            position.exchange_stop,
            int((now - since).total_seconds()),
        )
        self._breach_since.pop(position_id, None)
        return await self.worker.close(
            position_id, ExitReason.STOP, ExitMechanism.GUARD
        )

    async def _limit_stop(self, position: Position) -> bool:
        """Whether the current list's stop leg is a STOP_LOSS_LIMIT."""
        if position.protection_list_id is None:
            return False
        async with self.runtime.session() as session:
            request = await session.scalar(
                select(OrderAttempt.request).where(
                    OrderAttempt.client_order_id == position.protection_list_id
                )
            )
        return bool(request and request.get("stop_limit") not in (None, "None"))

    # --- manual --------------------------------------------------------------------------

    async def close_symbol(self, symbol: str) -> StepResult:
        """The ``close_position`` command."""
        position_id = await self._open_position(symbol.upper())
        # Under the position's lock: every close holds it, so an alert this
        # read sets cannot outlive a position closed meanwhile.
        async with self.worker.orders.lock_for(position_id):
            position = await self._position(position_id)
            if position.status == PositionStatus.CLOSED:
                raise NoSuchPosition(f"no open position in {symbol.upper()}")
            try:
                status = await self.worker.not_trading(position)
            except StatusUnknown as exc:
                raise SymbolNotTrading(
                    f"{exc}: not closed, its protection stays on the exchange"
                ) from exc
        if status is not None:
            raise SymbolNotTrading(
                f"{symbol.upper()} is {status}: not closed, its protection stays "
                "on the exchange"
            )
        return await self.worker.close(
            position_id, ExitReason.MANUAL, ExitMechanism.MANUAL_CLOSE
        )

    async def write_off(
        self, symbol: str, *, confirm: str, proceeds: Decimal = Decimal(0)
    ) -> StepResult:
        """The ``write_off_position`` command (see the module docstring)."""
        symbol = symbol.upper()
        if confirm.strip().upper() != symbol:
            raise WriteOffRefused("confirm must repeat the symbol")
        if proceeds < 0:
            raise WriteOffRefused("proceeds must not be negative")
        position_id = await self._open_position(symbol)
        worker = self.worker
        async with worker.orders.lock_for(position_id):
            if not self.runtime.may_mutate():
                raise WriteOffRefused("no execution lock")
            position = await self._position(position_id)
            if position.status == PositionStatus.CLOSED:
                raise NoSuchPosition(f"no open position in {symbol}")
            try:
                status = await worker.not_trading(position, fresh=True)
            except StatusUnknown as exc:
                raise WriteOffRefused(f"{exc}; try again") from exc
            if status is None:
                raise WriteOffRefused(
                    f"{symbol} trades: sell it with close_position instead"
                )
            unproven = await worker.protector._open_attempts(position, tuple(Role))
            if unproven:
                raise WriteOffRefused(
                    f"{len(unproven)} order(s) of the position have an unproven "
                    "outcome; reconcile first"
                )
            await self._no_open_orders(symbol)
            if not await self.runtime.confirm_lead():
                raise WriteOffRefused("no execution lock")
            logger.warning(
                "%s written off: quantity %s, proceeds %s",
                symbol,
                position.quantity,
                proceeds,
            )
            return await worker.protector._finalize(
                position_id,
                ExitReason.DELISTED,
                ExitMechanism.WRITE_OFF,
                proceeds=proceeds,
            )

    async def _no_open_orders(self, symbol: str) -> None:
        trade = self.runtime.trade
        if trade is None:
            raise WriteOffRefused("no trade client")
        try:
            orders = (await trade.open_orders(symbol=symbol)).data
        except TradeCallError as error:
            if error.exchange_code == INVALID_SYMBOL:
                return  # the symbol is gone, and its orders with it
            raise WriteOffRefused(
                f"open orders of {symbol} not readable: {error.message}"
            ) from error
        if orders:
            raise WriteOffRefused(
                f"{len(orders)} open order(s) on {symbol}: cancel them first"
            )

    async def _open_position(self, symbol: str) -> uuid.UUID:
        context = self.runtime.context
        if context is None:
            raise NoSuchPosition("execution has no context")
        async with self.runtime.session() as session:
            position_id = await session.scalar(
                select(Position.id).where(
                    Position.account == context.account,
                    Position.network == context.network,
                    Position.symbol == symbol,
                    Position.status != PositionStatus.CLOSED,
                )
            )
        if position_id is None:
            raise NoSuchPosition(f"no open position in {symbol}")
        return position_id

    async def _position(self, position_id: uuid.UUID) -> Position:
        async with self.runtime.session() as session:
            position = await session.get(Position, position_id)
            assert position is not None
            return position


__all__ = ["Exits", "NoSuchPosition", "Step"]
