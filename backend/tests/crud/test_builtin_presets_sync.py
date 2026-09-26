"""PRESET-05b: the built-in preset sync against the real database (plan §4.3.2).

Redis is a stand-in (the sync reads one key); PostgreSQL is real, because the
guarantees under test are its: the lock row, the upsert on the expression
index, reads after a concurrent insert, ``lower()`` of the index.
"""

import asyncio
import json
import threading
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlmodel import Session, col, delete, select

from app.core import builtin_presets, strategy_catalog
from app.core.builtin_presets import SyncOutcome, sync_builtin_presets
from app.core.db import engine
from app.models import StrategyBuiltinSync, StrategyPreset

STRATEGY = "sync_test_strategy"

DEFAULT = {"interval": "1h", "window": 10, "ratio": "1.5"}
FAST = {"interval": "4h", "window": 5, "ratio": "2"}


def document(
    presets: Any = None,
    *,
    published_at: str = "2026-09-26T00:00:00+00:00",
    **extra: Any,
) -> dict[str, Any]:
    """A compilable catalog with one strategy; ``presets=None`` omits the field."""
    entry: dict[str, Any] = {
        "name": STRATEGY,
        "label": "Sync test",
        "settings_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "interval": {"type": "string", "enum": ["1h", "4h"], "default": "1h"},
                "window": {"type": "integer", "default": 10, "exclusiveMinimum": 0},
                "ratio": {
                    "anyOf": [
                        {"type": "number", "exclusiveMinimum": 0.0},
                        {"type": "string"},
                    ],
                    "default": "1.5",
                },
            },
        },
        "constraints": [
            {
                "cmp": "gte",
                "lhs": {"field": "window"},
                "rhs": {"const": 5},
                "message": "window must be at least {rhs}",
            }
        ],
    }
    if presets is not None:
        entry["presets"] = presets
    entry.update(extra)
    return {
        "version": strategy_catalog.CATALOG_VERSION,
        "schema_hash": "sync-test",
        "published_at": published_at,
        "lookups": {"intervals_ms": {"1h": 3_600_000, "4h": 14_400_000}},
        "strategies": [entry],
    }


BOTH = [{"name": "Default", "params": DEFAULT}, {"name": "Fast", "params": FAST}]


class FakeRedis:
    """The one key the sync reads; ``on_get`` runs inside the sync's lock."""

    def __init__(self, doc: dict[str, Any] | None) -> None:
        self.raw = json.dumps(doc) if doc is not None else None
        self.on_get: Callable[[], Any] | None = None

    def publish(self, doc: dict[str, Any]) -> None:
        self.raw = json.dumps(doc)

    async def get(self, key: str) -> str | None:
        assert key == strategy_catalog.REDIS_STRATEGIES_KEY
        raw = self.raw
        if self.on_get is not None:
            result = self.on_get()
            if asyncio.iscoroutine(result):
                await result
        return raw


def sync(redis: FakeRedis) -> SyncOutcome:
    """One sync in its own session, as one request of one worker."""
    with Session(engine) as session:
        return asyncio.run(sync_builtin_presets(session, redis, STRATEGY))  # type: ignore[arg-type]


def rows() -> dict[str, StrategyPreset]:
    with Session(engine) as session:
        found = session.exec(
            select(StrategyPreset).where(StrategyPreset.strategy == STRATEGY)
        ).all()
        return {row.name: row for row in found}


def state() -> StrategyBuiltinSync | None:
    with Session(engine) as session:
        return session.get(StrategyBuiltinSync, STRATEGY)


def add_user_preset(name: str, params: dict[str, Any] | None = None) -> None:
    with Session(engine) as session:
        session.add(
            StrategyPreset(strategy=STRATEGY, name=name, params=params or DEFAULT)
        )
        session.commit()


@pytest.fixture(autouse=True)
def clean() -> Iterator[None]:
    yield
    with Session(engine) as session:
        session.execute(
            delete(StrategyPreset).where(col(StrategyPreset.strategy) == STRATEGY)
        )
        session.execute(
            delete(StrategyBuiltinSync).where(
                col(StrategyBuiltinSync.strategy) == STRATEGY
            )
        )
        session.commit()


# --- Writing and idempotency -------------------------------------------------


def test_the_first_sync_writes_the_accepted_presets() -> None:
    outcome = sync(FakeRedis(document(BOTH)))
    assert (outcome.result, outcome.status, outcome.errors) == ("applied", "ok", [])
    found = rows()
    assert {name: row.params for name, row in found.items()} == {
        "Default": DEFAULT,
        "Fast": FAST,
    }
    assert all(row.is_builtin and row.retired_at is None for row in found.values())
    sync_row = state()
    assert sync_row is not None
    assert (sync_row.status, sync_row.errors) == ("ok", [])
    assert sync_row.synced_at is not None and len(sync_row.content_hash) == 64


def test_a_republished_unchanged_catalog_is_a_noop_without_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = FakeRedis(document(BOTH))
    sync(redis)
    before = {name: row.updated_at for name, row in rows().items()}
    parses: list[Any] = []
    real = strategy_catalog.CompiledStrategy.parse_presets

    def spy(self: strategy_catalog.CompiledStrategy) -> Any:
        parses.append(self)
        return real(self)

    monkeypatch.setattr(strategy_catalog.CompiledStrategy, "parse_presets", spy)
    redis.publish(document(BOTH, published_at="2026-09-27T00:00:00+00:00"))
    assert sync(redis).result == "noop"
    assert parses == []
    assert {name: row.updated_at for name, row in rows().items()} == before


def test_an_older_published_at_with_new_content_is_applied() -> None:
    redis = FakeRedis(document(BOTH))
    sync(redis)
    changed = [BOTH[0], {"name": "Fast", "params": {**FAST, "window": 6}}]
    redis.publish(document(changed, published_at="2020-01-01T00:00:00+00:00"))
    assert sync(redis).result == "applied"
    assert rows()["Fast"].params["window"] == 6


def test_what_is_applied_is_what_redis_holds_under_the_lock() -> None:
    """A worker that "read" A earlier syncs B if B is what Redis now holds."""
    redis = FakeRedis(document(BOTH))
    changed = [BOTH[0], {"name": "Fast", "params": {**FAST, "window": 7}}]
    redis.publish(document(changed))
    sync(redis)
    assert rows()["Fast"].params["window"] == 7


def test_a_to_b_to_a_across_workers_ends_at_a() -> None:
    a = document(BOTH)
    b = document([BOTH[0], {"name": "Fast", "params": {**FAST, "window": 8}}])
    redis = FakeRedis(a)
    assert sync(redis).result == "applied"  # worker 1 applies A
    redis.publish(b)
    assert sync(redis).result == "applied"  # worker 2 applies B
    redis.publish(a)
    assert sync(redis).result == "applied"  # worker 1 again: no stale local cache
    assert rows()["Fast"].params == FAST


def test_a_publication_during_the_sync_waits_for_the_next_request() -> None:
    a = document(BOTH)
    b = document([BOTH[0], {"name": "Fast", "params": {**FAST, "window": 9}}])
    redis = FakeRedis(a)
    redis.on_get = lambda: redis.publish(b)  # B lands after A was read
    sync(redis)
    assert rows()["Fast"].params == FAST  # A committed
    redis.on_get = None
    assert sync(redis).result == "applied"
    assert rows()["Fast"].params["window"] == 9


def test_changed_legacy_keys_reparse_an_unchanged_list() -> None:
    old = {"interval": "1h", "size": 10, "ratio": "1.5"}
    presets = [{"name": "Default", "params": old}]
    redis = FakeRedis(document(presets))
    outcome = sync(redis)
    assert outcome.status == "partial"
    assert outcome.errors == [{"index": 0, "name": "Default", "type": "rejected"}]
    assert rows() == {}
    redis.publish(document(presets, legacy_keys={"size": "window"}))
    outcome = sync(redis)
    assert (outcome.result, outcome.status) == ("applied", "ok")
    assert rows()["Default"].params == DEFAULT


def test_a_new_pipeline_version_reparses(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = FakeRedis(document(BOTH))
    sync(redis)
    assert sync(redis).result == "noop"
    monkeypatch.setattr(
        builtin_presets,
        "PRESETS_PIPELINE_VERSION",
        builtin_presets.PRESETS_PIPELINE_VERSION + 1,
    )
    assert sync(redis).result == "applied"


# --- Retirement ---------------------------------------------------------------


def test_a_vanished_preset_is_retired_and_comes_back_with_its_id() -> None:
    redis = FakeRedis(document(BOTH))
    sync(redis)
    fast_id = rows()["Fast"].id
    redis.publish(document([BOTH[0]]))
    sync(redis)
    assert rows()["Fast"].retired_at is not None
    assert rows()["Default"].retired_at is None
    redis.publish(document(BOTH))
    sync(redis)
    fast = rows()["Fast"]
    assert (fast.id, fast.retired_at) == (fast_id, None)


def test_an_explicit_empty_list_retires_every_builtin() -> None:
    redis = FakeRedis(document(BOTH))
    sync(redis)
    redis.publish(document([]))
    assert sync(redis).status == "ok"
    assert all(row.retired_at is not None for row in rows().values())


def test_no_presets_field_changes_nothing() -> None:
    redis = FakeRedis(document(BOTH))
    sync(redis)
    before = state()
    redis.publish(document())  # an older agent: no presets at all
    assert sync(redis).result == "absent"
    assert all(row.retired_at is None for row in rows().values())
    after = state()
    assert after is not None and before is not None
    assert after.content_hash == before.content_hash


def test_a_malformed_list_changes_nothing() -> None:
    redis = FakeRedis(document(BOTH))
    sync(redis)
    redis.publish(document({"name": "Default"}))
    assert sync(redis).result == "malformed"
    assert all(row.retired_at is None for row in rows().values())


# --- Parse errors -------------------------------------------------------------


def test_a_broken_fast_beside_a_valid_default() -> None:
    redis = FakeRedis(document(BOTH))
    sync(redis)
    fast = rows()["Fast"]
    new_default = {**DEFAULT, "window": 11}
    redis.publish(
        document(
            [
                {"name": "Default", "params": new_default},
                {"name": "Fast", "params": {**FAST, "window": 3}},  # breaks the rule
            ]
        )
    )
    outcome = sync(redis)
    assert outcome.status == "partial"
    assert outcome.errors == [{"index": 1, "name": "Fast", "type": "rejected"}]
    found = rows()
    assert found["Default"].params == new_default
    assert (found["Fast"].params, found["Fast"].retired_at) == (fast.params, None)


def test_an_unreadable_entry_blocks_retirement() -> None:
    redis = FakeRedis(document(BOTH))
    sync(redis)
    redis.publish(document([BOTH[0], None]))
    outcome = sync(redis)
    assert outcome.errors == [{"index": 1, "name": None, "type": "unreadable"}]
    assert rows()["Fast"].retired_at is None  # the list is not authoritative
    sync_row = state()
    assert sync_row is not None
    assert (sync_row.status, sync_row.errors) == ("partial", outcome.errors)


@pytest.mark.parametrize(
    "second",
    [
        {"name": "fast", "params": FAST},
        {"name": " FAST ", "params": {**FAST, "window": 3}},  # accepted + rejected
    ],
)
def test_a_repeated_identity_leaves_the_row_alone(second: Any) -> None:
    redis = FakeRedis(document(BOTH))
    sync(redis)
    fast = rows()["Fast"]
    redis.publish(
        document([BOTH[0], {"name": "Fast", "params": {**FAST, "window": 12}}, second])
    )
    outcome = sync(redis)
    assert [e["type"] for e in outcome.errors] == ["duplicate_name"] * 2
    assert rows()["Fast"].params == fast.params


def test_an_overlong_name_is_unreadable_and_the_good_ones_are_written() -> None:
    outcome = sync(FakeRedis(document([*BOTH, {"name": "x" * 65, "params": FAST}])))
    assert outcome.errors == [{"index": 2, "name": None, "type": "unreadable"}]
    assert set(rows()) == {"Default", "Fast"}


def test_names_one_for_postgres_but_two_for_python_are_a_duplicate() -> None:
    """R83: the index's ``lower()`` decides, not Python's ``str.lower()``."""
    pairs = [("\u0130", "i"), ("\u03a3\u0391\u03a3", "\u03c3\u03b1\u03c3")]
    with Session(engine) as session:
        pair = next(
            (
                (a, b)
                for a, b in pairs
                if a.lower() != b.lower()
                and session.execute(
                    text("SELECT lower(:a) = lower(:b)"), {"a": a, "b": b}
                ).scalar_one()
            ),
            None,
        )
    if pair is None:
        pytest.skip("this database's lower() agrees with Python on both pairs")
    first, second = pair
    outcome = sync(
        FakeRedis(
            document(
                [
                    {"name": first, "params": DEFAULT},
                    {"name": second, "params": FAST},
                ]
            )
        )
    )
    assert [e["type"] for e in outcome.errors] == ["duplicate_name"] * 2
    assert rows() == {}


# --- name_conflict --------------------------------------------------------------


def test_a_user_row_with_a_builtin_name_is_kept_until_it_goes() -> None:
    mine = {**DEFAULT, "window": 20}
    add_user_preset("fast", mine)
    redis = FakeRedis(document(BOTH))
    outcome = sync(redis)
    assert outcome.errors == [{"index": 1, "name": "Fast", "type": "name_conflict"}]
    assert outcome.status == "partial"
    user_row = rows()["fast"]
    assert (user_row.is_builtin, user_row.params) == (False, mine)
    default_updated = rows()["Default"].updated_at

    # The same catalog is synced again while the conflict stands...
    assert sync(redis).status == "partial"
    # ...and once the user renames their row, the built-in one appears.
    with Session(engine) as session:
        row = session.get(StrategyPreset, user_row.id)
        assert row is not None
        row.name = "My fast"
        session.add(row)
        session.commit()
    outcome = sync(redis)
    assert (outcome.status, outcome.errors) == ("ok", [])
    found = rows()
    assert found["Fast"].is_builtin and found["Fast"].params == FAST
    assert found["Default"].updated_at == default_updated  # good rows untouched


def test_a_user_row_inserted_during_the_sync_is_a_conflict() -> None:
    """A user's POST that commits after the sync began, before its upsert."""
    redis = FakeRedis(document(BOTH))
    redis.on_get = lambda: add_user_preset("Fast", {**DEFAULT, "window": 30})
    outcome = sync(redis)
    assert outcome.errors == [{"index": 1, "name": "Fast", "type": "name_conflict"}]
    assert rows()["Fast"].is_builtin is False


# --- Failures and concurrency ---------------------------------------------------


def test_a_redis_error_changes_nothing_and_the_next_sync_works() -> None:
    redis = FakeRedis(document(BOTH))

    def fail() -> None:
        raise RedisError("down")

    redis.on_get = fail
    assert sync(redis).result == "failed"
    assert state() is None  # the first insert of the lock row rolled back too
    redis.on_get = None
    assert sync(redis).result == "applied"


def test_a_database_error_changes_nothing_and_the_next_sync_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy.exc import OperationalError

    def broken(*_: Any) -> Any:
        raise OperationalError("upsert", {}, Exception("connection lost"))

    monkeypatch.setattr(builtin_presets, "_regroup_by_database", broken)
    redis = FakeRedis(document(BOTH))
    assert sync(redis).result == "failed"
    assert rows() == {} and state() is None
    monkeypatch.undo()
    assert sync(redis).result == "applied"


def test_a_hung_redis_releases_the_lock_after_the_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(builtin_presets, "REDIS_READ_TIMEOUT_S", 0.2)
    redis = FakeRedis(document(BOTH))
    redis.on_get = lambda: asyncio.sleep(30)
    assert sync(redis).result == "failed"
    redis.on_get = None
    assert sync(redis).result == "applied"


def test_an_unusable_catalog_is_a_failure_not_an_exception() -> None:
    redis = FakeRedis(None)
    redis.raw = "{not json"
    assert sync(redis).result == "failed"
    redis.raw = None
    assert sync(redis).result == "no_catalog"
    assert state() is None


def test_four_concurrent_first_syncs_write_one_row_per_name() -> None:
    redis = FakeRedis(document(BOTH))
    start = threading.Barrier(4)
    outcomes: list[SyncOutcome] = []
    failures: list[BaseException] = []

    def worker() -> None:
        try:
            start.wait()
            outcomes.append(sync(redis))
        except BaseException as exc:  # surfaced below
            failures.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert failures == []
    assert sorted(o.result for o in outcomes) == ["applied", "noop", "noop", "noop"]
    assert set(rows()) == {"Default", "Fast"}


def test_the_first_inserter_rolling_back_lets_the_next_one_sync() -> None:
    """Worker 1 inserted the lock row and fails; worker 2 waited on it."""
    redis = FakeRedis(document(BOTH))
    inside, release = threading.Event(), threading.Event()
    calls = 0

    def first_fails() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            inside.set()
            release.wait(timeout=30)
            raise RedisError("worker 1 fails while holding the new row")

    redis.on_get = first_fails
    results: dict[str, SyncOutcome] = {}
    one = threading.Thread(target=lambda: results.update(one=sync(redis)))
    one.start()
    assert inside.wait(timeout=30)
    two = threading.Thread(target=lambda: results.update(two=sync(redis)))
    two.start()
    two.join(timeout=0.5)
    assert two.is_alive()  # blocked on worker 1's uncommitted lock row
    release.set()
    one.join(timeout=30)
    two.join(timeout=30)
    assert (results["one"].result, results["two"].result) == ("failed", "applied")
    assert set(rows()) == {"Default", "Fast"}


def test_the_sync_owns_its_transaction() -> None:
    """``items`` are read after the sync: nothing is left open or pending."""
    with Session(engine) as session:
        asyncio.run(
            sync_builtin_presets(session, FakeRedis(document(BOTH)), STRATEGY)  # type: ignore[arg-type]
        )
        assert not session.in_transaction()
