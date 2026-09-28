"""The preset and option-list API against the real database (PRESET-06).

Plan §4.3.2–§4.3.5 and the backend acceptance of §7.1. Redis is a stand-in
(the catalog is one key); PostgreSQL is real, because the name index, the
built-in sync and the option lists are its.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, col, delete, select

from app import crud
from app.api.routes import presets, trading
from app.core import builtin_presets, field_options
from app.core.builtin_presets import SyncOutcome
from app.core.config import settings
from app.core.redis_client import get_sync_redis
from app.core.strategy_catalog import REDIS_STRATEGIES_KEY
from app.main import app
from app.models import (
    StrategyBuiltinSync,
    StrategyFieldOptions,
    StrategyPreset,
    UserCreate,
)
from tests.test_trading_contract import MemoryRedis
from tests.utils.user import user_authentication_headers
from tests.utils.utils import random_email, random_lower_string

ROOT = Path(__file__).resolve().parents[4]
STRATEGY = "tripple_screen"
OTHER = "other_strategy"
UNKNOWN = "no_such_strategy"
API = f"{settings.API_V1_STR}/trading"
PRESETS = f"{API}/strategies/{STRATEGY}/presets/"
OPTIONS = f"{API}/strategies/{STRATEGY}/options/"

#: The agent's real published catalog, plus a second strategy for the rules
#: that are per strategy.
CATALOG: dict[str, Any] = json.loads(
    (ROOT / "frontend/tests/fixtures/strategy-catalog.json").read_text()
)
_other = copy.deepcopy(CATALOG["strategies"][0])
_other["name"] = OTHER
_other.pop("presets", None)
CATALOG["strategies"].append(_other)
CATALOG_RAW = json.dumps(CATALOG)

FIXTURE = json.loads((ROOT / "tests/fixtures/validation-edge-cases.json").read_text())
DEFAULT: dict[str, Any] = FIXTURE["baseline"]["params"]
#: What the migration seeds (T2); restored after every test.
SEEDED: dict[str, list[str]] = FIXTURE["baseline"]["options"]
START = {"network": "testnet", "strategy": STRATEGY, "symbols": ["BTCUSDT"]}


class SyncRedis:
    """The blocking client of the built-in sync: one key, optionally gated."""

    def __init__(self, raw: str | None) -> None:
        self.raw = raw
        self.fail = False

    def get(self, key: str) -> str | None:
        assert key == REDIS_STRATEGIES_KEY
        if self.fail:
            raise RedisError("down")
        return self.raw


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> MemoryRedis:
    store = MemoryRedis()
    store.values[REDIS_STRATEGIES_KEY] = CATALOG_RAW
    monkeypatch.setattr(trading, "get_redis", lambda: store)
    monkeypatch.setattr(presets, "get_redis", lambda: store)
    return store


@pytest.fixture
def sync_redis() -> Iterator[SyncRedis]:
    fake = SyncRedis(CATALOG_RAW)
    app.dependency_overrides[get_sync_redis] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_sync_redis, None)


pytestmark = pytest.mark.usefixtures("redis", "sync_redis")


@pytest.fixture(autouse=True)
def clean(db: Session) -> Iterator[None]:
    yield
    for strategy in (STRATEGY, OTHER):
        db.execute(
            delete(StrategyPreset).where(col(StrategyPreset.strategy) == strategy)
        )
        db.execute(
            delete(StrategyBuiltinSync).where(
                col(StrategyBuiltinSync.strategy) == strategy
            )
        )
        db.execute(
            delete(StrategyFieldOptions).where(
                col(StrategyFieldOptions.strategy) == strategy
            )
        )
    for field, options in SEEDED.items():
        db.add(StrategyFieldOptions(strategy=STRATEGY, field=field, options=options))
    db.commit()


def set_list(db: Session, field: str, options: list[str] | None) -> None:
    db.execute(
        delete(StrategyFieldOptions).where(
            col(StrategyFieldOptions.strategy) == STRATEGY,
            col(StrategyFieldOptions.field) == field,
        )
    )
    if options is not None:
        db.add(StrategyFieldOptions(strategy=STRATEGY, field=field, options=options))
    db.commit()


def post(
    client: TestClient, headers: dict[str, str], name: str, **params: Any
) -> httpx.Response:
    response: httpx.Response = client.post(
        PRESETS, headers=headers, json={"name": name, "params": params}
    )
    return response


# --- GET presets/ and the built-in sync -----------------------------------------


def test_get_presets_syncs_the_builtins_and_checks_every_preset(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    mine = post(client, superuser_token_headers, "Mine", tp_pct="12")
    assert mine.status_code == 201, mine.json()
    body = client.get(PRESETS, headers=superuser_token_headers).json()
    assert body["builtin_sync"]["status"] == "ok"
    assert body["builtin_sync"]["synced_at"] is not None
    names = [
        (item["name"], item["is_builtin"], item["valid"]) for item in body["items"]
    ]
    assert names == [
        ("Default", True, True),
        ("Fast", True, True),
        ("Mine", False, True),
    ]


def test_without_a_catalog_presets_are_served_unchecked_and_unsynced(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    redis: MemoryRedis,
    db: Session,
) -> None:
    assert post(client, superuser_token_headers, "Mine").status_code == 201
    for broken in ("missing", "redis"):
        if broken == "missing":
            del redis.values[REDIS_STRATEGIES_KEY]
        else:
            redis.values[REDIS_STRATEGIES_KEY] = CATALOG_RAW
            redis.fail = "get"
        body = client.get(PRESETS, headers=superuser_token_headers).json()
        assert [(i["name"], i["valid"], i["errors"]) for i in body["items"]] == [
            ("Mine", None, [])
        ]
        assert body["builtin_sync"] == {
            "status": "never",
            "synced_at": None,
            "errors": [],
        }
    assert db.get(StrategyBuiltinSync, STRATEGY) is None


def test_a_failed_sync_serves_the_last_recorded_state(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    sync_redis: SyncRedis,
) -> None:
    first = client.get(PRESETS, headers=superuser_token_headers).json()
    sync_redis.fail = True
    again = client.get(PRESETS, headers=superuser_token_headers)
    assert again.status_code == 200
    assert again.json()["builtin_sync"] == first["builtin_sync"]
    assert [i["name"] for i in again.json()["items"]] == ["Default", "Fast"]


def test_two_requests_in_one_event_loop_keep_the_loop_serving(
    superuser_token_headers: dict[str, str],
    sync_redis: SyncRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R99, R104, R111: the sync must run off the event loop.

    The first sync's Redis read waits for a permission that only a coroutine
    of the same loop can give. A route that ran the sync on the loop would
    hold it, the permission would never come, the read would time out after
    the 2 s watchdog and the sync would fail.
    """
    entered, allowed = threading.Event(), threading.Event()
    permits: list[bool] = []

    def gated_get(key: str) -> str | None:
        entered.set()
        permits.append(allowed.wait(timeout=2))
        if not permits[-1]:
            raise RedisTimeoutError("no permission within 2 s: the loop was blocked")
        return SyncRedis.get(sync_redis, key)

    monkeypatch.setattr(sync_redis, "get", gated_get)
    outcomes: list[SyncOutcome] = []
    real_sync = builtin_presets.sync_builtin_presets

    def recording_sync(*args: Any) -> SyncOutcome:
        outcomes.append(real_sync(*args))
        return outcomes[-1]

    monkeypatch.setattr(presets, "sync_builtin_presets", recording_sync)

    async def permit() -> None:
        while not entered.is_set():  # never block the loop while waiting
            await asyncio.sleep(0.005)
        allowed.set()

    async def two_requests() -> list[httpx.Response]:
        permitter = asyncio.create_task(permit())
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                return list(
                    await asyncio.gather(
                        client.get(PRESETS, headers=superuser_token_headers),
                        client.get(PRESETS, headers=superuser_token_headers),
                    )
                )
        finally:
            permitter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await permitter

    responses = asyncio.run(asyncio.wait_for(two_requests(), timeout=30))
    assert [r.status_code for r in responses] == [200, 200]
    assert permits and all(permits), "the loop was blocked while a sync ran"
    assert sorted(o.result for o in outcomes) == ["applied", "noop"]


# --- Common rules ----------------------------------------------------------------


ENDPOINTS: list[tuple[str, str, dict[str, Any] | None]] = [
    ("GET", "presets/", None),
    ("POST", "presets/", {"name": "X", "params": {}}),
    ("PUT", f"presets/{uuid.uuid4()}", {"name": "X", "params": {}}),
    ("DELETE", f"presets/{uuid.uuid4()}", None),
    ("GET", "options/", None),
    ("PUT", "options/quote_asset", {"options": ["USDT"]}),
]


@pytest.mark.parametrize("method,path,body", ENDPOINTS)
def test_a_normal_user_gets_403(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    response = client.request(
        method,
        f"{API}/strategies/{STRATEGY}/{path}",
        headers=normal_user_token_headers,
        json=body,
    )
    assert response.status_code == 403


@pytest.mark.parametrize("catalog", [True, False])
@pytest.mark.parametrize("method,path,body", ENDPOINTS)
def test_an_unknown_strategy_is_404_with_or_without_a_catalog(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    redis: MemoryRedis,
    catalog: bool,
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    if not catalog:
        del redis.values[REDIS_STRATEGIES_KEY]
    response = client.request(
        method,
        f"{API}/strategies/{UNKNOWN}/{path}",
        headers=superuser_token_headers,
        json=body,
    )
    assert response.status_code == 404, response.json()


def test_redis_down_is_no_catalog(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    redis: MemoryRedis,
) -> None:
    redis.fail = "get"
    assert client.get(PRESETS, headers=superuser_token_headers).status_code == 200
    assert post(client, superuser_token_headers, "Mine").status_code == 503
    saved = client.put(
        f"{OPTIONS}quote_asset",
        headers=superuser_token_headers,
        json={"options": ["USDC"]},
    )
    assert saved.status_code == 200
    assert saved.json() == {"options": ["USDC"], "unsupported": None}


def test_a_database_error_is_503(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    redis: MemoryRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*_: Any) -> Any:
        raise OperationalError("select", {}, Exception("connection lost"))

    monkeypatch.setattr(presets, "read_options", broken)
    monkeypatch.setattr(trading, "read_options", broken)
    assert client.get(OPTIONS, headers=superuser_token_headers).status_code == 503
    assert post(client, superuser_token_headers, "Mine").status_code == 503
    start = client.post(
        f"{API}/start/", headers=superuser_token_headers, json={**START, "params": {}}
    )
    assert start.status_code == 503
    assert not redis.publications


def test_an_integrity_error_other_than_the_name_is_500(
    superuser_token_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A foreign-key violation (an author that does not exist) is not ``409``."""
    real = StrategyPreset

    def with_missing_author(**fields: Any) -> StrategyPreset:
        return real(**{**fields, "created_by": uuid.uuid4()})

    monkeypatch.setattr(presets, "StrategyPreset", with_missing_author)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = post(client, superuser_token_headers, "Mine")
    assert response.status_code == 500


# --- Saving ---------------------------------------------------------------------


def test_a_partial_preset_is_stored_as_a_full_snapshot(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    response = post(client, superuser_token_headers, "  Partial  ", tp_pct="12")
    assert response.status_code == 201, response.json()
    row = db.get(StrategyPreset, uuid.UUID(response.json()["id"]))
    assert row is not None
    assert row.name == "Partial"
    assert row.params == {**DEFAULT, "tp_pct": "12"}
    assert row.is_builtin is False


@pytest.mark.parametrize("name", ["Default", " default ", "FAST", "fast"])
def test_a_builtin_name_is_reserved(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    name: str,
) -> None:
    assert post(client, superuser_token_headers, name).status_code == 409


@pytest.mark.parametrize("name", ["", "   ", "x" * 65, "Tab\there", "NUL\u0000"])
def test_a_bad_name_is_422_at_body_name(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    name: str,
) -> None:
    response = post(client, superuser_token_headers, name)
    assert response.status_code == 422
    assert [e["loc"] for e in response.json()["detail"]] == [["body", "name"]]


def test_names_are_unique_per_strategy_ignoring_case(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    assert post(client, superuser_token_headers, "Alpha").status_code == 201
    assert post(client, superuser_token_headers, "alpha").status_code == 409
    other = client.post(
        f"{API}/strategies/{OTHER}/presets/",
        headers=superuser_token_headers,
        json={"name": "alpha", "params": {}},
    )
    assert other.status_code == 201, other.json()


def test_concurrent_alpha_and_alpha_give_one_201_and_one_409(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    start = threading.Barrier(2)
    codes: list[int] = []

    def create(name: str) -> None:
        start.wait()
        codes.append(post(client, superuser_token_headers, name).status_code)

    threads = [threading.Thread(target=create, args=(n,)) for n in ("Alpha", "alpha")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert sorted(codes) == [201, 409]


def test_invalid_params_are_422_at_body_params_and_nothing_is_stored(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    response = post(client, superuser_token_headers, "Bad", tp_pct=0)
    assert response.status_code == 422
    assert all(e["loc"][:2] == ["body", "params"] for e in response.json()["detail"])
    old = post(client, superuser_token_headers, "Old", swing_low_h1_n=7)
    assert old.status_code == 422  # an old name is refused, as on Start
    assert "renamed" in old.json()["detail"][0]["msg"]
    assert (
        db.exec(select(StrategyPreset).where(StrategyPreset.strategy == STRATEGY)).all()
        == []
    )


def test_a_preset_outside_the_lists_cannot_be_saved(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    set_list(db, "quote_asset", ["USDC"])
    response = post(client, superuser_token_headers, "Usdt")  # default USDT
    assert response.status_code == 422
    assert [(e["loc"], e["type"]) for e in response.json()["detail"]] == [
        (["body", "params", "quote_asset"], "option_value")
    ]


# --- PUT and DELETE ---------------------------------------------------------------


def test_put_replaces_the_snapshot_and_renames(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    preset_id = post(client, superuser_token_headers, "Mine").json()["id"]
    response = client.put(
        f"{PRESETS}{preset_id}",
        headers=superuser_token_headers,
        json={"name": "Renamed", "params": {"tp_pct": "15"}},
    )
    assert response.status_code == 200, response.json()
    row = db.get(StrategyPreset, uuid.UUID(preset_id))
    assert row is not None
    db.refresh(row)
    assert (row.name, row.params) == ("Renamed", {**DEFAULT, "tp_pct": "15"})
    to_fast = client.put(
        f"{PRESETS}{preset_id}",
        headers=superuser_token_headers,
        json={"name": " fast ", "params": {}},
    )
    assert to_fast.status_code == 409


def test_put_and_delete_find_the_id_within_the_strategy_only(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    preset_id = post(client, superuser_token_headers, "Mine").json()["id"]
    elsewhere = f"{API}/strategies/{OTHER}/presets/{preset_id}"
    body = {"name": "Mine", "params": {}}
    assert (
        client.put(elsewhere, headers=superuser_token_headers, json=body).status_code
        == 404
    )
    assert client.delete(elsewhere, headers=superuser_token_headers).status_code == 404
    assert (
        client.delete(
            f"{PRESETS}{preset_id}", headers=superuser_token_headers
        ).status_code
        == 204
    )
    assert (
        client.delete(
            f"{PRESETS}{preset_id}", headers=superuser_token_headers
        ).status_code
        == 404
    )


def test_builtin_presets_are_read_only(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    items = client.get(PRESETS, headers=superuser_token_headers).json()["items"]
    fast = next(item for item in items if item["name"] == "Fast")
    url = f"{PRESETS}{fast['id']}"
    body = {"name": "Fast", "params": {}}
    assert (
        client.put(url, headers=superuser_token_headers, json=body).status_code == 409
    )
    assert client.delete(url, headers=superuser_token_headers).status_code == 409


def test_delete_works_without_a_catalog(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    redis: MemoryRedis,
) -> None:
    preset_id = post(client, superuser_token_headers, "Mine").json()["id"]
    redis.fail = "get"
    response = client.delete(f"{PRESETS}{preset_id}", headers=superuser_token_headers)
    assert response.status_code == 204


def test_deleting_the_author_keeps_the_preset(
    client: TestClient,
    db: Session,
) -> None:
    password = random_lower_string()
    author = crud.create_user(
        session=db,
        user_create=UserCreate(
            email=random_email(), password=password, is_superuser=True
        ),
    )
    author_headers = user_authentication_headers(
        client=client, email=author.email, password=password
    )
    preset_id = post(client, author_headers, "Authored").json()["id"]
    db.delete(author)
    db.commit()
    row = db.get(StrategyPreset, uuid.UUID(preset_id))
    assert row is not None
    db.refresh(row)
    assert row.created_by is None


# --- Reading against a changed schema ----------------------------------------------


def stored(db: Session, name: str, params: dict[str, Any]) -> uuid.UUID:
    row = StrategyPreset(strategy=STRATEGY, name=name, params=params)
    db.add(row)
    db.commit()
    return row.id


def test_a_preset_saved_with_old_names_reads_valid_and_put_stores_new_names(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    old = {
        k: v
        for k, v in DEFAULT.items()
        if k not in ("swing_low_wave_n", "trail_ripple_n")
    }
    preset_id = stored(db, "Old", {**old, "swing_low_h1_n": 7, "trail_m15_n": 9})
    items = client.get(PRESETS, headers=superuser_token_headers).json()["items"]
    item = next(i for i in items if i["name"] == "Old")
    assert (item["valid"], item["errors"]) == (True, [])
    moved = {**old, "swing_low_wave_n": 7, "trail_ripple_n": 9}
    response = client.put(
        f"{PRESETS}{preset_id}",
        headers=superuser_token_headers,
        json={"name": "Old", "params": moved},
    )
    assert response.status_code == 200, response.json()
    row = db.get(StrategyPreset, preset_id)
    assert row is not None
    db.refresh(row)
    assert row.params == moved


def test_a_new_field_makes_a_saved_preset_invalid(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    lacking = {k: v for k, v in DEFAULT.items() if k != "tp_pct"}
    stored(db, "Lacking", lacking)
    stored(db, "Extra", {**DEFAULT, "future_param": 1})
    items = {
        i["name"]: i
        for i in client.get(PRESETS, headers=superuser_token_headers).json()["items"]
    }
    assert items["Lacking"]["valid"] is False
    assert [(e["loc"], e["type"]) for e in items["Lacking"]["errors"]] == [
        (["body", "params", "tp_pct"], "missing_in_preset")
    ]
    assert items["Extra"]["valid"] is False
    assert [e["type"] for e in items["Extra"]["errors"]] == ["unknown_param"]


def test_a_changed_default_does_not_move_a_saved_preset(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    redis: MemoryRedis,
    sync_redis: SyncRedis,
) -> None:
    preset_id = post(client, superuser_token_headers, "Mine").json()["id"]
    changed = copy.deepcopy(CATALOG)
    changed["strategies"][0]["settings_schema"]["properties"]["tp_pct"]["default"] = (
        "11"
    )
    redis.values[REDIS_STRATEGIES_KEY] = sync_redis.raw = json.dumps(changed)
    items = client.get(PRESETS, headers=superuser_token_headers).json()["items"]
    mine = next(i for i in items if i["id"] == preset_id)
    assert (mine["params"]["tp_pct"], mine["valid"]) == (DEFAULT["tp_pct"], True)


def test_a_list_change_makes_a_saved_preset_invalid(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    post(client, superuser_token_headers, "Mine")
    set_list(db, "ripple_interval", ["1h", "5m"])  # the default 15m is gone
    items = client.get(PRESETS, headers=superuser_token_headers).json()["items"]
    mine = next(i for i in items if i["name"] == "Mine")
    assert mine["valid"] is False
    assert [e["type"] for e in mine["errors"]] == ["option_value"]


# --- Start and the lists (step 5) -----------------------------------------------------


def start(client: TestClient, headers: dict[str, str], **params: Any) -> httpx.Response:
    response: httpx.Response = client.post(
        f"{API}/start/", headers=headers, json={**START, "params": params}
    )
    return response


def command_start(
    client: TestClient, headers: dict[str, str], **params: Any
) -> httpx.Response:
    response: httpx.Response = client.post(
        f"{API}/commands/",
        headers=headers,
        json={"type": "start", "payload": {**START, "params": params}},
    )
    return response


@pytest.mark.parametrize("route", [start, command_start])
def test_start_checks_the_effective_value_against_the_list(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    redis: MemoryRedis,
    db: Session,
    route: Any,
) -> None:
    set_list(db, "quote_asset", ["USDC"])
    for params in ({}, {"tp_pct": "12"}):
        response = route(client, superuser_token_headers, **params)
        assert response.status_code == 422, response.json()
        assert [(e["loc"], e["type"]) for e in response.json()["detail"]] == [
            (["body", "params", "quote_asset"], "option_value")
        ]
    assert not redis.publications
    assert trading.REDIS_RUNTIME_KEY not in redis.values
    ok = route(client, superuser_token_headers, quote_asset="USDC")
    assert ok.status_code == 202, ok.json()


@pytest.mark.parametrize("route", [start, command_start])
def test_a_field_without_a_list_is_checked_by_the_catalog_only(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
    route: Any,
) -> None:
    set_list(db, "ripple_interval", None)
    ok = route(client, superuser_token_headers, ripple_interval="3m")  # in S, no list
    assert ok.status_code == 202, ok.json()
    for field in field_options.OPTION_FIELDS:
        set_list(db, field, None)
    assert route(client, superuser_token_headers).status_code == 202


def test_rule_and_list_errors_are_reported_together(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    set_list(db, "quote_asset", ["USDC"])
    response = start(client, superuser_token_headers, ema_tide_periods=[34, 13, 5])
    assert response.status_code == 422
    assert {e["type"] for e in response.json()["detail"]} == {
        "value_error",
        "option_value",
    }


def test_start_without_a_catalog_is_503(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    redis: MemoryRedis,
) -> None:
    del redis.values[REDIS_STRATEGIES_KEY]
    assert start(client, superuser_token_headers).status_code == 503
    assert not redis.publications


# --- Option lists ------------------------------------------------------------------


def test_get_options_reports_values_outside_s(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    redis: MemoryRedis,
    db: Session,
) -> None:
    set_list(db, "ripple_interval", ["15m", "1M", "custom"])
    body = client.get(OPTIONS, headers=superuser_token_headers).json()
    assert body["quote_asset"] == {"options": SEEDED["quote_asset"], "unsupported": []}
    assert body["ripple_interval"] == {
        "options": ["15m", "1M", "custom"],
        "unsupported": ["1M", "custom"],
    }
    redis.fail = "get"
    unknown = client.get(OPTIONS, headers=superuser_token_headers).json()
    assert unknown["ripple_interval"]["unsupported"] is None


def test_put_options_normalizes_and_keeps_unsupported_values(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    response = client.put(
        f"{OPTIONS}ripple_interval",
        headers=superuser_token_headers,
        json={"options": [" H1 ", "M15", "1m", "W1", "1M", "custom", "m1"]},
    )
    assert response.status_code == 200, response.json()
    assert response.json() == {
        "options": ["1h", "15m", "1m", "1w", "1M", "custom", "m1"],
        "unsupported": ["1M", "custom", "m1"],
    }
    row = db.get(StrategyFieldOptions, (STRATEGY, "ripple_interval"))
    assert row is not None
    db.refresh(row)
    assert row.options == response.json()["options"]


@pytest.mark.parametrize(
    "options",
    [[], ["1h", " "], ["H1", "1h"], ["USDT"] * 2],
)
def test_put_options_rejects_empty_and_duplicate_values(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    options: list[str],
) -> None:
    field = "quote_asset" if options == ["USDT"] * 2 else "tide_interval"
    response = client.put(
        f"{OPTIONS}{field}", headers=superuser_token_headers, json={"options": options}
    )
    assert response.status_code == 422


def test_put_options_on_an_unknown_field_is_404(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    response = client.put(
        f"{OPTIONS}tp_pct", headers=superuser_token_headers, json={"options": ["1"]}
    )
    assert response.status_code == 404


def test_a_value_outside_s_on_a_list_is_refused_by_start(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    set_list(db, "ripple_interval", ["1M", "15m"])
    response = start(client, superuser_token_headers, ripple_interval="1M")
    assert response.status_code == 422
    assert [e["type"] for e in response.json()["detail"]] == ["unsupported_value"]
