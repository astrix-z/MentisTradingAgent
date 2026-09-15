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
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar, Literal
from uuid import uuid4

from pydantic import BaseModel

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
    entry_ref: Decimal | None = None
    swing_low_ref: Decimal | None = None
    tp_rule: str | None = None
    valid_until: datetime | None = None
    # EXIT
    exit_reason: str | None = None
    # Full per-screen provenance (bools, indicator values, timestamps).
    provenance: dict[str, Any] = field(default_factory=dict)
    signal_id: str = field(default_factory=lambda: str(uuid4()))
    side: Literal["LONG"] = "LONG"
    config_version: str | None = None
    market_window_versions: dict[str, str] = field(default_factory=dict)

    def transport(self) -> dict[str, Any]:
        """Full JSON-ready signal, without the event envelope (e.g. ``kind``).

        All fields are present, including nulls. Decimal uses str (never float),
        datetime uses ISO 8601, and nested mappings/sequences follow the same
        rules. A retry must reuse this signal_id; sinks must deduplicate by it
        and raise on failed delivery. Exactly-once delivery cannot be inferred
        from a sink exception after an ambiguous acknowledgement.
        """
        return {key: transport_value(value) for key, value in asdict(self).items()}


def transport_value(value: Any) -> Any:
    """Convert strategy values at the JSON boundary, preserving decimal digits."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: transport_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [transport_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported signal transport value: {type(value).__name__}")


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
    """Base class for trading strategies.

    A strategy also *owns its configuration contract*: the pydantic model of
    its ``params``, the cross-field rules that model cannot express in JSON
    Schema, and the value tables those rules index. The agent publishes all of
    it to the backend (see :mod:`agent.protocol`) so that the API and the UI
    validate and render against the strategy's own declaration rather than a
    copy of it.
    """

    #: Registry key; must be unique across the ``strategies`` package.
    name: ClassVar[str] = ""

    #: Human-readable name for the UI; falls back to the registry key.
    label: ClassVar[str] = ""

    #: Pydantic model validating this strategy's ``params``. ``None`` means the
    #: strategy takes no settings; its JSON Schema is published as-is.
    settings_model: ClassVar[type[BaseModel] | None] = None

    #: Cross-field rules the settings schema cannot express, in the constraint
    #: grammar documented in :mod:`agent.protocol`. Evaluated against ``params``
    #: by every consumer (backend before accepting a Start, UI before sending
    #: one), so the rule text is never mirrored by hand.
    settings_constraints: ClassVar[list[dict[str, Any]]] = []

    #: Named value tables referenced by :attr:`settings_constraints` via
    #: ``{"lookup": "<name>", "field": "<field>"}``. Declared by the strategy
    #: because only it knows which exchange/SDK values back its fields. A field
    #: marked ``{"ui": {"widget": "interval"}}`` takes its allowed values from
    #: the ``intervals_ms`` table.
    settings_lookups: ClassVar[dict[str, dict[str, int]]] = {}

    def __init__(self, params: dict[str, Any]) -> None:
        self._params = deepcopy(params)
        self._services: StrategyServices | None = None

    @property
    def services(self) -> StrategyServices:
        if self._services is None:
            raise RuntimeError("strategy is not started")
        return self._services

    @property
    def params(self) -> dict[str, Any]:
        return deepcopy(self._params)

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
