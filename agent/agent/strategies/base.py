"""Strategy contract — the *core* of the agent.

A strategy **is** the agent's decision core: it analyses market data and emits
signals. Everything else in ``agent`` (websocket streams, REST access, lifecycle,
heartbeat) exists to feed the strategy and to carry out its signals.

A concrete strategy subclasses :class:`Strategy`, declares a unique ``name`` and
is registered with the :func:`strategy` decorator. Strategies never build the
exchange layer themselves: they receive ready-to-use :class:`StrategyServices`.
"""

from __future__ import annotations

import abc
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, Literal

from agent.exchange.rest import ExchangeRest
from agent.exchange.streams import MarketDataStreams

SignalType = Literal["ENTRY", "EXIT", "HOLD"]


@dataclass(frozen=True)
class Signal:
    """A high-level decision emitted by a strategy.

    This is the strategy's *output contract*, not an exchange model — exchange
    models come from ``binance_sdk_spot`` and never appear here. The strategy
    deliberately stops at intent (``D-SIGNAL-ONLY``); sizing, order type and
    protection are downstream concerns.
    """

    type: SignalType
    symbol: str
    emitted_at: datetime
    reason: str | None = None
    # ENTRY
    epoch_id: str | None = None
    entry_ref: float | None = None
    swing_low_ref: float | None = None
    tp_rule: str | None = None
    valid_until: datetime | None = None
    # EXIT
    exit_reason: str | None = None
    # Full per-screen provenance (bools, indicator values, timestamps).
    provenance: dict[str, Any] = field(default_factory=dict)


SignalSink = Callable[[Signal], Awaitable[None]]


@dataclass
class StrategyServices:
    """Everything a strategy may use, wired by the engine.

    All exchange access goes through these SDK-backed adapters; the strategy
    never instantiates ``binance_sdk_spot`` clients itself.
    """

    network: str
    symbols: list[str]
    rest: ExchangeRest
    streams: MarketDataStreams
    emit: SignalSink


class Strategy(abc.ABC):
    """Base class for trading strategies."""

    #: Registry key; must be unique across the ``strategies`` package.
    name: ClassVar[str] = ""

    def __init__(self, params: dict[str, Any]) -> None:
        self._params = params
        self._services: StrategyServices | None = None

    @property
    def services(self) -> StrategyServices:
        if self._services is None:
            raise RuntimeError("strategy is not started")
        return self._services

    @property
    def params(self) -> dict[str, Any]:
        return dict(self._params)

    @abc.abstractmethod
    async def on_start(self, services: StrategyServices) -> None:
        """Subscribe to data and bootstrap the strategy's state."""

    @abc.abstractmethod
    async def on_stop(self) -> None:
        """Release resources (unsubscribe, flush state)."""

    async def on_kline(self, symbol: str, interval: str, kline: Any) -> None:  # noqa: B027
        """Handle a kline update (SDK ``KlineResponse``) for ``symbol``."""

    async def on_price(self, symbol: str, price: float) -> None:  # noqa: B027
        """Handle a live price tick for ``symbol``."""


__all__ = ["Signal", "SignalSink", "SignalType", "Strategy", "StrategyServices"]
