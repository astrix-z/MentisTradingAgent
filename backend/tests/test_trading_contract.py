"""Standalone trading regressions: pytest --noconftest tests/test_trading_contract.py.

Only the trading router is mounted. Redis is in memory and authentication uses
signed JWTs with an in-memory user lookup; no database or agent is required.
"""

import asyncio
import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from redis.exceptions import RedisError

from app.api import deps
from app.api.routes import trading
from app.core.security import create_access_token
from app.core.strategy_catalog import REDIS_STRATEGIES_KEY
from app.models import User

NOW = datetime(2026, 9, 12, tzinfo=UTC)
START = {
    "network": "live",
    "strategy": "tripple_screen",
    "symbols": ["btcusdt"],
    "params": {"tp_pct": 12},
}


def catalog_payload() -> dict[str, Any]:
    """A strategy catalog in exactly the format the agent publishes.

    Deliberately written out here rather than imported from the agent: the
    backend consumes this wire format, and ``test_strategy_catalog_fidelity``
    separately checks the compiler against the *real* published document. This
    fixture is small but covers every grammar construct the backend enforces
    (enum, anyOf-Decimal, fixed-length array, indexed field, lookup, operators,
    ``{rhs}`` substitution, and extra-property rejection).
    """
    return {
        "version": 1,
        "schema_hash": "fixture-hash",
        "published_at": "2026-09-12T00:00:00+00:00",
        "lookups": {
            "intervals_ms": {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}
        },
        "strategies": [
            {
                "name": "tripple_screen",
                "label": "Triple Screen",
                "description": "Test strategy.",
                "settings_schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "title": "TripleScreenSettings",
                    "properties": {
                        "wave_interval": {
                            "type": "string",
                            "title": "Wave interval",
                            "default": "1h",
                            "enum": ["15m", "1h", "4h"],
                            "ui": {"widget": "interval"},
                        },
                        "ema_tide_periods": {
                            "type": "array",
                            "title": "EMA tide periods",
                            "default": [5, 13, 34],
                            "minItems": 3,
                            "maxItems": 3,
                            "prefixItems": [
                                {"type": "integer", "exclusiveMinimum": 0},
                                {"type": "integer", "exclusiveMinimum": 0},
                                {"type": "integer", "exclusiveMinimum": 0},
                            ],
                        },
                        "kline_window": {
                            "type": "integer",
                            "title": "Kline window",
                            "default": 1000,
                            "exclusiveMinimum": 0,
                        },
                        "tp_pct": {
                            "anyOf": [
                                {"type": "number", "exclusiveMinimum": 0.0},
                                {
                                    "type": "string",
                                    "pattern": "[+-]?0*\\d*\\.?\\d*",
                                },
                            ],
                            "title": "Take profit %",
                            "default": "10",
                        },
                    },
                },
                "constraints": [
                    {
                        "cmp": "lt",
                        "lhs": {"field": ["ema_tide_periods", 0]},
                        "rhs": {"field": ["ema_tide_periods", 1]},
                        "message": "EMA tide periods must be strictly increasing",
                    },
                    {
                        "cmp": "gte",
                        "lhs": {"field": "kline_window"},
                        "rhs": {
                            "op": "max",
                            "args": [
                                {
                                    "op": "add",
                                    "args": [
                                        {"field": ["ema_tide_periods", 2]},
                                        {"const": 1},
                                    ],
                                },
                                {"const": 2},
                            ],
                        },
                        "message": "kline_window must be at least {rhs}",
                    },
                    {
                        "cmp": "eq",
                        "lhs": {"lookup": "intervals_ms", "field": "wave_interval"},
                        "rhs": {"const": 3_600_000},
                        "message": "wave interval must be 1h in this fixture",
                    },
                ],
            }
        ],
    }


class _FakePipe:
    """Mimics ``redis.pipeline(transaction=True)`` for hash writes only."""

    def __init__(self, store: MemoryRedis) -> None:
        self.store = store
        self.ops: list[tuple[str, tuple[Any, ...]]] = []

    def hset(self, key: str, mapping: dict[str, str]) -> _FakePipe:
        self.ops.append(("hset", (key, mapping)))
        return self

    def expire(self, key: str, ttl: int) -> _FakePipe:
        self.ops.append(("expire", (key, ttl)))
        return self

    async def __aenter__(self) -> _FakePipe:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def execute(self) -> list[Any]:
        if self.store.fail == "pipeline":
            raise RedisError("pipeline failed")
        results: list[Any] = []
        for name, args in self.ops:
            if name == "hset":
                key, mapping = args
                self.store.hashes.setdefault(key, {}).update(mapping)
                results.append(len(mapping))
            else:
                results.append(1)
        return results


class MemoryRedis:
    """Model Redis's atomic CAS contract, with forced read/write interleavings."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.publications: list[tuple[str, dict[str, Any], str | None]] = []
        self.conflicts = 0
        self.fail: str | None = None

    async def get(self, key: str) -> str | None:
        if self.fail == "get":
            raise RedisError("read failed")
        raw = self.values.get(key)
        await asyncio.sleep(0)
        return raw

    async def hgetall(self, key: str) -> dict[str, str]:
        if self.fail == "get":
            raise RedisError("read failed")
        return dict(self.hashes.get(key, {}))

    def pipeline(self, transaction: bool = True) -> _FakePipe:
        return _FakePipe(self)

    async def xadd(
        self,
        key: str,
        fields: dict[str, str],
        maxlen: int | None = None,
        approximate: bool | None = None,
    ) -> str:
        if self.fail == "publish":
            raise RedisError("publish failed")
        self.publications.append(
            (
                key,
                json.loads(fields["data"]),
                self.values.get(trading.REDIS_RUNTIME_KEY),
            )
        )
        return "0-1"  # No consumers yet: acceptance still must not claim execution.

    async def execute_command(
        self,
        command: str,
        script: str,
        numkeys: int,
        key: str,
        expected: str,
        snapshot: str,
        stream: str,
        envelope: str,
        maxlen: str,
    ) -> int:
        assert command == "EVAL"
        assert script == trading.COMMIT_RUNTIME_LUA
        assert numkeys == 1
        await asyncio.sleep(0)
        if self.fail == "eval":
            raise RedisError("commit failed")
        if self.values.get(key, "") != expected:
            self.conflicts += 1
            return 0
        self.values[key] = snapshot
        # No await between comparison, SET and XADD, just like a Redis script.
        self.publications.append((stream, json.loads(envelope), snapshot))
        return 1


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> MemoryRedis:
    store = MemoryRedis()
    # A published catalog is the normal state of the system: the backend
    # rejects an invalid Start before Redis, but only using rules the agent
    # declared. Tests that need "the agent has not run yet" delete this key.
    store.values[REDIS_STRATEGIES_KEY] = json.dumps(catalog_payload())
    monkeypatch.setattr(trading, "get_redis", lambda: store)
    monkeypatch.setattr(trading, "_now", lambda: NOW)
    return store


@pytest.fixture
def client(redis: MemoryRedis) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(trading.router, prefix="/api/v1")
    users = {
        "admin": User(
            email="admin@example.com", hashed_password="unused", is_superuser=True
        ),
        "user": User(
            email="user@example.com", hashed_password="unused", is_superuser=False
        ),
    }
    session = Mock()
    session.get.side_effect = lambda _model, user_id: next(
        (user for user in users.values() if str(user.id) == user_id), None
    )
    app.dependency_overrides[deps.get_db] = lambda: session
    with TestClient(app) as test_client:
        test_client.app_state["tokens"] = {
            role: create_access_token(user.id, timedelta(minutes=5))
            for role, user in users.items()
        }
        yield test_client
    assert redis is not None


def headers(client: TestClient, role: str = "admin") -> dict[str, str]:
    return {"Authorization": f"Bearer {client.app_state['tokens'][role]}"}


@pytest.mark.parametrize("path", ["status", "result"])
@pytest.mark.parametrize("role,expected", [(None, 401), ("user", 403), ("admin", 200)])
def test_financial_access(
    client: TestClient,
    redis: MemoryRedis,
    path: str,
    role: str | None,
    expected: int,
) -> None:
    if role != "admin":
        redis.fail = "get"  # Unauthorized calls must not even reach Redis.
    response = client.get(
        f"/api/v1/trading/{path}/",
        headers=headers(client, role) if role else {},
    )
    assert response.status_code == expected


@pytest.mark.parametrize(
    "age,online", [(0, True), (10, True), (11, False), (-1, False)]
)
def test_status_keeps_desired_and_actual_separate(
    client: TestClient,
    redis: MemoryRedis,
    age: int,
    online: bool,
) -> None:
    desired = trading.RuntimeSnapshot(
        running=True,
        network="testnet",
        strategy="rejected_strategy",
        symbols=["ETHUSDT"],
        params={"ema_period": 91},
        updated_at=NOW,
    )
    actual = trading.AgentHeartbeat(
        state="running",
        network="live",
        strategy="tripple_screen",
        balances={"USDT": 125.5},
        updated_at=NOW - timedelta(seconds=age),
    )
    redis.values[trading.REDIS_RUNTIME_KEY] = desired.model_dump_json()
    redis.values[trading.REDIS_HEARTBEAT_KEY] = actual.model_dump_json()
    response = client.get("/api/v1/trading/status/", headers=headers(client))
    assert response.status_code == 200
    assert response.json() == {
        "desired": desired.model_dump(mode="json"),
        "actual": actual.model_dump(mode="json"),
        "online": online,
    }


def test_missing_heartbeat_is_not_confirmed_running(
    client: TestClient,
    redis: MemoryRedis,
) -> None:
    response = client.get("/api/v1/trading/status/", headers=headers(client))
    status = response.json()
    assert status["actual"] is None
    assert status["online"] is False
    assert status["desired"]["running"] is False
    assert status["desired"]["params"] == {}
    client.post("/api/v1/trading/start/", json=START, headers=headers(client))
    status = client.get("/api/v1/trading/status/", headers=headers(client)).json()
    assert status["desired"]["running"] is True
    assert status["actual"] is None
    assert status["online"] is False
    assert trading.REDIS_HEARTBEAT_KEY not in redis.values


def test_start_stop_preserves_form_and_increments_revision(
    client: TestClient,
    redis: MemoryRedis,
) -> None:
    start = client.post("/api/v1/trading/start/", json=START, headers=headers(client))
    assert start.status_code == 202
    assert start.json()["payload"]["symbols"] == ["BTCUSDT"]
    assert "ok" not in start.json()
    stop = client.post("/api/v1/trading/stop/", headers=headers(client))
    assert stop.status_code == 202
    status = client.get("/api/v1/trading/status/", headers=headers(client)).json()
    assert status["desired"] == {
        **START,
        "symbols": ["BTCUSDT"],
        "running": False,
        "last_command": "stop",
        "updated_at": "2026-09-12T00:00:00Z",
        "revision": 2,
    }
    assert len(redis.publications) == 2


@pytest.mark.parametrize(
    "commands", [("start", "stop"), ("stop", "start"), ("start", "start", "stop")]
)
def test_concurrent_commands_retry_without_reordering(
    redis: MemoryRedis,
    commands: tuple[str, ...],
) -> None:
    async def run() -> None:
        tasks = []
        for index, command in enumerate(commands):
            if command == "start":
                payload = trading._start_command_payload(
                    trading.StartPayload.model_validate(
                        {**START, "params": {"tp_pct": 10 + index}}
                    )
                )
                tasks.append(trading._apply_command("start", payload))
            else:
                tasks.append(trading._apply_command("stop", {}))
        envelopes = await asyncio.gather(*tasks)
        assert len({envelope.id for envelope in envelopes}) == len(commands)

    asyncio.run(run())
    assert redis.conflicts > 0
    assert len(redis.publications) == len(commands)
    assert [entry[1]["type"] for entry in redis.publications] == list(commands)
    for revision, (channel, envelope, raw) in enumerate(redis.publications, start=1):
        assert channel == trading.REDIS_COMMAND_STREAM
        assert raw is not None
        snapshot = json.loads(raw)
        assert snapshot["revision"] == revision
        assert snapshot["last_command"] == envelope["type"]
        assert snapshot["running"] == (envelope["type"] == "start")
        if envelope["type"] == "start":
            assert snapshot["params"] == envelope["payload"]["params"]
    assert redis.values[trading.REDIS_RUNTIME_KEY] == redis.publications[-1][2]


def test_old_snapshot_and_balance_do_not_lose_configuration(
    client: TestClient,
    redis: MemoryRedis,
) -> None:
    old = {**START, "running": True, "updated_at": NOW.isoformat()}
    redis.values[trading.REDIS_RUNTIME_KEY] = json.dumps(old)
    response = client.post(
        "/api/v1/trading/balance/",
        json={"network": "testnet"},
        headers=headers(client),
    )
    assert response.status_code == 202
    assert redis.values[trading.REDIS_RUNTIME_KEY] == json.dumps(old)
    client.post("/api/v1/trading/stop/", headers=headers(client))
    snapshot = json.loads(redis.values[trading.REDIS_RUNTIME_KEY])
    assert snapshot["revision"] == 1
    assert snapshot["params"] == old["params"]
    assert snapshot["symbols"] == old["symbols"]


def test_results_are_correlated_and_reads_do_not_write(
    client: TestClient,
    redis: MemoryRedis,
) -> None:
    command_id = uuid.uuid4()
    result = trading.CommandResult(
        command_id=command_id,
        type="start",
        ok=False,
        message="Invalid strategy",
        at=NOW,
    )
    latest = trading.CommandResult(
        command_id=uuid.uuid4(), type="balance", ok=True, at=NOW
    )
    key = f"{trading.REDIS_RESULT_KEY}:{command_id}"
    redis.values[key] = result.model_dump_json()
    redis.values[trading.REDIS_RESULT_KEY] = latest.model_dump_json()
    before = redis.values.copy()
    response = client.get(
        "/api/v1/trading/result/",
        params={"command_id": str(command_id)},
        headers=headers(client),
    )
    assert response.status_code == 200
    assert response.json() == result.model_dump(mode="json")
    assert client.get(
        "/api/v1/trading/result/", headers=headers(client)
    ).json() == latest.model_dump(mode="json")
    assert redis.values == before
    del redis.values[key]  # Agent TTL expired; do not return the latest result.
    assert (
        client.get(
            "/api/v1/trading/result/",
            params={"command_id": str(command_id)},
            headers=headers(client),
        ).json()
        is None
    )
    redis.values[key] = (
        latest.model_dump_json()
    )  # Even a mis-keyed result cannot leak through.
    assert (
        client.get(
            "/api/v1/trading/result/",
            params={"command_id": str(command_id)},
            headers=headers(client),
        ).json()
        is None
    )
    assert (
        client.get(
            "/api/v1/trading/result/",
            params={"command_id": "bad-id"},
            headers=headers(client),
        ).status_code
        == 422
    )


@pytest.mark.parametrize(
    "path,fail", [("start", "eval"), ("stop", "get"), ("balance", "publish")]
)
def test_command_redis_failure_is_not_accepted(
    client: TestClient,
    redis: MemoryRedis,
    path: str,
    fail: str,
) -> None:
    redis.fail = fail
    body: dict[str, Any] | None = None
    if path == "start":
        body = START
    elif path == "balance":
        body = {"network": "testnet"}
    response = client.post(
        f"/api/v1/trading/{path}/",
        json=body,
        headers=headers(client),
    )
    assert response.status_code == 503
    assert response.json() == {"detail": "Redis is unavailable"}
    assert not redis.publications
    assert trading.REDIS_RUNTIME_KEY not in redis.values


@pytest.mark.parametrize("path", ["status", "result", "symbols"])
def test_read_redis_failure(client: TestClient, redis: MemoryRedis, path: str) -> None:
    redis.fail = "get"
    assert (
        client.get(f"/api/v1/trading/{path}/", headers=headers(client)).status_code
        == 503
    )


@pytest.mark.parametrize("command", ["start", "stop", "balance", "symbols"])
def test_generic_commands_share_the_same_commit_path(
    client: TestClient,
    redis: MemoryRedis,
    command: str,
) -> None:
    response = client.post(
        "/api/v1/trading/commands/",
        json={"type": command, "payload": START if command == "start" else {}},
        headers=headers(client),
    )
    assert response.status_code == 202
    assert len(redis.publications) == 1
    assert redis.publications[0][1] == response.json()
    if command in ("balance", "symbols"):
        assert trading.REDIS_RUNTIME_KEY not in redis.values
    else:
        snapshot = json.loads(redis.values[trading.REDIS_RUNTIME_KEY])
        assert snapshot["running"] == (command == "start")
        assert snapshot["revision"] == 1
        assert snapshot["params"] == (START["params"] if command == "start" else {})
        assert snapshot["symbols"] == (["BTCUSDT"] if command == "start" else [])


@pytest.mark.parametrize(
    "payload",
    [
        {**START, "strategy": "unknown_strategy"},
        {**START, "params": {"unknown_param": 1}},
        {**START, "params": {"tp_pct": -1}},  # Decimal branch: tp_pct > 0
        {**START, "params": {"tp_pct": "not-a-number"}},
        {**START, "symbols": []},
        {**START, "symbols": ["not a symbol!"]},
        {**START, "symbols": ["BTCUSDT", "btcusdt"]},  # duplicate after normalizing
        # Cross-field: declared by the agent, enforced by the backend.
        {**START, "params": {"ema_tide_periods": [34, 13, 5]}},
        {**START, "params": {"ema_tide_periods": [5, 13]}},  # fixed length is 3
        {**START, "params": {"kline_window": 10}},
        {**START, "params": {"wave_interval": "15m"}},  # lookup rule
    ],
)
def test_invalid_start_rejected_before_redis(
    client: TestClient,
    redis: MemoryRedis,
    payload: dict[str, Any],
) -> None:
    response = client.post(
        "/api/v1/trading/start/", json=payload, headers=headers(client)
    )
    assert response.status_code == 422
    assert not redis.publications
    assert trading.REDIS_RUNTIME_KEY not in redis.values


def test_cross_field_rejection_carries_the_published_message(
    client: TestClient,
    redis: MemoryRedis,
) -> None:
    """The UI shows the agent's rule text, so its shape is part of the contract."""
    response = client.post(
        "/api/v1/trading/start/",
        json={**START, "params": {"kline_window": 10}},
        headers=headers(client),
    )
    assert response.status_code == 422
    assert [error["msg"] for error in response.json()["detail"]] == [
        "kline_window must be at least 35"
    ]
    assert not redis.publications


def test_start_is_refused_when_no_catalog_is_published(
    client: TestClient,
    redis: MemoryRedis,
) -> None:
    """Without the agent's declaration the backend cannot certify anything."""
    del redis.values[REDIS_STRATEGIES_KEY]
    response = client.post(
        "/api/v1/trading/start/", json=START, headers=headers(client)
    )
    assert response.status_code == 503
    assert "strategy catalog" in response.json()["detail"]
    assert not redis.publications
    assert trading.REDIS_RUNTIME_KEY not in redis.values


@pytest.mark.parametrize(
    "broken",
    [
        "not json at all",
        json.dumps({"version": 99, "schema_hash": "h", "strategies": [{}]}),
        json.dumps({"version": 1, "strategies": []}),  # no schema_hash
    ],
)
def test_start_is_refused_when_the_catalog_is_unusable(
    client: TestClient,
    redis: MemoryRedis,
    broken: str,
) -> None:
    """A catalog that cannot be evaluated must not read as "valid"."""
    redis.values[REDIS_STRATEGIES_KEY] = broken
    response = client.post(
        "/api/v1/trading/start/", json=START, headers=headers(client)
    )
    assert response.status_code == 503
    assert not redis.publications


def test_strategies_endpoint_serves_the_published_catalog(
    client: TestClient,
) -> None:
    response = client.get("/api/v1/trading/strategies/", headers=headers(client))
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert body["schema_hash"] == "fixture-hash"
    assert body["lookups"]["intervals_ms"]["1h"] == 3_600_000
    entry = body["strategies"][0]
    assert entry["name"] == "tripple_screen"
    assert entry["label"] == "Triple Screen"
    # The UI renders from these, so they must survive verbatim.
    assert entry["settings_schema"]["additionalProperties"] is False
    assert entry["settings_schema"]["properties"]["tp_pct"]["title"] == (
        "Take profit %"
    )
    assert len(entry["constraints"]) == 3


def test_strategies_endpoint_reports_a_missing_agent_without_erroring(
    client: TestClient,
    redis: MemoryRedis,
) -> None:
    """An unpublished catalog is a state, not a failure the UI must toast."""
    del redis.values[REDIS_STRATEGIES_KEY]
    response = client.get("/api/v1/trading/strategies/", headers=headers(client))
    assert response.status_code == 200
    assert response.json() == {
        "available": False,
        "schema_hash": None,
        "published_at": None,
        "lookups": {},
        "strategies": [],
    }


def test_balance_requires_network(client: TestClient, redis: MemoryRedis) -> None:
    response = client.post("/api/v1/trading/balance/", headers=headers(client))
    assert response.status_code == 422
    assert not redis.publications


def test_commands_are_durably_enqueued_on_a_stream(
    client: TestClient, redis: MemoryRedis
) -> None:
    """Prompt 5: the command lane is a Redis Stream (XADD), not Pub/Sub."""
    response = client.post(
        "/api/v1/trading/balance/", json={"network": "testnet"}, headers=headers(client)
    )
    assert response.status_code == 202
    assert len(redis.publications) == 1
    assert redis.publications[0][0] == trading.REDIS_COMMAND_STREAM


def test_command_state_tracks_accepted_then_agent_transitions(
    client: TestClient, redis: MemoryRedis
) -> None:
    response = client.post(
        "/api/v1/trading/start/", json=START, headers=headers(client)
    )
    command_id = response.json()["id"]
    state = client.get(
        f"/api/v1/trading/commands/{command_id}/state/", headers=headers(client)
    ).json()
    assert state["status"] == "accepted"
    assert state["type"] == "start"
    assert state["network"] == "live"

    # The agent side owns later transitions; simulate it writing the hash.
    redis.hashes[f"{trading.REDIS_COMMAND_STATE_PREFIX}{command_id}"].update(
        {"status": "succeeded", "updated_at": NOW.isoformat()}
    )
    state = client.get(
        f"/api/v1/trading/commands/{command_id}/state/", headers=headers(client)
    ).json()
    assert state["status"] == "succeeded"


def test_command_state_unknown_id_is_null(client: TestClient) -> None:
    unknown = uuid.uuid4()
    response = client.get(
        f"/api/v1/trading/commands/{unknown}/state/", headers=headers(client)
    )
    assert response.status_code == 200
    assert response.json() is None


def test_openapi_exposes_typed_status_and_optional_id(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    models = schema["components"]["schemas"]
    status = models["TradingStatus"]
    assert set(status["required"]) == {"desired", "actual", "online"}
    assert status["properties"]["desired"]["$ref"].endswith("/RuntimeSnapshot")
    assert {"network", "symbols", "params", "revision"} <= set(
        models["RuntimeSnapshot"]["properties"]
    )
    query = schema["paths"]["/api/v1/trading/result/"]["get"]["parameters"][0]
    assert query["name"] == "command_id"
    assert query["required"] is False
    assert {"type": "string", "format": "uuid"} in query["schema"]["anyOf"]


# --- working symbols --------------------------------------------------------


def universe_payload() -> dict[str, Any]:
    """A symbols document in exactly the format the agent publishes.

    Written out here rather than imported from the agent: this is the wire
    format the backend consumes, and the agent's own tests check that
    ``agent.symbols.build_universe`` produces it.
    """

    def entry(
        symbol: str,
        base_asset: str,
        *,
        volume: str,
        classification: str = "crypto",
        label: str | None = None,
        excluded_reason: str | None = None,
        selected: bool = False,
    ) -> dict[str, Any]:
        return {
            "symbol": symbol,
            "base_asset": base_asset,
            "quote_asset": "USDT",
            "status": "TRADING",
            "spot_allowed": True,
            "quote_volume_24h": volume,
            "classification": classification,
            "classification_label": label,
            "excluded_reason": excluded_reason,
            "selected": selected,
        }

    return {
        "version": 1,
        "generated_at": "2026-09-15T00:00:00+00:00",
        # Fields the backend does not model must not break the read.
        "network": "live",
        "quote_asset": "USDT",
        "usable": 2,
        "total": 3,
        "selected": 1,
        "selected_unusable": 0,
        "symbols": [
            entry(
                "AAPLBUSDT",
                "AAPLB",
                volume="10",
                classification="tokenized_equity",
                label="tokenized equity",
                excluded_reason="tokenized equity",
            ),
            entry("BTCUSDT", "BTC", volume="123.45", selected=True),
            entry("ETHUSDT", "ETH", volume="99.9"),
        ],
    }


def test_symbols_endpoint_serves_the_published_universe(
    client: TestClient, redis: MemoryRedis
) -> None:
    redis.values[trading.REDIS_SYMBOLS_KEY] = json.dumps(universe_payload())
    response = client.get("/api/v1/trading/symbols/", headers=headers(client))
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert body["quote_asset"] == "USDT"
    assert body["usable"] == 2
    assert body["total"] == 3
    assert body["selected"] == 1
    # The reason a symbol cannot be marked is served exactly as the agent wrote
    # it, and None means it can be marked.
    by_symbol = {entry["symbol"]: entry for entry in body["symbols"]}
    assert by_symbol["AAPLBUSDT"]["excluded_reason"] == "tokenized equity"
    assert by_symbol["ETHUSDT"]["excluded_reason"] is None
    assert by_symbol["BTCUSDT"]["selected"] is True
    # Volume stays a Decimal string: the UI sorts it numerically, not lexically.
    assert by_symbol["BTCUSDT"]["quote_volume_24h"] == "123.45"


def test_symbols_endpoint_reports_a_missing_agent_without_erroring(
    client: TestClient,
) -> None:
    """An unpublished universe is a state, not a failure the UI must toast."""
    response = client.get("/api/v1/trading/symbols/", headers=headers(client))
    assert response.status_code == 200
    assert response.json() == {
        "available": False,
        "quote_asset": "USDT",
        "generated_at": None,
        "total": 0,
        "usable": 0,
        "selected": 0,
        "selected_unusable": 0,
        "symbols": [],
    }


@pytest.mark.parametrize(
    "broken",
    [
        "not json at all",
        json.dumps({"symbols": "BTCUSDT"}),
        json.dumps({"symbols": [{"symbol": "BTCUSDT"}]}),  # entry lacks fields
    ],
)
def test_symbols_endpoint_reports_an_unusable_document(
    client: TestClient, redis: MemoryRedis, broken: str
) -> None:
    """A document the backend cannot read must not render as a full table."""
    redis.values[trading.REDIS_SYMBOLS_KEY] = broken
    response = client.get("/api/v1/trading/symbols/", headers=headers(client))
    assert response.status_code == 200
    assert response.json()["available"] is False
    assert response.json()["symbols"] == []


@pytest.mark.parametrize("method", ["get", "post"])
@pytest.mark.parametrize("role,expected", [(None, 401), ("user", 403)])
def test_working_symbols_are_superuser_only(
    client: TestClient,
    redis: MemoryRedis,
    method: str,
    role: str | None,
    expected: int,
) -> None:
    redis.fail = "get"  # Unauthorized calls must not even reach Redis.
    kwargs: dict[str, Any] = (
        {"json": {"symbols": ["BTCUSDT"]}} if method == "post" else {}
    )
    response = getattr(client, method)(
        "/api/v1/trading/symbols/",
        headers=headers(client, role) if role else {},
        **kwargs,
    )
    assert response.status_code == expected


def test_set_symbols_enqueues_a_command(
    client: TestClient, redis: MemoryRedis
) -> None:
    response = client.post(
        "/api/v1/trading/symbols/",
        json={"symbols": ["btcusdt", " ethusdt "]},
        headers=headers(client),
    )
    assert response.status_code == 202
    assert len(redis.publications) == 1
    stream, envelope, _ = redis.publications[0]
    assert stream == trading.REDIS_COMMAND_STREAM
    assert envelope["type"] == "symbols"
    assert envelope["payload"] == {"symbols": ["BTCUSDT", "ETHUSDT"]}
    # Marking symbols says nothing about whether the strategy is running, so the
    # snapshot must be left alone (it would otherwise be rewritten as stopped).
    assert trading.REDIS_RUNTIME_KEY not in redis.values


@pytest.mark.parametrize(
    "payload",
    [
        {"symbols": "BTCUSDT"},  # not a list
        {"symbols": ["BTC USDT"]},  # invalid format
        {"symbols": ["BTCUSDT", "btcusdt"]},  # duplicate after normalizing
    ],
)
def test_invalid_symbols_rejected_before_redis(
    client: TestClient, redis: MemoryRedis, payload: dict[str, Any]
) -> None:
    response = client.post(
        "/api/v1/trading/symbols/", json=payload, headers=headers(client)
    )
    assert response.status_code == 422
    assert not redis.publications
    assert trading.REDIS_RUNTIME_KEY not in redis.values


def test_set_symbols_allows_clearing_the_selection(
    client: TestClient, redis: MemoryRedis
) -> None:
    """An empty set is a real choice (nothing is marked), not a bad request."""
    response = client.post(
        "/api/v1/trading/symbols/", json={"symbols": []}, headers=headers(client)
    )
    assert response.status_code == 202
    assert redis.publications[0][1]["payload"] == {"symbols": []}


def test_generic_symbols_command_shares_the_payload_rules(
    client: TestClient, redis: MemoryRedis
) -> None:
    """The generic lane enforces the same contract as the alias above."""
    response = client.post(
        "/api/v1/trading/commands/",
        json={"type": "symbols", "payload": {"symbols": ["BTC USDT"]}},
        headers=headers(client),
    )
    assert response.status_code == 422
    assert not redis.publications
    assert trading.REDIS_RUNTIME_KEY not in redis.values


def test_openapi_exposes_typed_working_symbols(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    models = schema["components"]["schemas"]
    assert {"excluded_reason", "selected", "quote_volume_24h"} <= set(
        models["SymbolEntry"]["properties"]
    )
    assert {"available", "usable", "selected_unusable", "symbols"} <= set(
        models["WorkingSymbols"]["properties"]
    )
    paths = schema["paths"]["/api/v1/trading/symbols/"]
    assert (
        paths["get"]["responses"]["200"]["content"]["application/json"]["schema"][
            "$ref"
        ].endswith("/WorkingSymbols")
    )
    assert (
        paths["post"]["responses"]["202"]["content"]["application/json"]["schema"][
            "$ref"
        ].endswith("/CommandEnvelope")
    )
