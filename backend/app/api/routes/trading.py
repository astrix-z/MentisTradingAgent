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
from typing import Annotated, Any, Literal, Self

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, ValidationError, model_validator
from redis.exceptions import RedisError

from app.api.deps import CurrentUser, get_current_active_superuser
from app.core.redis_client import get_redis
from app.core.strategy_catalog import (
    CatalogError,
    CompiledCatalog,
    CompiledStrategy,
    json_safe_errors,
    read_catalog,
)
from app.models import User

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
# preserve empty arrays.
COMMIT_RUNTIME_LUA = """
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
CommandType = Literal["start", "stop", "balance", "symbols", "prices"]

# Commands that describe the running intent and therefore rewrite the runtime
# snapshot. Everything else is observation-only (balance, symbols) and goes
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
    them.
    """

    name: str
    label: str
    description: str = ""
    settings_schema: dict[str, Any] | None = None
    constraints: list[dict[str, Any]] = Field(default_factory=list)


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
                else:
                    # Stopping changes intent, not the saved configuration for the UI.
                    snapshot.running = False
                snapshot.last_command = type_
                snapshot.updated_at = _now()
                snapshot.revision += 1
                committed = await redis.execute_command(  # type: ignore[no-untyped-call]
                    "EVAL",
                    COMMIT_RUNTIME_LUA,
                    1,
                    REDIS_RUNTIME_KEY,
                    raw if raw is not None else "",
                    snapshot.model_dump_json(),
                    REDIS_COMMAND_STREAM,
                    envelope.model_dump_json(),
                    str(COMMAND_STREAM_MAXLEN),
                )
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
    }


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


async def _enforce_strategy_params(
    strategy: str, params: dict[str, Any]
) -> None:
    """Reject an invalid Start here, so nothing invalid reaches Redis.

    The rules enforced are the agent's own published ones (schema + cross-field
    constraints); the backend contributes no domain knowledge.
    """
    compiled = await _compiled_strategy(strategy)
    try:
        errors = compiled.validate(params)
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
async def issue_command(request: CommandRequest, _: SuperUserDep) -> CommandEnvelope:
    """Publish an arbitrary known command to the agent."""
    if request.type == "start":
        try:
            parsed = StartPayload(**request.payload)
        except ValidationError as exc:
            raise HTTPException(
                status_code=422, detail=json_safe_errors(exc)
            ) from exc
        await _enforce_strategy_params(parsed.strategy, parsed.params)
        return await _apply_command("start", _start_command_payload(parsed))
    if request.type == "balance":
        return await _apply_command("balance", request.payload)
    if request.type == "symbols":
        try:
            symbols = SymbolsPayload(**request.payload)
        except ValidationError as exc:
            raise HTTPException(
                status_code=422, detail=json_safe_errors(exc)
            ) from exc
        return await _apply_command("symbols", symbols.model_dump())
    if request.type == "prices":
        return await _apply_command("prices", {})
    if request.type == "stop":
        return await _apply_command("stop", {})
    # Unreachable while CommandType lists every type: the branches above are
    # exhaustive on purpose, so a new type cannot silently become a stop.
    raise HTTPException(  # pragma: no cover
        status_code=422, detail=f"unknown command type {request.type!r}"
    )


@router.post("/start/", status_code=202, response_model=CommandEnvelope)
async def start_trading(payload: StartPayload, _: SuperUserDep) -> CommandEnvelope:
    """Convenience alias: start trading on a network with a strategy."""
    await _enforce_strategy_params(payload.strategy, payload.params)
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
