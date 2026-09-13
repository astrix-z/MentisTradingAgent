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
from app.models import User

NOW = datetime(2026, 9, 12, tzinfo=UTC)
START = {
    "network": "live",
    "strategy": "tripple_screen",
    "symbols": ["btcusdt"],
    "params": {"ema_period": 37, "nested": {"enabled": True}, "empty": []},
}


class MemoryRedis:
    """Model Redis's atomic CAS contract, with forced read/write interleavings."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.publications: list[tuple[str, dict[str, Any], str | None]] = []
        self.conflicts = 0
        self.fail: str | None = None

    async def get(self, key: str) -> str | None:
        if self.fail == "get":
            raise RedisError("read failed")
        raw = self.values.get(key)
        await asyncio.sleep(0)
        return raw

    async def publish(self, channel: str, raw: str) -> int:
        if self.fail == "publish":
            raise RedisError("publish failed")
        self.publications.append(
            (channel, json.loads(raw), self.values.get(trading.REDIS_RUNTIME_KEY))
        )
        return 0  # No subscribers: acceptance still must not claim execution.

    async def execute_command(
        self,
        command: str,
        script: str,
        numkeys: int,
        key: str,
        expected: str,
        snapshot: str,
        channel: str,
        envelope: str,
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
        # No await between comparison, SET and PUBLISH, just like a Redis script.
        self.publications.append((channel, json.loads(envelope), snapshot))
        return 1


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> MemoryRedis:
    store = MemoryRedis()
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
                        {**START, "params": {"index": index}}
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
        assert channel == trading.REDIS_COMMAND_CHANNEL
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
    response = client.post("/api/v1/trading/balance/", headers=headers(client))
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
    response = client.post(
        f"/api/v1/trading/{path}/",
        json=START if path == "start" else None,
        headers=headers(client),
    )
    assert response.status_code == 503
    assert response.json() == {"detail": "Redis is unavailable"}
    assert not redis.publications
    assert not redis.values


@pytest.mark.parametrize("path", ["status", "result"])
def test_read_redis_failure(client: TestClient, redis: MemoryRedis, path: str) -> None:
    redis.fail = "get"
    assert (
        client.get(f"/api/v1/trading/{path}/", headers=headers(client)).status_code
        == 503
    )


@pytest.mark.parametrize("command", ["start", "stop", "balance"])
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
    if command == "balance":
        assert trading.REDIS_RUNTIME_KEY not in redis.values
    else:
        snapshot = json.loads(redis.values[trading.REDIS_RUNTIME_KEY])
        assert snapshot["running"] == (command == "start")
        assert snapshot["revision"] == 1
        assert snapshot["params"] == (START["params"] if command == "start" else {})
        assert snapshot["symbols"] == (["BTCUSDT"] if command == "start" else [])


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
