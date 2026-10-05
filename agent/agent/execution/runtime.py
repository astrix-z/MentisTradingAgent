"""ExecutionRuntime: the agent-level owner of real execution (plan §4).

Created at boot next to the :class:`~agent.engine.Engine` and alive for the
whole process — independent of ``agent:runtime.running`` and of Start/Stop. A
strategy only feeds it signals; Stop closes admission and nothing else, so
positions, their protection and their monitoring keep going (Q3).

What it owns here:

* the **execution context** ``(account, network)``: the account is the
  exchange ``uid`` of the credentials, checked, never the API key. At boot the
  context is taken from unfinished intents, attempts and positions in the
  database — not from the desired network in Redis, which a rejected Start may
  already have changed;
* the **single-writer lock** of that context (:mod:`agent.execution.lock`) and
  the rule that nothing is mutated without it, or before the leadership hooks
  (reconciliation) ran after taking it;
* **admission state**: settings (enabled / off), Stop, pause, and every
  condition that keeps entries closed, reported through the heartbeat;
* the **key preflight** (``canTrade`` and ``SPOT``), the **clock check** and
  the **BNB low** warning.

Context switches (another network or another account) are refused while the
current context still has open, pending or UNKNOWN work, and they are checked
*before* the running strategy is torn down.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, union_all
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agent.exchange.trade import RECV_WINDOW_MS, Outcome, TradeCallError, TradeClient
from agent.execution.lock import LeaderLock
from agent.execution.models import (
    INTENT_HOLDING_STATES,
    AttemptState,
    ExecutionIntent,
    OrderAttempt,
    Position,
    PositionStatus,
    SymbolCooldown,
)
from agent.execution.settings import ExecutionSettings
from agent.strategies.base import PositionEvent, PositionsSnapshot

logger = logging.getLogger(__name__)

#: ``error_type`` of a send refused by :meth:`ExecutionRuntime.guard_mutation`:
#: nothing reached the exchange.
NOT_LEADER = "NotLeader"

WATCHDOG_SECONDS = 5.0
CLOCK_CHECK_EVERY = timedelta(hours=1)
ACCOUNT_CHECK_EVERY = timedelta(minutes=5)
#: Past this offset admission closes: signed requests would start failing.
CLOCK_SKEW_LIMIT = timedelta(milliseconds=RECV_WINDOW_MS // 2)
CLOCK_SKEW_WARN = timedelta(seconds=1)
#: Alerts for exchange state no position or attempt explains (reconcile.py).
_UNRESOLVED = ("divergence:", "quarantine:")
#: Reconcile findings about one account/network, dropped when it is left.
_CONTEXT_PREFIXES = (*_UNRESOLVED, "foreign_trades:")

LeadershipHook = Callable[["ExecutionContext"], Awaitable[None]]


class ContextRefused(ValueError):
    """A Start would move execution away from unfinished work."""


@dataclass(frozen=True)
class ExecutionContext:
    account: str
    network: str


def _now() -> datetime:
    return datetime.now(UTC)


def connect_trade(network: str) -> TradeClient:
    if network == "live":
        return TradeClient.connect("live")
    if network == "testnet":
        return TradeClient.connect("testnet")
    raise ValueError(f"unknown network: {network!r}")


class ExecutionRuntime:
    def __init__(
        self,
        *,
        db: AsyncEngine | None,
        db_url: str | None,
        schema_ready: bool,
        trade_factory: Callable[[str], TradeClient] = connect_trade,
        lock_factory: Callable[[str, str, str], LeaderLock] = LeaderLock,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._db = db
        self._db_url = db_url
        self.schema_ready = schema_ready and db is not None and db_url is not None
        self._trade_factory = trade_factory
        self._lock_factory = lock_factory
        self._clock = clock
        self._sessions = (
            async_sessionmaker(db, expire_on_commit=False) if db is not None else None
        )
        self.settings = ExecutionSettings()
        self.context: ExecutionContext | None = None
        self.trade: TradeClient | None = None
        self.lock: LeaderLock | None = None
        self.strategy: str | None = None
        self.admission_open = False
        self.paused_by_owner = False
        #: True once the leadership hooks ran after the lock was (re)taken.
        self.reconciled = False
        self.alerts: dict[str, str] = {}
        self.open_positions = 0
        self._hooks: list[LeadershipHook] = []
        self._watches: list[LeadershipHook] = []
        self._watchdog: asyncio.Task[None] | None = None
        self._context_lock = asyncio.Lock()
        self._last_clock_check: datetime | None = None
        self._last_account_check: datetime | None = None
        self._restore_pending = False
        #: Delivers committed position changes to the running strategy.
        self.feedback: Callable[[PositionEvent, bool], Awaitable[None]] | None = None
        #: Wakes the admission worker when a new ENTRY is in the inbox.
        self._admission_wake: Callable[[], None] | None = None
        #: Sells a symbol's open position at market (``close_position``).
        self.close_position: Callable[[str], Awaitable[Any]] | None = None
        #: Releases an entry proven never placed (``release_entry``).
        self.release_entry: Callable[[str], Awaitable[Any]] | None = None

    # --- wiring ------------------------------------------------------------

    def on_leadership(self, hook: LeadershipHook) -> None:
        """Run ``hook`` each time the lock is taken, before any mutation."""
        self._hooks.append(hook)

    def on_watch(self, check: LeadershipHook) -> None:
        """Run ``check`` on every watchdog round while this process leads.

        For safety checks that must not wait on the execution round, which
        can be blocked on an exchange call (the protection deadline).
        """
        self._watches.append(check)

    def session(self) -> AsyncSession:
        if self._sessions is None:
            raise RuntimeError("execution has no database")
        return self._sessions()

    # --- lifecycle -----------------------------------------------------------

    async def boot(self) -> None:
        """Restore the context of unfinished work and start the watchdog.

        The restore needs the exchange (to identify the account); if it is not
        reachable at boot, the watchdog keeps retrying and mutations stay off.
        """
        if not self.schema_ready:
            self.alerts["schema"] = "agent database not migrated; real execution closed"
            logger.error(self.alerts["schema"])
            return
        self._restore_pending = True
        try:
            await self.tick()
        except Exception:  # noqa: BLE001 - the watchdog retries; boot must finish
            logger.exception("first execution round failed; the watchdog retries")
        self._watchdog = asyncio.create_task(self._watch())

    async def _restore(self) -> None:
        try:
            restored = await self._unfinished_context()
            if restored is not None and self.context is None:
                logger.info(
                    "restoring execution context account=%s network=%s",
                    restored.account,
                    restored.network,
                )
                await self._enter(restored.network, expected_account=restored.account)
        except (SQLAlchemyError, TradeCallError, ValueError) as exc:
            self.alerts["restore"] = f"cannot restore execution context: {exc}"
            logger.warning(self.alerts["restore"])
            return
        self._restore_pending = False
        self.alerts.pop("restore", None)

    async def shutdown(self) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()
            try:
                await self._watchdog
            except asyncio.CancelledError:
                pass
            self._watchdog = None
        if self.lock is not None:
            await self.lock.dispose()
            self.lock = None

    # --- Start / Stop ----------------------------------------------------------

    async def prepare_start(
        self, *, network: str, settings: ExecutionSettings, params: Mapping[str, Any]
    ) -> None:
        """Validate a Start against execution before anything is torn down.

        Raises ``ValueError`` (the Start is refused, the current run stays).
        """
        if settings.enabled:
            quote = str(params.get("quote_asset") or "USDT")
            if quote != "USDT":
                raise ValueError(
                    f"real execution supports USDT only; quote_asset={quote} "
                    "can run with execution disabled"
                )
            if not self.schema_ready:
                raise ValueError("agent database is not ready; real execution refused")
        async with self._context_lock:
            if self._restore_pending:
                await self._restore()
                if self._restore_pending:
                    raise ValueError(
                        "execution state could not be restored yet; Start refused"
                    )
            current = self.context
            if current is None and self.schema_ready:
                current = await self._unfinished_context()
            busy = current is not None and await self.unfinished(current) > 0
            if busy and current is not None and current.network != network:
                raise ContextRefused(
                    f"execution has open, pending or UNKNOWN work on {current.network} "
                    f"(account {current.account}); a Start on {network} is refused"
                )
            expected = current.account if busy and current is not None else None
            if self.context is not None and self.context.network != network:
                await self._leave()
            if self.context is None and (self.schema_ready or settings.enabled):
                await self._enter(network, expected_account=expected)
            elif expected is not None and self.trade is not None:
                await self._verify_account(expected)
            self.settings = settings

    def open_admission(self, strategy: str) -> None:
        self.strategy = strategy
        self.admission_open = True

    def close_admission(self) -> None:
        """Stop of the strategy: no new entries; everything else continues."""
        self.admission_open = False

    def pause(self) -> None:
        self.paused_by_owner = True

    def resume(self) -> None:
        self.paused_by_owner = False

    def set_admission_wake(self, wake: Callable[[], None]) -> None:
        self._admission_wake = wake

    def wake_admission(self) -> None:
        if self._admission_wake is not None:
            self._admission_wake()

    # --- strategy feedback (plan §4.4) -------------------------------------------

    async def positions_snapshot(self) -> PositionsSnapshot:
        """Open positions and running cooldowns of the current context."""
        if self.context is None or self._sessions is None:
            return PositionsSnapshot()
        now = self._clock()
        async with self.session() as session:
            symbols = (
                await session.scalars(
                    select(Position.symbol).where(
                        Position.account == self.context.account,
                        Position.network == self.context.network,
                        Position.status != PositionStatus.CLOSED,
                    )
                )
            ).all()
            cooldowns = (
                await session.execute(
                    select(SymbolCooldown.symbol, SymbolCooldown.cooldown_until).where(
                        SymbolCooldown.account == self.context.account,
                        SymbolCooldown.network == self.context.network,
                        SymbolCooldown.cooldown_until > now,
                    )
                )
            ).all()
        return PositionsSnapshot(
            open_symbols=frozenset(symbols),
            cooldown_until={symbol: until for symbol, until in cooldowns},
        )

    async def notify(self, event: PositionEvent, *, closed: bool) -> None:
        """Tell the strategy about a committed change; never raises.

        A lost notification is repaired by the snapshot the next run restores.
        """
        if self.feedback is None:
            return
        try:
            await self.feedback(event, closed)
        except Exception:  # noqa: BLE001 - feedback must not undo a commit
            logger.exception("strategy feedback failed for %s", event.symbol)

    # --- questions other components ask ----------------------------------------

    def may_mutate(self) -> bool:
        """May an order be sent or cancelled right now (any kind)?"""
        return (
            self.lock is not None
            and self.lock.held
            and self.reconciled
            and self.trade is not None
        )

    def guard_mutation(self) -> None:
        """Last look before an order is placed or cancelled, in the sending thread.

        Only in-memory state, no awaits: a lock loss the watchdog or a
        :meth:`confirm_lead` noticed while the request waited for the budget
        or a worker still stops it. Not :meth:`admission_block`: a Stop or a
        Pause must not stop protecting and closing what is open.
        """
        if not self.may_mutate():
            raise TradeCallError(
                Outcome.REJECTED, "no execution lock", error_type=NOT_LEADER
            )

    async def confirm_lead(self) -> bool:
        """Ask the database, not the flag, whether this process still leads.

        A killed lock session is only noticed when the connection is used;
        callers about to start a new mutation after awaited work ask here.
        """
        if self.lock is None or not self.may_mutate():
            return False
        if await self.lock.check():
            return True
        self.reconciled = False
        self.alerts["lock"] = "execution lock lost; mutations stopped"
        return False

    def admission_block(self) -> str | None:
        """Why no new ENTRY may be executed now, or ``None`` if one may."""
        if not self.settings.enabled:
            return "execution_off"
        if not self.admission_open:
            return "strategy_stopped"
        if self.paused_by_owner:
            return "paused_by_owner"
        if not self.may_mutate():
            return "no_execution_lock"
        if self.trade is not None and self.trade.budget.banned():
            return "ip_banned"
        # One atomic copy: the sending thread calls this too (orders.py).
        alerts = tuple(self.alerts)
        blocking = {"schema", "database", "key", "clock", "account", "lock"} & set(
            alerts
        )
        if blocking:
            return sorted(blocking)[0]
        # Unexplained exchange state (reconcile.py): nothing new until resolved.
        if any(key.startswith(_UNRESOLVED) for key in alerts):
            return "divergence"
        return None

    # --- heartbeat ---------------------------------------------------------------

    def heartbeat(self) -> dict[str, Any]:
        block = self.admission_block()
        if not self.settings.enabled:
            mode = "off"
        elif block is None:
            mode = "enabled"
        else:
            mode = f"paused({block})"
        return {
            "mode": mode,
            "account": self.context.account if self.context else None,
            "network": self.context.network if self.context else None,
            "leader": bool(self.lock and self.lock.held),
            "open_positions": self.open_positions,
            "alerts": dict(sorted(self.alerts.items())),
        }

    def message(self) -> str | None:
        """Alerts for the heartbeat ``message``; ``None`` when all is well."""
        if not self.alerts:
            return None
        return "execution: " + "; ".join(
            f"{name}: {text}" for name, text in sorted(self.alerts.items())
        )

    # --- database questions ------------------------------------------------------

    async def _unfinished_context(self) -> ExecutionContext | None:
        """The context that still has unfinished work, from the database."""
        rows = union_all(
            select(
                ExecutionIntent.account,
                ExecutionIntent.network,
                ExecutionIntent.updated_at.label("at"),
            ).where(ExecutionIntent.state.in_(list(INTENT_HOLDING_STATES))),
            select(
                OrderAttempt.account,
                OrderAttempt.network,
                OrderAttempt.created_at.label("at"),
            ).where(
                OrderAttempt.state.in_(
                    [AttemptState.PREPARED, AttemptState.SENT, AttemptState.UNKNOWN]
                )
            ),
            select(
                Position.account, Position.network, Position.updated_at.label("at")
            ).where(Position.status != PositionStatus.CLOSED),
        ).subquery()
        async with self.session() as session:
            found = (
                await session.execute(
                    select(rows.c.account, rows.c.network)
                    .group_by(rows.c.account, rows.c.network)
                    .order_by(func.max(rows.c.at).desc())
                )
            ).all()
        if not found:
            return None
        if len(found) > 1:
            others = ", ".join(f"{a}/{n}" for a, n in found[1:])
            self.alerts["contexts"] = f"unfinished work in several contexts: {others}"
        return ExecutionContext(account=found[0][0], network=found[0][1])

    async def unfinished(self, context: ExecutionContext) -> int:
        """Open positions + holding intents + attempts without a proven outcome."""
        if self._sessions is None:
            return 0
        where_ctx = (context.account, context.network)
        async with self.session() as session:
            positions = await session.scalar(
                select(func.count()).where(
                    Position.account == where_ctx[0],
                    Position.network == where_ctx[1],
                    Position.status != PositionStatus.CLOSED,
                )
            )
            intents = await session.scalar(
                select(func.count()).where(
                    ExecutionIntent.account == where_ctx[0],
                    ExecutionIntent.network == where_ctx[1],
                    ExecutionIntent.state.in_(list(INTENT_HOLDING_STATES)),
                )
            )
            attempts = await session.scalar(
                select(func.count()).where(
                    OrderAttempt.account == where_ctx[0],
                    OrderAttempt.network == where_ctx[1],
                    OrderAttempt.state.in_(
                        [AttemptState.PREPARED, AttemptState.SENT, AttemptState.UNKNOWN]
                    ),
                )
            )
        self.open_positions = int(positions or 0)
        return int(positions or 0) + int(intents or 0) + int(attempts or 0)

    # --- context ------------------------------------------------------------------

    async def _enter(self, network: str, *, expected_account: str | None) -> None:
        """Connect to ``network``, identify the account, take the lock."""
        trade = self._trade_factory(network)
        account = (await trade.get_account()).data
        uid = str(account.uid) if account.uid is not None else None
        if uid is None:
            raise ValueError(f"exchange returned no account uid on {network}")
        if expected_account is not None and uid != expected_account:
            raise ContextRefused(
                f"credentials for {network} belong to account {uid}, but unfinished "
                f"work belongs to {expected_account}"
            )
        trade.mutation_guard = self.guard_mutation
        self.trade = trade
        self.context = ExecutionContext(account=uid, network=network)
        self.alerts.pop("account", None)
        self._check_key(account)
        self._check_bnb(account)
        self._last_account_check = self._clock()
        await self._check_clock()
        if self.schema_ready and self._db_url is not None:
            self.lock = self._lock_factory(self._db_url, uid, network)
            await self._take_lead()

    async def _leave(self) -> None:
        if self.lock is not None:
            await self.lock.dispose()
        self.lock = None
        self.trade = None
        self.context = None
        self.reconciled = False
        self.open_positions = 0
        for name in ("lock", "key", "clock", "bnb", "account", "reconcile"):
            self.alerts.pop(name, None)
        for key in [k for k in self.alerts if k.startswith(_CONTEXT_PREFIXES)]:
            del self.alerts[key]

    async def _verify_account(self, expected: str) -> None:
        assert self.trade is not None
        account = (await self.trade.get_account()).data
        if str(account.uid) != expected:
            raise ContextRefused(
                f"credentials now belong to account {account.uid}, but unfinished "
                f"work belongs to {expected}"
            )

    async def _take_lead(self) -> None:
        """Take the lock, then reconcile before anything may be mutated.

        A failed reconciliation keeps the lock (nobody else may mutate either)
        and leaves ``reconciled`` false; the next watchdog round retries it.
        """
        assert self.lock is not None and self.context is not None
        self.reconciled = False
        if not self.lock.held:
            if not await self.lock.acquire():
                self.alerts["lock"] = (
                    f"another process holds execution for {self.context.account}/"
                    f"{self.context.network}"
                )
                return
            self.alerts.pop("lock", None)
        try:
            for hook in self._hooks:
                await hook(self.context)
        except Exception as exc:  # noqa: BLE001 - any failure: retry, stay closed
            self.alerts["reconcile"] = (
                f"reconciliation before execution failed, retrying: {exc}"
            )
            logger.warning(self.alerts["reconcile"])
            return
        self.alerts.pop("reconcile", None)
        self.reconciled = True
        await self.unfinished(self.context)

    # --- periodic checks ------------------------------------------------------------

    def _check_key(self, account: Any) -> None:
        permissions = set(account.permissions or [])
        if account.can_trade is not True or "SPOT" not in permissions:
            self.alerts["key"] = (
                f"API key cannot trade spot (canTrade={account.can_trade}, "
                f"permissions={sorted(permissions)})"
            )
        else:
            self.alerts.pop("key", None)

    def _check_bnb(self, account: Any) -> None:
        free = Decimal(0)
        for row in account.balances or []:
            if row.asset == "BNB":
                free = Decimal(row.free or "0") + Decimal(row.locked or "0")
        if free < self.settings.bnb_low_threshold:
            self.alerts["bnb"] = (
                f"BNB low ({free}); fees will be paid in the traded asset"
            )
        else:
            self.alerts.pop("bnb", None)

    async def _check_clock(self) -> None:
        if self.trade is None:
            return
        try:
            offset = await self.trade.clock_offset()
        except TradeCallError as exc:
            logger.warning("clock check failed: %s", exc)
            return
        self._last_clock_check = self._clock()
        if abs(offset) > CLOCK_SKEW_LIMIT:
            self.alerts["clock"] = f"clock off by {offset.total_seconds():.3f}s"
        elif abs(offset) > CLOCK_SKEW_WARN:
            self.alerts["clock_warning"] = f"clock off by {offset.total_seconds():.3f}s"
            self.alerts.pop("clock", None)
        else:
            self.alerts.pop("clock", None)
            self.alerts.pop("clock_warning", None)

    async def tick(self) -> None:
        """One watchdog round: lock health, re-lead, clock, account."""
        async with self._context_lock:
            if self._restore_pending:
                await self._restore()
            if self.lock is not None and self.context is not None:
                if self.lock.held and not await self.lock.check():
                    self.reconciled = False
                    self.alerts["lock"] = "execution lock lost; mutations stopped"
                if not self.lock.held or not self.reconciled:
                    await self._take_lead()
            now = self._clock()
            if self.trade is not None and (
                self._last_clock_check is None
                or now - self._last_clock_check >= CLOCK_CHECK_EVERY
            ):
                await self._check_clock()
            if self.trade is not None and (
                self._last_account_check is None
                or now - self._last_account_check >= ACCOUNT_CHECK_EVERY
            ):
                try:
                    account = (await self.trade.get_account()).data
                except TradeCallError as exc:
                    logger.warning("account check failed: %s", exc)
                else:
                    self._last_account_check = now
                    self._check_key(account)
                    self._check_bnb(account)
                    if self.context and str(account.uid) != self.context.account:
                        self.alerts["account"] = (
                            f"credentials switched to account {account.uid}"
                        )
            if self.context is not None and self._sessions is not None:
                try:
                    await self.unfinished(self.context)
                except SQLAlchemyError as exc:
                    self.alerts["database"] = f"cannot read execution state: {exc}"
                else:
                    self.alerts.pop("database", None)
            if self.context is not None and self.lock is not None and self.lock.held:
                for check in self._watches:
                    try:
                        await check(self.context)
                    except Exception:  # noqa: BLE001 - one check must not stop others
                        logger.exception("execution watch check failed")

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(WATCHDOG_SECONDS)
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 - the watchdog must survive anything
                logger.exception("execution watchdog round failed")


__all__ = [
    "ContextRefused",
    "ExecutionContext",
    "ExecutionRuntime",
    "LeadershipHook",
]
