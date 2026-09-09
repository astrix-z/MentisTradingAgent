"""Trading command endpoints (backend side of the Redis live plane).

The backend talks to the agent through *commands*, not through a shared
"desired state" document:

* command endpoints publish a command envelope to the ``agent:commands``
  channel (start / stop, and more to come);
* a *runtime snapshot* key (``agent:runtime``) is kept alongside so the agent
  can resume its intent after a restart and the UI can report what *should* be
  running;
* the agent reports liveness through a heartbeat key.

To add a new command, define a payload schema, a handler on the agent side,
and (optionally) a thin endpoint here — the generic ``POST /trading/commands``
and the snapshot logic already route by ``type``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ValidationError
from redis.exceptions import RedisError

from app.api.deps import CurrentUser, get_current_active_superuser
from app.core.redis_client import get_redis
from app.models import User

router = APIRouter(prefix="/trading", tags=["trading"])

# Redis keys/channel shared with the agent (keep in sync with agent/agent/engine.py).
REDIS_COMMAND_CHANNEL = "agent:commands"
REDIS_RUNTIME_KEY = "agent:runtime"
REDIS_HEARTBEAT_KEY = "trading:heartbeat"
REDIS_RESULT_KEY = "agent:result"

# How old an agent heartbeat may be before the agent is considered offline.
# Must be greater than the agent's heartbeat interval (agent/agent/engine.py).
HEARTBEAT_STALE_SECONDS = 10

SuperUserDep = Annotated[User, Depends(get_current_active_superuser)]

# Trading network the agent should run against (must match agent key selection).
Network = Literal["testnet", "live"]
CommandType = Literal["start", "stop", "balance"]

# Strategy/protector names the UI can pick from. Keep in sync with the agent
# registries: ``agent/agent/strategies`` (@strategy) and
# ``agent/agent/protection`` (@protector). The agent validates unknown names.
KNOWN_STRATEGIES = ("grid",)
KNOWN_PROTECTORS = ("trailing_stop",)


def _now() -> datetime:
    return datetime.now(UTC)


# --- Documents -------------------------------------------------------------


class StartPayload(BaseModel):
    """Payload of the ``start`` command (strategy/protector/symbols/params)."""

    network: Network = "testnet"
    strategy: str
    protector: str | None = None
    symbols: list[str] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)


class NetworkRequest(BaseModel):
    """Optional body carrying the target network (e.g. for ``balance``)."""

    network: Network = "testnet"


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
    protector: str | None = None
    symbols: list[str] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)
    last_command: str | None = None
    updated_at: datetime


AgentState = Literal["running", "idle", "error"]


class AgentHeartbeat(BaseModel):
    """Liveness + account state reported by the agent."""

    state: AgentState
    message: str | None = None
    network: Network | None = None
    strategy: str | None = None
    balances: dict[str, float] | None = None
    updated_at: datetime


class TradingStatus(BaseModel):
    """Intent (snapshot) + agent liveness (heartbeat) for the UI."""

    running: bool = False
    network: Network | None = None
    strategy: str | None = None
    protector: str | None = None
    symbols: list[str] = Field(default_factory=list)
    state: AgentState | None = None
    message: str | None = None
    balances: dict[str, float] | None = None
    online: bool


class CommandResult(BaseModel):
    """The most recent command result produced by the agent."""

    command_id: uuid.UUID
    type: str
    ok: bool
    data: dict[str, Any] | list[Any] | None = None
    message: str | None = None
    at: datetime


# --- Redis helpers ---------------------------------------------------------


def _redis_unavailable(_exc: RedisError) -> HTTPException:
    return HTTPException(status_code=503, detail="Redis is unavailable")


async def _publish(envelope: CommandEnvelope) -> None:
    redis = get_redis()
    try:
        await redis.publish(REDIS_COMMAND_CHANNEL, envelope.model_dump_json())
    except RedisError as exc:
        raise _redis_unavailable(exc) from exc


async def _set_snapshot(snapshot: RuntimeSnapshot) -> None:
    redis = get_redis()
    try:
        await redis.set(REDIS_RUNTIME_KEY, snapshot.model_dump_json())
    except RedisError as exc:
        raise _redis_unavailable(exc) from exc


async def _get_snapshot() -> RuntimeSnapshot | None:
    redis = get_redis()
    try:
        raw = await redis.get(REDIS_RUNTIME_KEY)
    except RedisError as exc:
        raise _redis_unavailable(exc) from exc
    return RuntimeSnapshot.model_validate_json(raw) if raw else None


async def _apply_command(type_: CommandType, payload: dict[str, Any]) -> CommandEnvelope:
    """Build+publish an envelope and fold runtime-changing commands into the snapshot."""
    envelope = CommandEnvelope(
        id=uuid.uuid4(), type=type_, payload=payload, issued_at=_now()
    )
    await _publish(envelope)

    # Only commands that change what *should* be running touch the snapshot.
    if type_ in ("start", "stop"):
        snapshot = await _get_snapshot()
        if snapshot is None:
            snapshot = RuntimeSnapshot(updated_at=_now())
        if type_ == "start":
            snapshot.running = True
            snapshot.network = payload["network"]
            snapshot.strategy = payload["strategy"]
            snapshot.protector = payload.get("protector") or None
            snapshot.symbols = [str(s).upper() for s in (payload.get("symbols") or [])]
            snapshot.params = dict(payload.get("params") or {})
        elif type_ == "stop":
            snapshot.running = False
            snapshot.protector = None
            snapshot.symbols = []
            snapshot.params = {}
            snapshot.running = False
        snapshot.last_command = type_
        snapshot.updated_at = _now()
        await _set_snapshot(snapshot)
    return envelope


def _start_command_payload(parsed: StartPayload) -> dict[str, Any]:
    """Normalize a validated start payload into the command/snapshot payload."""
    return {
        "network": parsed.network,
        "strategy": parsed.strategy,
        "protector": parsed.protector,
        "symbols": [str(s).upper() for s in parsed.symbols],
        "params": dict(parsed.params),
    }


def _start_payload(request: CommandRequest) -> dict[str, Any]:
    try:
        parsed = StartPayload(**request.payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc
    return _start_command_payload(parsed)


# --- Endpoints -------------------------------------------------------------


@router.get("/strategies/", response_model=list[str])
async def list_strategies(_: CurrentUser) -> list[str]:
    """Names of selectable trading strategies (see ``KNOWN_STRATEGIES``)."""
    return list(KNOWN_STRATEGIES)


@router.get("/protectors/", response_model=list[str])
async def list_protectors(_: CurrentUser) -> list[str]:
    """Names of selectable position-protection algorithms."""
    return list(KNOWN_PROTECTORS)


@router.post("/commands/", status_code=202, response_model=CommandEnvelope)
async def issue_command(request: CommandRequest, _: SuperUserDep) -> CommandEnvelope:
    """Publish an arbitrary known command to the agent."""
    if request.type == "start":
        return await _apply_command("start", _start_payload(request))
    if request.type == "balance":
        return await _apply_command("balance", request.payload)
    return await _apply_command("stop", {})


@router.post("/start/", status_code=202, response_model=CommandEnvelope)
async def start_trading(payload: StartPayload, _: SuperUserDep) -> CommandEnvelope:
    """Convenience alias: start trading on a network with a strategy."""
    return await _apply_command("start", _start_command_payload(payload))


@router.post("/stop/", status_code=202, response_model=CommandEnvelope)
async def stop_trading(_: SuperUserDep) -> CommandEnvelope:
    """Convenience alias: stop trading."""
    return await _apply_command("stop", {})


@router.post("/balance/", status_code=202, response_model=CommandEnvelope)
async def fetch_balance(
    _: SuperUserDep, request: NetworkRequest | None = None
) -> CommandEnvelope:
    """Ask the agent for the account balance on the given network."""
    if request is None:
        payload: dict[str, Any] = {}
    else:
        payload = {"network": request.network}
    return await _apply_command("balance", payload)


@router.get("/result/", response_model=CommandResult | None)
async def get_command_result(_: CurrentUser) -> CommandResult | None:
    """Return the most recent command result produced by the agent."""
    redis = get_redis()
    try:
        raw = await redis.get(REDIS_RESULT_KEY)
    except RedisError as exc:
        raise _redis_unavailable(exc) from exc
    if raw is None:
        return None
    return CommandResult.model_validate_json(raw)


@router.get("/status/", response_model=TradingStatus)
async def get_status(_: CurrentUser) -> TradingStatus:
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
    state: AgentState | None = None
    message: str | None = None
    balances: dict[str, float] | None = None
    if heartbeat is not None:
        age = (_now() - heartbeat.updated_at).total_seconds()
        online = age <= HEARTBEAT_STALE_SECONDS
        state = heartbeat.state
        message = heartbeat.message
        balances = heartbeat.balances

    return TradingStatus(
        running=bool(snapshot.running) if snapshot else False,
        network=snapshot.network if snapshot else None,
        strategy=snapshot.strategy if snapshot else None,
        protector=snapshot.protector if snapshot else None,
        symbols=snapshot.symbols if snapshot else [],
        state=state,
        message=message,
        balances=balances,
        online=online,
    )
