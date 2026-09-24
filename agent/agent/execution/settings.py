"""Execution settings (plan §4.3, §7): what the owner configures for real orders.

Published in the catalog next to the strategy schemas (``execution`` section,
see :mod:`agent.protocol`), validated by the backend with the same compiled
schema before a Start reaches Redis, stored in the ``agent:runtime`` snapshot
with the Start, and validated again here. ``enabled=false`` is observe-only:
signals are recorded, nothing is sent.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agent.execution.sizing import SizingParams


class ExecutionSettings(BaseModel):
    model_config = ConfigDict(frozen=True, allow_inf_nan=False, extra="forbid")

    enabled: bool = Field(
        default=False,
        title="Real execution",
        description="Place real orders on ENTRY. Off: signals are only recorded.",
    )
    max_slippage_pct: Decimal = Field(
        default=Decimal("0.3"), gt=0, le=5, title="Max entry slippage %"
    )
    unprotected_deadline_s: int = Field(
        default=30, ge=5, le=300, title="Protection deadline (s)"
    )
    daily_losing_trades_limit: int = Field(
        default=5, ge=1, le=100, title="Losing trades per UTC day"
    )
    max_symbols: int = Field(default=3, ge=1, le=10, title="Max symbols")
    slot_quote: Decimal = Field(
        default=Decimal(7), gt=0, title="Minimum per slot (USDT)"
    )
    position_share: Decimal = Field(
        default=Decimal("0.9"), gt=0, le=1, title="Deposit share in positions"
    )
    stop_limit_gap_pct: Decimal = Field(
        default=Decimal("0.5"), gt=0, lt=10, title="Stop-limit gap %"
    )
    quote_max_fetch_age_s: int = Field(
        default=5, ge=1, le=60, title="Max quote fetch age (s)"
    )
    quote_max_trade_age_s: int = Field(
        default=60, ge=1, le=900, title="Max last-trade age (s)"
    )
    unknown_escalate_s: int = Field(
        default=120, ge=10, le=3600, title="Escalate unresolved UNKNOWN after (s)"
    )
    reconcile_interval_s: int = Field(
        default=60, ge=10, le=3600, title="Reconcile interval (s)"
    )
    guard_grace_s: int = Field(default=5, ge=1, le=120, title="Guard grace (s)")
    bnb_low_threshold: Decimal = Field(
        default=Decimal("0.001"), ge=0, title="BNB low warning below"
    )

    @classmethod
    def from_payload(cls, payload: Any) -> ExecutionSettings:
        """Validate a Start's ``execution`` object; absent means defaults."""
        if payload is None:
            return cls()
        if not isinstance(payload, dict):
            raise ValueError("execution settings must be an object")
        return cls.model_validate(payload)

    def sizing(self) -> SizingParams:
        return SizingParams(
            max_symbols=self.max_symbols,
            slot_quote=self.slot_quote,
            position_share=self.position_share,
            max_slippage_pct=self.max_slippage_pct,
            stop_limit_gap_pct=self.stop_limit_gap_pct,
            max_trade_age=timedelta(seconds=self.quote_max_trade_age_s),
        )


__all__ = ["ExecutionSettings"]
