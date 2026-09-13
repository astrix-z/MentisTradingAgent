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

from fastapi import APIRouter, Depends, HTTPException, Query
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

# Compare the exact document read by this worker, then commit and publish without
# interleaving with another worker. JSON stays in Python to preserve empty arrays.
COMMIT_RUNTIME_LUA = """
local current = redis.call('GET', KEYS[1])
if (current or '') ~= ARGV[1] then
    return 0
end
redis.call('SET', KEYS[1], ARGV[2])
redis.call('PUBLISH', ARGV[3], ARGV[4])
return 1
"""

# How old an agent heartbeat may be before the agent is considered offline.
# Must be greater than the agent's heartbeat interval (agent/agent/engine.py).
HEARTBEAT_STALE_SECONDS = 10

SuperUserDep = Annotated[User, Depends(get_current_active_superuser)]

# Trading network the agent should run against (must match agent key selection).
Network = Literal["testnet", "live"]
CommandType = Literal["start", "stop", "balance"]

# Strategy names the UI can pick from. Keep in sync with the agent registry
# ``agent/agent/strategies`` (@strategy). The agent validates unknown names.
# Position protection is part of the strategy, so there is no separate
# protector concept.
KNOWN_STRATEGIES = ("tripple_screen",)


def _now() -> datetime:
    return datetime.now(UTC)


# --- Documents -------------------------------------------------------------


class StartPayload(BaseModel):
    """Payload of the ``start`` command (strategy/symbols/params)."""

    network: Network = "testnet"
    strategy: str
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
    updated_at: datetime


class TradingStatus(BaseModel):
    """Intent (snapshot) + agent liveness (heartbeat) for the UI."""

    desired: RuntimeSnapshot
    actual: AgentHeartbeat | None
    online: bool


class CommandResult(BaseModel):
    """Execution outcome produced by the agent, not an acceptance receipt."""

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
    """Accept intent; publication does not confirm agent execution."""
    envelope = CommandEnvelope(
        id=uuid.uuid4(), type=type_, payload=payload, issued_at=_now()
    )
    if type_ == "balance":
        await _publish(envelope)
        return envelope

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
                REDIS_COMMAND_CHANNEL,
                envelope.model_dump_json(),
            )
            if committed:
                return envelope
    except RedisError as exc:
        raise _redis_unavailable(exc) from exc


def _start_command_payload(parsed: StartPayload) -> dict[str, Any]:
    """Normalize a validated start payload into the command/snapshot payload."""
    return {
        "network": parsed.network,
        "strategy": parsed.strategy,
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
