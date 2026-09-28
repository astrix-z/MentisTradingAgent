"""Trading command endpoints (backend side of the Redis live plane).

The backend talks to the agent through *commands*, not through a shared
"desired state" document:

* command endpoints durably enqueue a command envelope onto the
  ``agent:commands:stream`` Redis Stream (start / stop, and more to come),
  read by the agent through a consumer group — not at-most-once Pub/Sub, so a
  command survives an agent restart/reconnect (see the Prompt 5 design note
  below);
* a *runtime snapshot* key (``agent:runtime``) is kept alongside so the agent
  can resume its intent after a restart and the UI can report what *should* be
  running;
* a per-command state hash (``agent:command:<id>``) tracks the lifecycle
  (accepted -> dispatched -> running -> succeeded|failed|timed_out) for
  diagnostics/audit, separate from the terminal-only ``/result/`` polling
  contract the UI already uses;
* the agent reports liveness through a heartbeat key.

To add a new command, define a payload schema, a handler on the agent side,
and (optionally) a thin endpoint here — the generic ``POST /trading/commands``
and the snapshot logic already route by ``type``.

Design note (Prompt 5 — durable delivery):
* Delivery mechanism: a Redis Stream + one consumer group, not Postgres/
  Alembic-backed outbox. There is no existing trading-command persistence
  path in this project to extend, and Redis is already the sole control-plane
  transport between backend and agent (see module docstring above and
  ``agent/agent/engine.py``); introducing a second datastore purely for this
  would be a much larger, separately-reviewable change. Durability here comes
  from the stream (entries survive until trimmed/acked) plus the consumer
  group's per-consumer pending-entries list (survives an agent restart).
* Retention: the stream is capped with an approximate ``MAXLEN`` trim
  (``COMMAND_STREAM_MAXLEN``); per-command state hashes get a short TTL while
  in flight and a longer one once terminal (``COMMAND_STATE_TTL_ACTIVE`` /
  ``COMMAND_STATE_TTL_TERMINAL``), refreshed by the agent on each transition.
* Idempotency/dedup: the agent (``agent/agent/common/commands.py``) checks the
  state hash before invoking a handler and skips (ack-only) redelivery of a
  command that already reached a terminal state, so a strategy Start is never
  re-run just because its stream entry was redelivered.
* Stop-vs-Balance: Start/Stop stay on the existing serialized queue with the
  generation-based cancel-on-Stop barrier (``agent/agent/main.py``); Balance
  commands run as independent concurrent tasks so a slow balance fetch can
  never make Stop wait behind it.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal, Self

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, ValidationError, model_validator
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, col, select

from app.api.deps import CurrentUser, SessionDep, get_current_active_superuser
from app.core.field_options import option_errors, read_options
from app.core.redis_client import get_redis
from app.core.strategy_catalog import (
    CatalogError,
    CompiledCatalog,
    CompiledStrategy,
    json_safe_errors,
    read_catalog,
)
from app.models import SignalPosition, User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/trading", tags=["trading"])

# Redis keys/stream shared with the agent (keep in sync with agent/agent/engine.py
# and agent/agent/common/commands.py).
REDIS_COMMAND_STREAM = "agent:commands:stream"
REDIS_COMMAND_GROUP = "agent-workers"
REDIS_RUNTIME_KEY = "agent:runtime"
REDIS_HEARTBEAT_KEY = "trading:heartbeat"
REDIS_RESULT_KEY = "agent:result"
REDIS_COMMAND_STATE_PREFIX = "agent:command:"
REDIS_SYMBOLS_KEY = "agent:symbols"
#: Set by the deploy fence script while the agent and its catalog change
#: (plan §4.6 G4); no Start is written while it exists.
REDIS_START_FENCE_KEY = "trading:start_fence"

# Approximate trim keeps the durable command stream bounded without an exact
# (expensive) length check on every write.
COMMAND_STREAM_MAXLEN = 10_000
# Command state TTLs: short while in flight (refreshed on every transition by
# whichever side moves the command forward), long once terminal so a recent
# history is inspectable without this hash becoming unbounded audit storage.
COMMAND_STATE_TTL_ACTIVE = 3600
COMMAND_STATE_TTL_TERMINAL = 86400
_TERMINAL_STATUSES = ("succeeded", "failed", "timed_out")

# Compare the exact document read by this worker, then commit and durably
# enqueue without interleaving with another worker. JSON stays in Python to
# preserve empty arrays. A Start (ARGV[6] = "1") is refused while the start
# fence KEYS[2] exists — checked in the same script as the write, so a Start
# validated before the fence was set is still refused (plan §4.6 G4).
# Returns 1 written, 0 snapshot CAS lost (re-read and retry), -1 fenced.
COMMIT_RUNTIME_LUA = """
if ARGV[6] == '1' and redis.call('EXISTS', KEYS[2]) == 1 then
    return -1
end
local current = redis.call('GET', KEYS[1])
if (current or '') ~= ARGV[1] then
    return 0
end
redis.call('SET', KEYS[1], ARGV[2])
redis.call('XADD', ARGV[3], 'MAXLEN', '~', ARGV[5], '*', 'data', ARGV[4])
return 1
"""

# How old an agent heartbeat may be before the agent is considered offline.
# Must be greater than the agent's heartbeat interval (agent/agent/engine.py).
HEARTBEAT_STALE_SECONDS = 10

SuperUserDep = Annotated[User, Depends(get_current_active_superuser)]

# Trading network the agent should run against (must match agent key selection).
Network = Literal["testnet", "live"]
CommandType = Literal[
    "start",
    "stop",
    "balance",
    "symbols",
    "prices",
    "indicators",
    "execution_pause",
    "execution_resume",
    "close_position",
]

# Commands that describe the running intent and therefore rewrite the runtime
# snapshot. Everything else is observation-only (balance, symbols, prices,
# indicators) and goes
# straight to the stream — an accidental snapshot write there would mean
# silently stopping the strategy.
_SNAPSHOT_COMMANDS: tuple[CommandType, ...] = ("start", "stop")

# Strategy names and per-strategy parameter rules are *not* listed here: the
# agent owns them and publishes them (see ``app.core.strategy_catalog``), so a
# new strategy or a changed domain rule needs no backend change and cannot
# drift from the agent. ``GET /strategies/`` serves that declaration to the UI.

_SYMBOL_RE = re.compile(r"[A-Z0-9]+")


def _now() -> datetime:
    return datetime.now(UTC)


# --- Documents -------------------------------------------------------------


class StartPayload(BaseModel):
    """Payload of the ``start`` command (strategy/symbols/params).

    Only what the backend itself knows is validated here: symbols are
    normalized and malformed/empty/duplicate ones are rejected with ``422``, so
    an invalid Start never reaches Redis. ``strategy`` and ``params`` are
    enforced against the agent's published catalog further down the request, in
    :func:`_enforce_strategy_params` — the agent owns those rules.
    """

    network: Network = "testnet"
    strategy: str
    symbols: list[str] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)
    #: Real-execution settings, enforced against the catalog's ``execution``
    #: section like ``params`` against the strategy's. Empty means defaults,
    #: which keep real execution off.
    execution: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_symbols(self) -> Self:
        if not self.symbols:
            raise ValueError("symbols must be a non-empty list")
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in self.symbols:
            symbol = str(raw).strip().upper()
            if _SYMBOL_RE.fullmatch(symbol) is None:
                raise ValueError(f"invalid symbol format: {raw!r}")
            if symbol in seen:
                raise ValueError(f"duplicate symbol: {symbol!r}")
            seen.add(symbol)
            normalized.append(symbol)
        self.symbols = normalized
        return self


class NetworkRequest(BaseModel):
    """Body carrying the target network (e.g. for ``balance``)."""

    network: Network


class CommandRequest(BaseModel):
    """Generic command request: ``{"type": "...", "payload": {...}}``."""

    type: CommandType
    payload: dict[str, Any] = Field(default_factory=dict)


class CommandEnvelope(BaseModel):
    """A command as it travels over the channel to the agent."""

    id: uuid.UUID
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    issued_at: datetime


class RuntimeSnapshot(BaseModel):
    """What the agent is *supposed* to be doing (drive from commands)."""

    running: bool = False
    network: Network = "testnet"
    strategy: str | None = None
    symbols: list[str] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)
    execution: dict[str, Any] = Field(default_factory=dict)
    last_command: str | None = None
    updated_at: datetime
    revision: int = Field(default=0, ge=0)


AgentState = Literal["running", "idle", "error"]


class AgentHeartbeat(BaseModel):
    """Liveness + account state reported by the agent."""

    state: AgentState
    message: str | None = None
    network: Network | None = None
    strategy: str | None = None
    balances: dict[str, float] | None = None
    active_symbols: list[str] | None = None
    updated_at: datetime


class TradingStatus(BaseModel):
    """Intent (snapshot) + agent liveness (heartbeat) for the UI."""

    desired: RuntimeSnapshot
    actual: AgentHeartbeat | None
    online: bool


class StrategyDescription(BaseModel):
    """One strategy exactly as the agent declares it.

    ``settings_schema`` is a JSON Schema for ``params`` and ``constraints`` are
    the cross-field rules it cannot express (see ``agent/agent/protocol.py``
    for the grammar). Both are served verbatim: the backend compiles them to
    validate a Start, the UI renders a form from them, and neither restates
    them. ``legacy_keys`` lets the UI read saved params that still use a
    renamed parameter's old name.
    """

    name: str
    label: str
    description: str = ""
    settings_schema: dict[str, Any] | None = None
    constraints: list[dict[str, Any]] = Field(default_factory=list)
    legacy_keys: dict[str, str] = Field(
        default_factory=dict,
        description="Renamed parameters, old name -> new name.",
    )


class StrategyCatalog(BaseModel):
    """What the UI needs to render and validate strategy settings.

    ``available=False`` means the agent has never published a catalog (it has
    not started yet), which is distinct from a strategy list that is empty by
    accident — the UI must say so instead of showing a blank form.
    """

    available: bool
    schema_hash: str | None = None
    published_at: str | None = None
    lookups: dict[str, dict[str, int]] = Field(default_factory=dict)
    strategies: list[StrategyDescription] = Field(default_factory=list)


class SymbolEntry(BaseModel):
    """One symbol in the universe the agent published.

    Everything here is the agent's classification, served verbatim: the backend
    contributes no domain knowledge. ``excluded_reason`` is why the symbol cannot
    be marked — ``None`` means it can.
    """

    symbol: str
    base_asset: str | None = None
    quote_asset: str
    status: str
    spot_allowed: bool = False
    quote_volume_24h: str = "0"
    #: Last trade price as a Decimal string, or ``null`` when the exchange gave
    #: none — which is not the same as a price of zero.
    last_price: str | None = None
    classification: str
    classification_label: str | None = None
    excluded_reason: str | None = None
    #: The exchange's Monitoring tag. A label, never a verdict: a monitored
    #: symbol trades normally, so this only ever informs.
    monitoring: bool = False
    selected: bool = False


class WorkingSymbols(BaseModel):
    """The tradeable universe plus which symbols the user marked.

    ``available=False`` means the agent has never published a universe (it has
    not started yet), which is distinct from a universe that is legitimately
    empty — the UI must say so instead of showing a blank table.
    """

    #: Defaults to True because a parsed *published* document is available by
    #: definition: the only unavailable response is the one this backend builds
    #: itself (:func:`list_working_symbols`), so no published document can be
    #: mistaken for it.
    available: bool = True
    quote_asset: str = "USDT"
    generated_at: str | None = None
    #: When ``last_price`` and ``quote_volume_24h`` were read from the exchange,
    #: which is later than ``generated_at`` once a price refresh has run.
    prices_updated_at: str | None = None
    #: When the exchange's Monitoring tag list was last read. ``null`` means it
    #: has never been read, which is not the same as nothing being monitored.
    monitoring_updated_at: str | None = None
    total: int = 0
    usable: int = 0
    selected: int = 0
    selected_unusable: int = 0
    symbols: list[SymbolEntry] = Field(default_factory=list)


class SymbolsPayload(BaseModel):
    """Payload of the ``symbols`` command: the complete marked set.

    ``[]`` is meaningful (nothing is marked) rather than an error. Only payload
    hygiene is enforced here — whether a symbol may actually be used is the
    agent's call, since it is the only service that reads the exchange.
    """

    symbols: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_symbols(self) -> Self:
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in self.symbols:
            symbol = str(raw).strip().upper()
            if _SYMBOL_RE.fullmatch(symbol) is None:
                raise ValueError(f"invalid symbol format: {raw!r}")
            if symbol in seen:
                raise ValueError(f"duplicate symbol: {symbol!r}")
            seen.add(symbol)
            normalized.append(symbol)
        self.symbols = normalized
        return self


class BalanceAsset(BaseModel):
    """One non-zero asset row. Amounts are Decimal-precise strings, never
    binary floats, so financial values round-trip exactly through JSON."""

    asset: str
    free: str
    locked: str


class BalanceSnapshot(BaseModel):
    """Documented shape of ``CommandResult.data`` for a succeeded ``balance``
    command (``CommandResult.data`` itself stays polymorphic across command
    types, so this is not wired as its response_model — it is the contract
    ``agent/agent/common/commands.py``'s ``_balance`` handler must produce,
    and what the backend/agent tests validate against).

    Zero-balance policy: assets with both ``free`` and ``locked`` equal to
    zero are omitted (matching the exchange call's ``omit_zero_balances``),
    so an asset present in ``assets`` is always non-zero.
    """

    network: Network
    fetched_at: datetime
    assets: list[BalanceAsset]


class IndicatorDeclaration(BaseModel):
    key: str
    label: str


class IndicatorGroupDeclaration(BaseModel):
    """A column group: indicators sharing one interval (e.g. Tide 4h)."""

    key: str
    label: str
    interval: str
    indicators: list[IndicatorDeclaration]


class IndicatorValue(BaseModel):
    """Values on the last closed bar and the bar before it, as Decimal-precise
    strings; ``None`` while the history is too short to compute one."""

    current: str | None
    previous: str | None


class IndicatorGroupValues(BaseModel):
    """``bar_open_time`` is the open of the bar the values were computed on;
    ``None`` (with empty ``values``) when the symbol has too little history."""

    bar_open_time: datetime | None
    values: dict[str, IndicatorValue]


class IndicatorRow(BaseModel):
    symbol: str
    groups: dict[str, IndicatorGroupValues]


class IndicatorSnapshot(BaseModel):
    """Documented shape of ``CommandResult.data`` for a succeeded
    ``indicators`` command, as produced by ``Strategy.indicator_snapshot`` in
    ``agent/agent/strategies/base.py`` (plus the run stamp added by the
    engine). The strategy declares its own ``groups``, so the UI renders any
    strategy's indicators without backend changes. ``rows`` holds only the
    symbols the strategy is processing, not its whole universe.
    """

    strategy: str
    network: Network
    computed_at: datetime
    groups: list[IndicatorGroupDeclaration]
    rows: list[IndicatorRow]


class SignalPositionPublic(BaseModel):
    """One ENTRY-signal-derived virtual position (D-SIGNAL-ONLY: the strategy
    never places a real order, so this reflects "if this signal had been
    acted on", not an actual exchange position). ``status`` is evaluated
    lazily on each read against whatever ``agent:symbols`` last published —
    not a continuous monitor — so it can lag the real market by up to the
    symbol-universe refresh interval (~5 min)."""

    symbol: str
    network: str
    entry_ref: str
    stop_ref: str
    take_ref: str
    current_price: str | None
    pnl_pct: str | None
    status: str
    emitted_at: datetime
    closed_at: datetime | None
    closed_price: str | None


class PositionsList(BaseModel):
    positions: list[SignalPositionPublic]


class CommandResult(BaseModel):
    """Execution outcome produced by the agent, not an acceptance receipt."""

    command_id: uuid.UUID
    type: str
    ok: bool
    data: dict[str, Any] | list[Any] | None = None
    message: str | None = None
    at: datetime


CommandStatus = Literal[
    "accepted", "dispatched", "running", "succeeded", "failed", "timed_out"
]


class CommandState(BaseModel):
    """Full command lifecycle (see the Prompt 5 design note above), for
    diagnostics/audit — additive; existing UI polling keeps using
    ``/result/`` for the terminal outcome."""

    command_id: uuid.UUID
    type: str
    status: CommandStatus
    network: Network | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    error: str | None = None


# --- Redis helpers ---------------------------------------------------------


def _redis_unavailable(_exc: RedisError) -> HTTPException:
    return HTTPException(status_code=503, detail="Redis is unavailable")


def _start_fenced() -> HTTPException:
    return HTTPException(status_code=503, detail="starting is closed for maintenance")


async def _refuse_start_if_fenced() -> None:
    """Early answer while a deploy holds the start fence (plan §4.6 G4).

    Only a courtesy before validation: the guarantee is the check inside
    ``COMMIT_RUNTIME_LUA``, atomic with the write.
    """
    try:
        fenced = await get_redis().exists(REDIS_START_FENCE_KEY)
    except RedisError as exc:
        raise _redis_unavailable(exc) from exc
    if fenced:
        raise _start_fenced()


def database_unavailable(exc: SQLAlchemyError) -> HTTPException:
    logger.error("database error: %s", exc)
    return HTTPException(status_code=503, detail="the database is unavailable")


async def _enqueue(envelope: CommandEnvelope) -> None:
    """Durably append to the command stream (at-least-once; see module docstring)."""
    redis = get_redis()
    try:
        await redis.xadd(
            REDIS_COMMAND_STREAM,
            {"data": envelope.model_dump_json()},
            maxlen=COMMAND_STREAM_MAXLEN,
            approximate=True,
        )
    except RedisError as exc:
        raise _redis_unavailable(exc) from exc


async def _record_command_accepted(
    envelope: CommandEnvelope, *, network: Any | None
) -> None:
    """Best-effort observability write. The durable stream entry (already
    committed by the time this runs) is what guarantees delivery — this hash
    is diagnostics, so its own failure must not fail command acceptance."""
    redis = get_redis()
    key = f"{REDIS_COMMAND_STATE_PREFIX}{envelope.id}"
    fields: dict[str, str] = {
        "status": "accepted",
        "type": envelope.type,
        "created_at": envelope.issued_at.isoformat(),
        "updated_at": envelope.issued_at.isoformat(),
    }
    if network:
        fields["network"] = str(network)
    try:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.hset(key, mapping=fields)
            pipe.expire(key, COMMAND_STATE_TTL_ACTIVE)
            await pipe.execute()
    except RedisError:
        logger.warning("cannot record command state (Redis unavailable)")


async def _get_snapshot() -> RuntimeSnapshot | None:
    redis = get_redis()
    try:
        raw = await redis.get(REDIS_RUNTIME_KEY)
    except RedisError as exc:
        raise _redis_unavailable(exc) from exc
    return RuntimeSnapshot.model_validate_json(raw) if raw else None


async def _apply_command(
    type_: CommandType, payload: dict[str, Any]
) -> CommandEnvelope:
    """Accept intent; durable enqueue does not confirm agent execution."""
    envelope = CommandEnvelope(
        id=uuid.uuid4(), type=type_, payload=payload, issued_at=_now()
    )
    if type_ not in _SNAPSHOT_COMMANDS:
        # Observation-only: no intent to record, so only the stream is written.
        await _enqueue(envelope)
    else:
        redis = get_redis()
        try:
            while True:
                raw = await redis.get(REDIS_RUNTIME_KEY)
                snapshot = (
                    RuntimeSnapshot.model_validate_json(raw)
                    if raw is not None
                    else RuntimeSnapshot(updated_at=_now())
                )
                if type_ == "start":
                    snapshot.running = True
                    snapshot.network = payload["network"]
                    snapshot.strategy = payload["strategy"]
                    snapshot.symbols = list(payload["symbols"])
                    snapshot.params = dict(payload["params"])
                    snapshot.execution = dict(payload.get("execution") or {})
                else:
                    # Stopping changes intent, not the saved configuration for the UI.
                    snapshot.running = False
                snapshot.last_command = type_
                snapshot.updated_at = _now()
                snapshot.revision += 1
                committed = await redis.execute_command(  # type: ignore[no-untyped-call]
                    "EVAL",
                    COMMIT_RUNTIME_LUA,
                    2,
                    REDIS_RUNTIME_KEY,
                    REDIS_START_FENCE_KEY,
                    raw if raw is not None else "",
                    snapshot.model_dump_json(),
                    REDIS_COMMAND_STREAM,
                    envelope.model_dump_json(),
                    str(COMMAND_STREAM_MAXLEN),
                    "1" if type_ == "start" else "0",
                )
                if committed == -1:
                    # Not retried: the fence stays until the deploy lifts it.
                    raise _start_fenced()
                if committed:
                    break
        except RedisError as exc:
            raise _redis_unavailable(exc) from exc
    await _record_command_accepted(envelope, network=payload.get("network"))
    return envelope


def _start_command_payload(parsed: StartPayload) -> dict[str, Any]:
    """Normalize a validated start payload into the command/snapshot payload."""
    return {
        "network": parsed.network,
        "strategy": parsed.strategy,
        "symbols": [str(s).upper() for s in parsed.symbols],
        "params": dict(parsed.params),
        "execution": dict(parsed.execution),
    }


def _params_loc(loc: list[Any]) -> list[Any]:
    """Point a strategy settings error at ``body.params`` (plan §4.3.4).

    Field errors come from the compiled model by field name alone; rule and
    unknown-parameter errors already carry ``["body", "params"]``.
    """
    return loc if loc[:2] == ["body", "params"] else ["body", "params", *loc]


def _execution_loc(loc: list[Any]) -> list[Any]:
    """Point an error of the execution section at ``body.execution``.

    The compiled section reports rule failures under ``body.params`` (its
    strategy-shaped origin) and field errors by field name alone.
    """
    rest = loc[2:] if loc[:2] == ["body", "params"] else loc
    return ["body", "execution", *rest]


async def _compiled_execution() -> CompiledStrategy:
    """The catalog's execution section; ``503`` when the agent has none."""
    redis = get_redis()
    try:
        catalog = await read_catalog(redis)
    except RedisError as exc:
        raise _redis_unavailable(exc) from exc
    if catalog is None or catalog.execution is None:
        raise HTTPException(
            status_code=503,
            detail="the agent has not published execution settings",
        )
    return catalog.execution


async def _compiled_strategy(name: str) -> CompiledStrategy:
    """The agent's compiled description of ``name``, or a clear HTTP error.

    A missing or unevaluable catalog is ``503``, not ``422``: the backend
    cannot tell whether the request is valid, and must not imply that it is.
    """
    redis = get_redis()
    try:
        catalog = await read_catalog(redis)
    except RedisError as exc:
        raise _redis_unavailable(exc) from exc
    except CatalogError as exc:
        logger.error("strategy catalog unusable: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="the agent's strategy catalog cannot be used; refusing to "
            "validate strategy parameters",
        ) from exc
    if catalog is None:
        raise HTTPException(
            status_code=503,
            detail="the agent has not published a strategy catalog",
        )
    compiled = catalog.strategy(name)
    if compiled is None:
        known = ", ".join(sorted(catalog.strategies)) or "<none>"
        raise HTTPException(
            status_code=422,
            detail=[
                {
                    "loc": ["body", "strategy"],
                    "msg": f"unknown strategy {name!r}; known: {known}",
                    "type": "value_error",
                }
            ],
        )
    return compiled


def strategy_params_errors(
    compiled: CompiledStrategy,
    params: dict[str, Any],
    session: Session,
    options: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    """The pipeline of plan §4.3.5 for a Start or a saved preset, ``loc`` at
    ``body.params``.

    Steps 1–4 are the agent's published rules (schema + cross-field
    constraints); step 5 is the option lists in the database, checked once
    steps 1–3 passed and reported together with step 4. Raises
    :class:`CatalogError` (a rule cannot be evaluated) and ``503`` when the
    lists cannot be read — neither means valid or invalid. ``options`` spares
    the read when the caller checks many presets at once.
    """
    checked = compiled.check(params)
    errors = list(checked.errors)
    if checked.canonical is not None:
        if options is None:
            try:
                options = read_options(session, compiled.name)
            except SQLAlchemyError as exc:
                raise database_unavailable(exc) from exc
        errors += option_errors(options, checked.canonical)
    return [{**error, "loc": _params_loc(error.get("loc") or [])} for error in errors]


async def _enforce_strategy_params(
    strategy: str,
    params: dict[str, Any],
    execution: dict[str, Any] | None,
    session: Session,
) -> None:
    """Reject an invalid Start here, so nothing invalid reaches Redis.

    The rules enforced are the agent's own published ones (schema + cross-field
    constraints) and the option lists the operator keeps; the backend
    contributes no domain knowledge.
    """
    compiled = await _compiled_strategy(strategy)
    try:
        errors = strategy_params_errors(compiled, params, session)
        if execution:
            section = await _compiled_execution()
            errors += [
                {**error, "loc": _execution_loc(error.get("loc") or [])}
                for error in section.validate(execution)
            ]
    except CatalogError as exc:
        logger.error("strategy catalog not evaluable: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="the agent's strategy catalog cannot be evaluated",
        ) from exc
    if errors:
        raise HTTPException(status_code=422, detail=errors)


def _catalog_response(catalog: CompiledCatalog | None) -> StrategyCatalog:
    if catalog is None:
        return StrategyCatalog(available=False)
    return StrategyCatalog(
        available=True,
        schema_hash=catalog.schema_hash,
        published_at=catalog.published_at or None,
        lookups=catalog.lookups,
        strategies=[
            StrategyDescription(
                name=s.name,
                label=s.label,
                description=s.description,
                settings_schema=s.settings_schema,
                constraints=s.constraints,
                legacy_keys=s.legacy_keys,
            )
            for s in catalog.strategies.values()
        ],
    )


async def _get_working_symbols() -> WorkingSymbols:
    """The universe the agent published, or ``available=False``.

    The document is served as the agent wrote it — the backend neither chooses
    the symbols nor judges them — and an unusable one is reported the same way
    an unpublished one is, because both mean the UI has nothing to render.
    """
    redis = get_redis()
    try:
        raw = await redis.get(REDIS_SYMBOLS_KEY)
    except RedisError as exc:
        raise _redis_unavailable(exc) from exc
    if not raw:
        return WorkingSymbols(available=False)
    try:
        return WorkingSymbols.model_validate_json(raw)
    except ValidationError as exc:
        logger.error("published symbol universe is unusable: %s", exc)
        return WorkingSymbols(available=False)


# --- Endpoints -------------------------------------------------------------


@router.get("/strategies/", response_model=StrategyCatalog)
async def list_strategies(_: CurrentUser) -> StrategyCatalog:
    """Strategies and their settings, as published by the agent.

    Served with ``available=False`` (not an error) when the agent has not run
    yet, so the UI can distinguish "no agent" from "no strategies".
    """
    redis = get_redis()
    try:
        catalog = await read_catalog(redis)
    except RedisError as exc:
        raise _redis_unavailable(exc) from exc
    except CatalogError as exc:
        logger.error("strategy catalog unusable: %s", exc)
        return StrategyCatalog(available=False)
    return _catalog_response(catalog)


@router.get("/symbols/", response_model=WorkingSymbols)
async def list_working_symbols(_: SuperUserDep) -> WorkingSymbols:
    """The tradeable universe and the marks, as published by the agent.

    Served with ``available=False`` (not an error) when the agent has not run
    yet, so the UI can distinguish "no agent" from "no symbols".
    """
    return await _get_working_symbols()


@router.get("/positions/", response_model=PositionsList)
async def list_positions(session: SessionDep, _: SuperUserDep) -> PositionsList:
    """ENTRY-signal-derived virtual positions (see `SignalPositionPublic`).

    Recorded by ``app.core.signal_listener`` as ENTRY signals arrive on
    ``agent:events``; a still-``open`` row is closed here, lazily, the first
    time a read observes the last known price having crossed its stop or take
    level — this endpoint is the only place that evaluates/persists that
    transition, so "closed" always reflects what a viewer actually saw.
    """
    rows = session.exec(
        select(SignalPosition).order_by(col(SignalPosition.emitted_at).desc())
    ).all()
    prices: dict[str, str] = {}
    if any(row.status == "open" for row in rows):
        universe = await _get_working_symbols()
        prices = {
            entry.symbol: entry.last_price
            for entry in universe.symbols
            if entry.last_price is not None
        }
    positions = [_position_public(row, prices, session) for row in rows]
    session.commit()
    return PositionsList(positions=positions)


def _cross_status(current: str, take_ref: str, stop_ref: str) -> str | None:
    """Long-only: ``take_ref > entry > stop_ref`` always holds (the
    strategy's own invariant), so a simple threshold check suffices."""
    try:
        current_dec, take_dec, stop_dec = (
            Decimal(current),
            Decimal(take_ref),
            Decimal(stop_ref),
        )
    except InvalidOperation:
        return None
    if current_dec >= take_dec:
        return "hit_take"
    if current_dec <= stop_dec:
        return "hit_stop"
    return None


def _pnl_pct(reference: str | None, entry_ref: str) -> str | None:
    if reference is None:
        return None
    try:
        return str((Decimal(reference) - Decimal(entry_ref)) / Decimal(entry_ref) * 100)
    except InvalidOperation, ZeroDivisionError:
        return None


def _position_public(
    row: SignalPosition, prices: dict[str, str], session: SessionDep
) -> SignalPositionPublic:
    current = prices.get(row.symbol)
    if row.status == "open" and current is not None:
        crossed = _cross_status(current, row.take_ref, row.swing_low_ref)
        if crossed is not None:
            row.status = crossed
            row.closed_at = datetime.now(UTC)
            row.closed_price = current
            session.add(row)
    reference = row.closed_price if row.closed_price is not None else current
    return SignalPositionPublic(
        symbol=row.symbol,
        network=row.network,
        entry_ref=row.entry_ref,
        stop_ref=row.swing_low_ref,
        take_ref=row.take_ref,
        current_price=current,
        pnl_pct=_pnl_pct(reference, row.entry_ref),
        status=row.status,
        emitted_at=row.emitted_at,
        closed_at=row.closed_at,
        closed_price=row.closed_price,
    )


@router.post("/symbols/", status_code=202, response_model=CommandEnvelope)
async def set_working_symbols(
    payload: SymbolsPayload, _: SuperUserDep
) -> CommandEnvelope:
    """Replace the marked working symbols.

    202 only acknowledges the command: the agent checks every symbol against the
    exchange before storing anything, and reports the outcome through
    ``/result/`` (a rejected symbol leaves the previous selection untouched).
    """
    return await _apply_command("symbols", {"symbols": payload.symbols})


@router.post("/commands/", status_code=202, response_model=CommandEnvelope)
async def issue_command(
    request: CommandRequest, session: SessionDep, _: SuperUserDep
) -> CommandEnvelope:
    """Publish an arbitrary known command to the agent."""
    if request.type == "start":
        await _refuse_start_if_fenced()
        try:
            parsed = StartPayload(**request.payload)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=json_safe_errors(exc)) from exc
        await _enforce_strategy_params(
            parsed.strategy, parsed.params, parsed.execution, session
        )
        return await _apply_command("start", _start_command_payload(parsed))
    if request.type == "balance":
        return await _apply_command("balance", request.payload)
    if request.type == "symbols":
        try:
            symbols = SymbolsPayload(**request.payload)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=json_safe_errors(exc)) from exc
        return await _apply_command("symbols", symbols.model_dump())
    if request.type == "prices":
        return await _apply_command("prices", {})
    if request.type == "indicators":
        return await _apply_command("indicators", {})
    if request.type == "stop":
        return await _apply_command("stop", {})
    if request.type == "close_position":
        symbol = str(request.payload.get("symbol") or "").strip().upper()
        if _SYMBOL_RE.fullmatch(symbol) is None:
            raise HTTPException(
                status_code=422,
                detail=[
                    {
                        "loc": ["body", "payload", "symbol"],
                        "msg": "a valid symbol is required",
                        "type": "value_error",
                    }
                ],
            )
        return await _apply_command("close_position", {"symbol": symbol})
    if request.type in ("execution_pause", "execution_resume"):
        # Entries only; positions, protection and exits are not affected.
        return await _apply_command(request.type, {})
    # Unreachable while CommandType lists every type: the branches above are
    # exhaustive on purpose, so a new type cannot silently become a stop.
    raise HTTPException(  # pragma: no cover
        status_code=422, detail=f"unknown command type {request.type!r}"
    )


@router.post("/start/", status_code=202, response_model=CommandEnvelope)
async def start_trading(
    payload: StartPayload, session: SessionDep, _: SuperUserDep
) -> CommandEnvelope:
    """Convenience alias: start trading on a network with a strategy."""
    await _refuse_start_if_fenced()
    await _enforce_strategy_params(
        payload.strategy, payload.params, payload.execution, session
    )
    return await _apply_command("start", _start_command_payload(payload))


@router.post("/stop/", status_code=202, response_model=CommandEnvelope)
async def stop_trading(_: SuperUserDep) -> CommandEnvelope:
    """Convenience alias: stop trading."""
    return await _apply_command("stop", {})


@router.post("/balance/", status_code=202, response_model=CommandEnvelope)
async def fetch_balance(request: NetworkRequest, _: SuperUserDep) -> CommandEnvelope:
    """Ask the agent for the account balance on the given network.

    ``network`` is required: the caller must state it explicitly rather than
    implicitly reusing whatever network the agent currently happens to run.
    """
    return await _apply_command("balance", {"network": request.network})


@router.post("/prices/", status_code=202, response_model=CommandEnvelope)
async def refresh_prices(_: SuperUserDep) -> CommandEnvelope:
    """Ask the agent to re-read prices and republish the universe.

    Nothing is returned but the command envelope: the fresh values arrive in the
    symbol document the agent republishes immediately afterwards, so the page has
    one shape of the data to read rather than two. 202 only acknowledges the
    command.
    """
    return await _apply_command("prices", {})


@router.post("/indicators/", status_code=202, response_model=CommandEnvelope)
async def fetch_indicators(_: SuperUserDep) -> CommandEnvelope:
    """Ask the running strategy for its indicator values on the last closed
    bar of each indicator's interval (Strategy Indicators Monitor).

    Observation-only: computed by the agent from candles it already holds, it
    never changes the runtime snapshot. The values arrive through
    ``/result/`` in the :class:`IndicatorSnapshot` shape.
    """
    return await _apply_command("indicators", {})


@router.get("/result/", response_model=CommandResult | None)
async def get_command_result(
    _: SuperUserDep,
    command_id: Annotated[
        uuid.UUID | None,
        Query(
            description="Command ID; agent retains per-command results for 3600 seconds."
        ),
    ] = None,
) -> CommandResult | None:
    """Read a correlated result, or the legacy latest result if no ID is supplied.

    Null means no retained result (pending, unknown, or expired), not success.
    Reads do not refresh the TTL owned by the agent.
    """
    redis = get_redis()
    key = (
        f"{REDIS_RESULT_KEY}:{command_id}"
        if command_id is not None
        else REDIS_RESULT_KEY
    )
    try:
        raw = await redis.get(key)
    except RedisError as exc:
        raise _redis_unavailable(exc) from exc
    if raw is None:
        return None
    result = CommandResult.model_validate_json(raw)
    if command_id is not None and result.command_id != command_id:
        return None
    return result


@router.get("/commands/{command_id}/state/", response_model=CommandState | None)
async def get_command_state(
    _: SuperUserDep, command_id: uuid.UUID
) -> CommandState | None:
    """Full lifecycle state for one command (diagnostics/audit).

    Distinct from ``/result/``: this reflects accepted/dispatched/running too,
    not only a terminal outcome. Null means no state is retained (unknown or
    expired past its TTL), not any particular status.
    """
    redis = get_redis()
    key = f"{REDIS_COMMAND_STATE_PREFIX}{command_id}"
    try:
        raw = await redis.hgetall(key)  # type: ignore[misc]
    except RedisError as exc:
        raise _redis_unavailable(exc) from exc
    if not raw:
        return None
    return CommandState(
        command_id=command_id,
        type=raw.get("type", ""),
        status=raw.get("status", "accepted"),
        network=raw.get("network"),
        created_at=raw.get("created_at"),
        updated_at=raw.get("updated_at"),
        error=raw.get("error"),
    )


@router.get("/status/", response_model=TradingStatus)
async def get_status(_: SuperUserDep) -> TradingStatus:
    """Return intent (snapshot) plus agent liveness derived from heartbeat."""
    snapshot = await _get_snapshot()
    redis = get_redis()
    try:
        heartbeat_raw = await redis.get(REDIS_HEARTBEAT_KEY)
    except RedisError as exc:
        raise _redis_unavailable(exc) from exc

    heartbeat = (
        AgentHeartbeat.model_validate_json(heartbeat_raw)
        if heartbeat_raw is not None
        else None
    )

    online = False
    if heartbeat is not None:
        age = (_now() - heartbeat.updated_at).total_seconds()
        online = 0 <= age <= HEARTBEAT_STALE_SECONDS

    return TradingStatus(
        desired=snapshot or RuntimeSnapshot(updated_at=_now()),
        actual=heartbeat,
        online=online,
    )
