"""One request-weight budget per exchange IP, shared by every REST caller.

Binance limits request weight per IP, not per SDK client, so the trade client,
reconciliation and market data draw from the same pool (plan §6.9). Lower
priorities stop earlier, which leaves the headroom to protection and exits:

========== ============================ =========================
priority   who                          may use up to (of limit)
========== ============================ =========================
PROTECT    protection, exits, cancels   100%
RECONCILE  order/trade reconciliation   90%
ENTRY      new entries                  75%
MARKET     market data, universe        60%
========== ============================ =========================

A 429 pauses everyone until ``Retry-After``; nothing is retried behind the
caller's back. A 418 is a ban: every request fails fast with ``BANNED`` until it
ends, and admission reads :attr:`RequestBudget.banned_until` to stay closed.
Protection is not promised during a ban — the exchange will not take it.

Usage is estimated from the weights we spend in the last minute and corrected
by the ``X-MBX-USED-WEIGHT-1M`` header whenever a response carries one.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from enum import IntEnum
from typing import Any

from binance_common.models import RateLimit

#: Default IP weight limit per minute on Binance spot (exchangeInfo rateLimits).
DEFAULT_WEIGHT_LIMIT = 6000
_WINDOW_SECONDS = 60.0
_DEFAULT_PAUSE_SECONDS = 60.0
_DEFAULT_BAN_SECONDS = 300.0
_POLL_SECONDS = 0.25


class Priority(IntEnum):
    PROTECT = 0
    RECONCILE = 1
    ENTRY = 2
    MARKET = 3


_CEILING = {
    Priority.PROTECT: 1.0,
    Priority.RECONCILE: 0.9,
    Priority.ENTRY: 0.75,
    Priority.MARKET: 0.6,
}


class BudgetExhausted(RuntimeError):
    """The request could not be admitted within its allowed wait."""

    def __init__(self, reason: str, *, banned: bool = False) -> None:
        super().__init__(reason)
        self.banned = banned


class RequestBudget:
    """Admission control for REST weight on one exchange host."""

    def __init__(
        self,
        limit: int = DEFAULT_WEIGHT_LIMIT,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self.limit = limit
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._spent: deque[tuple[float, int]] = deque()
        self._reported: tuple[float, int] | None = None
        self.paused_until = 0.0
        self.banned_until = 0.0

    # --- state -------------------------------------------------------------

    def used(self) -> int:
        """Estimated weight used in the current minute."""
        now = self._clock()
        with self._lock:
            while self._spent and now - self._spent[0][0] >= _WINDOW_SECONDS:
                self._spent.popleft()
            local = sum(weight for _, weight in self._spent)
            if self._reported is None or now - self._reported[0] >= _WINDOW_SECONDS:
                return local
            at, count = self._reported
            after = sum(weight for stamp, weight in self._spent if stamp > at)
            return max(local, count + after)

    def banned(self) -> bool:
        return self._clock() < self.banned_until

    def paused(self) -> bool:
        return self._clock() < self.paused_until

    # --- admission ---------------------------------------------------------

    async def acquire(
        self, priority: Priority, weight: int, *, max_wait: float | None = None
    ) -> None:
        """Wait until ``weight`` fits under ``priority``'s ceiling, then spend it.

        ``max_wait=None`` waits as long as needed; a number bounds the wait and
        raises :class:`BudgetExhausted` past it. A ban always fails at once.
        """
        started = self._clock()
        while True:
            now = self._clock()
            if now < self.banned_until:
                raise BudgetExhausted("IP banned by the exchange (418)", banned=True)
            if now >= self.paused_until and self._fits(priority, weight):
                with self._lock:
                    self._spent.append((now, weight))
                return
            if max_wait is not None and now - started >= max_wait:
                reason = "paused after 429" if now < self.paused_until else "budget"
                raise BudgetExhausted(f"request weight not available ({reason})")
            await self._sleep(_POLL_SECONDS)

    def _fits(self, priority: Priority, weight: int) -> bool:
        return self.used() + weight <= self.limit * _CEILING[priority]

    # --- feedback from responses ------------------------------------------

    def observe(self, rate_limits: Iterable[RateLimit]) -> None:
        """Take the exchange's own count from a response's rate-limit headers."""
        for limit in rate_limits:
            if (
                limit.rateLimitType == "REQUEST_WEIGHT"
                and limit.interval == "MINUTE"
                and limit.intervalNum == 1
            ):
                with self._lock:
                    self._reported = (self._clock(), limit.count)

    def on_rate_limited(self, retry_after: int | None) -> None:
        seconds = float(retry_after) if retry_after else _DEFAULT_PAUSE_SECONDS
        self.paused_until = max(self.paused_until, self._clock() + seconds)

    def on_banned(self, retry_after: int | None) -> None:
        seconds = float(retry_after) if retry_after else _DEFAULT_BAN_SECONDS
        self.banned_until = max(self.banned_until, self._clock() + seconds)


_BUDGETS: dict[str, RequestBudget] = {}


def budget_for(network: str) -> RequestBudget:
    """The process-wide budget for ``network``'s REST host."""
    budget = _BUDGETS.get(network)
    if budget is None:
        budget = _BUDGETS[network] = RequestBudget()
    return budget


#: Request weight per SDK method (Binance REST docs); unlisted methods cost 5.
WEIGHTS: dict[str, int] = {
    "new_order": 1,
    "order_test": 1,
    "order_list_oco": 1,
    "delete_order": 1,
    "delete_order_list": 1,
    "get_order": 4,
    "get_order_list": 4,
    "get_open_orders": 6,
    "open_order_list": 6,
    "my_trades": 20,
    "get_account": 20,
    "exchange_info": 20,
    "klines": 2,
    "get_trades": 25,
    "avg_price": 2,
    "reference_price": 2,
    "ticker_book_ticker": 2,
    "ticker_price": 2,
    "ticker24hr": 80,
    "time": 1,
}


def weight_of(method_name: str) -> int:
    return WEIGHTS.get(method_name, 5)


__all__ = [
    "DEFAULT_WEIGHT_LIMIT",
    "BudgetExhausted",
    "Priority",
    "RequestBudget",
    "budget_for",
    "weight_of",
]
