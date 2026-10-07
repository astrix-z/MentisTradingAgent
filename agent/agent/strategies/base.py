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
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar, Literal
from uuid import UUID, uuid4, uuid5

from pydantic import BaseModel

from agent.exchange.rest import ExchangeRest
from agent.exchange.streams import MarketStreams

SignalType = Literal["ENTRY", "EXIT", "HOLD"]

#: Namespace of deterministic ENTRY ids. Fixed forever: changing it would give
#: every past epoch a new id and let a restart re-admit it.
SIGNAL_ID_NAMESPACE = UUID("09ad640e-37e0-5abd-bdcd-f541859c2909")


def entry_signal_id(
    *, account_uid: str, network: str, strategy: str, symbol: str, epoch_id: str
) -> str:
    """The one id an ENTRY epoch has in an execution context (plan §5).

    The same epoch after a restart or a key rotation maps to the same id, so the
    inbox rejects the replay; another account or network maps elsewhere. The
    account is the exchange ``uid``, never an API key.
    """
    parts = (account_uid, network, strategy, symbol, epoch_id)
    if not all(parts) or any("|" in part for part in parts):
        raise ValueError(f"invalid signal identity parts: {parts!r}")
    return str(uuid5(SIGNAL_ID_NAMESPACE, "|".join(parts)))


EXIT_POLICY_VERSION = 1
_HUNDRED = Decimal(100)


@dataclass(frozen=True)
class ExitPolicy:
    """How a position opened from this ENTRY is to be exited (plan §5).

    Fixed into the position when it opens; a later Start with other settings
    does not change it. Every ``*_pct`` is in **percentage points**, exactly as
    in the strategy settings (``1`` means 1%); the helpers below do the only
    division by 100.
    """

    version: int
    stop_ref: Decimal
    swing_low_ref: Decimal
    min_stop_distance_pct: Decimal
    stop_buffer_pct: Decimal
    tp_pct: Decimal
    trail_interval: str
    trail_n: int
    trail_cap_pct: Decimal
    max_hold_hours: Decimal
    cooldown_minutes: int

    def initial_stop(self, fill: Decimal) -> Decimal:
        """``min(swing_low_ref, fill × (1 − min_stop_distance_pct/100))`` (spec §7.1)."""
        return min(
            self.swing_low_ref, fill * (1 - self.min_stop_distance_pct / _HUNDRED)
        )

    def take_profit(self, fill: Decimal) -> Decimal:
        return fill * (1 + self.tp_pct / _HUNDRED)

    def trail_ceiling(self, fill: Decimal) -> Decimal:
        """The trailing stop never rises above this (spec §7.2)."""
        return fill * (1 + self.trail_cap_pct / _HUNDRED)

    def trail_candidate(self, lows: list[Decimal]) -> Decimal:
        """``min(low[1..trail_n]) × (1 − stop_buffer_pct/100)``."""
        return min(lows) * (1 - self.stop_buffer_pct / _HUNDRED)

    def transport(self) -> dict[str, Any]:
        return {key: transport_value(value) for key, value in asdict(self).items()}

    @classmethod
    def from_transport(cls, data: dict[str, Any]) -> ExitPolicy:
        """Rebuild a policy stored as JSON, without losing Decimal digits."""
        if data.get("version") != EXIT_POLICY_VERSION:
            raise ValueError(
                f"unsupported exit policy version: {data.get('version')!r}"
            )
        return cls(
            version=int(data["version"]),
            stop_ref=Decimal(data["stop_ref"]),
            swing_low_ref=Decimal(data["swing_low_ref"]),
            min_stop_distance_pct=Decimal(data["min_stop_distance_pct"]),
            stop_buffer_pct=Decimal(data["stop_buffer_pct"]),
            tp_pct=Decimal(data["tp_pct"]),
            trail_interval=str(data["trail_interval"]),
            trail_n=int(data["trail_n"]),
            trail_cap_pct=Decimal(data["trail_cap_pct"]),
            max_hold_hours=Decimal(data["max_hold_hours"]),
            cooldown_minutes=int(data["cooldown_minutes"]),
        )


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
    exit_policy: ExitPolicy | None = None
    # EXIT
    exit_reason: str | None = None
    #: The ENTRY this EXIT closes, and the price it closed at (signal tracker).
    entry_signal_id: str | None = None
    exit_price: Decimal | None = None
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

    def identified(self, *, account_uid: str, network: str, strategy: str) -> Signal:
        """This signal with its execution-context identity.

        An ENTRY gets its deterministic :func:`entry_signal_id`; HOLD and EXIT
        keep their own ids, since one epoch may legitimately produce several
        HOLDs and each is audited.
        """
        if self.type != "ENTRY" or self.epoch_id is None:
            return self
        return replace(
            self,
            signal_id=entry_signal_id(
                account_uid=account_uid,
                network=network,
                strategy=strategy,
                symbol=self.symbol,
                epoch_id=self.epoch_id,
            ),
        )


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


@dataclass(frozen=True)
class PositionEvent:
    """Execution telling a strategy that one of its positions opened or closed.

    Sent after the change is committed. ``event_id`` identifies the change
    (the outbox event), so a repeated delivery is harmless; ``cooldown_until``
    of a close is absolute, computed once by execution from the exit fill.
    """

    event_id: str
    symbol: str
    position_id: str
    at: datetime
    cooldown_until: datetime | None = None


@dataclass(frozen=True)
class PositionsSnapshot:
    """Everything a strategy needs to know about positions before it decides.

    Open positions by symbol and the absolute end of every running cooldown.
    Restored before the first evaluation of a run (Start and resume alike), so
    a lost feedback call is repaired by the next run rather than repeated.
    """

    open_symbols: frozenset[str] = frozenset()
    cooldown_until: dict[str, datetime] = field(default_factory=dict)


@dataclass
class StrategyServices:
    """Everything a strategy may use, wired by the engine.

    All exchange access goes through these SDK-backed adapters; the strategy
    never instantiates ``binance_sdk_spot`` clients itself.
    """

    network: str
    symbols: list[str]
    rest: ExchangeRest
    streams: MarketStreams
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

    #: Renamed parameters, old name -> new name. The settings model translates
    #: them itself; the catalog publishes the map so the backend can explain a
    #: rejected old name and the UI can read old names from saved params.
    settings_legacy_keys: ClassVar[dict[str, str]] = {}

    #: Built-in presets, name -> full snapshot of the non-const parameters in
    #: JSON form. Published in the catalog; the backend keeps them as built-in
    #: presets users can pick (presets plan §4.2).
    settings_presets: ClassVar[dict[str, dict[str, Any]]] = {}

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

    @property
    def active_symbols(self) -> list[str] | None:
        """Symbols currently fully processed, for strategies that stage their
        universe through a pre-filter. ``None`` if the strategy has no such
        concept (every requested symbol is processed as given)."""
        return None

    @property
    def health_warning(self) -> str | None:
        """A degraded-but-running condition to surface in the heartbeat
        (e.g. a background cycle that keeps failing), or ``None``."""
        return None

    # --- feedback from execution (plan §4.4) -------------------------------

    async def on_position_opened(self, event: PositionEvent) -> None:  # noqa: B027
        """A position of this strategy opened (committed). Idempotent."""

    async def on_position_closed(self, event: PositionEvent) -> None:  # noqa: B027
        """A position closed; ``event.cooldown_until`` is absolute. Idempotent."""

    async def restore_positions(self, snapshot: PositionsSnapshot) -> None:  # noqa: B027
        """Replace position knowledge with ``snapshot``; called before start."""

    def indicator_snapshot(self) -> dict[str, Any] | None:
        """Indicator values on the last closed bar of each indicator's own
        interval, per symbol the strategy currently processes — for the
        Strategy Indicators Monitor page. ``None`` if the strategy has none.

        JSON-ready (decimals as strings, see :func:`transport_value`)::

            {
              "groups": [{"key", "label", "interval",
                          "indicators": [{"key", "label"}]}],
              "rows": [{"symbol",
                        "groups": {<group key>: {
                          "bar_open_time": ISO-8601 UTC | None,
                          "values": {<indicator key>: {"current", "previous"}},
                        }}}],
              # optional: why the universe narrowed to ``rows``
              "screening": {"at": ISO-8601 UTC, "universe", "selected",
                            "skipped": [{"symbol", "reason"}]},
            }

        Declaring the groups here keeps the UI generic: it renders whatever
        columns the running strategy lists, like the settings form does.
        """
        return None


__all__ = [
    "EXIT_POLICY_VERSION",
    "SIGNAL_ID_NAMESPACE",
    "ExitPolicy",
    "PositionEvent",
    "PositionsSnapshot",
    "Signal",
    "SignalSink",
    "SignalType",
    "Strategy",
    "StrategyServices",
    "entry_signal_id",
]
