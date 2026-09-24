"""``ExecutionOutcome``: what really happened to a position (plan §5).

Not a :class:`~agent.strategies.base.Signal`. A signal is the strategy's intent;
an outcome is the confirmed exchange result — fills, quantities, times — and is
produced only by execution. ``reason`` says why the position ended, ``mechanism``
how the exchange got there; they are separate because one reason can arrive
through several mechanisms (a stop hit on the exchange, or sold at market
because the price gapped through it).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from agent.strategies.base import transport_value


class ExitReason(StrEnum):
    STOP = "stop"
    TRAIL = "trail"
    TP = "tp"
    TIME = "time"
    MANUAL = "manual"
    EMERGENCY = "emergency"


class ExitMechanism(StrEnum):
    EXCHANGE_STOP = "exchange_stop"
    EXCHANGE_TP = "exchange_tp"
    MARKET_TRAIL = "market_trail"
    MARKET_TIME = "market_time"
    MARKET_GAP = "market_gap"
    MARKET_UNPROTECTED = "market_unprotected"
    MANUAL_CLOSE = "manual_close"
    GUARD = "guard"
    DUST = "dust"


@dataclass(frozen=True)
class ExecutionOutcome:
    """A closed position, as the exchange confirmed it."""

    position_id: str
    signal_id: str
    account: str
    network: str
    symbol: str
    strategy: str
    reason: ExitReason
    mechanism: ExitMechanism
    opened_at: datetime
    closed_at: datetime
    quantity: Decimal
    entry_vwap: Decimal
    exit_vwap: Decimal | None
    realized_pnl: Decimal
    commission_quote: Decimal
    order_ids: tuple[str, ...]
    exit_policy: dict[str, Any]

    @property
    def losing(self) -> bool:
        """A losing trade for the daily limit: net result below zero (Q19)."""
        return self.realized_pnl < 0

    def transport(self) -> dict[str, Any]:
        return {key: transport_value(value) for key, value in asdict(self).items()}


__all__ = ["ExecutionOutcome", "ExitMechanism", "ExitReason"]
