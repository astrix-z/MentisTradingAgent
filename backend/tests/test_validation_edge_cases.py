"""Shared validation edge cases, backend side (presets plan §6).

``tests/fixtures/validation-edge-cases.json`` is read by the agent, backend and
UI runners alike. This one posts each case to both Start routes with the
agent's real published catalog (``frontend/tests/fixtures/strategy-catalog.json``,
regenerated from ``agent.protocol``) in the in-memory Redis of
``test_trading_contract``, and saves each one as a preset (``POST`` and a
``PUT`` over a preset made from the baseline) against the fixture's option
lists in a mocked session, so it needs neither a database nor the agent
package and is never skipped:

    pytest --noconftest tests/test_validation_edge_cases.py

An accepted Start must enqueue the params exactly as sent (the backend
forwards them verbatim); a rejected one must enqueue nothing. An accepted
preset stores a full snapshot — every non-const parameter, the sent ones as
sent (plan §4.3.3); a rejected one stores nothing.

Every backend entry point the fixture declares is collected. One whose
endpoint does not exist yet is listed under ``stages`` with its owning task
and is reported as a ``deferred`` skip (``pytest -rs``); an ``n/a`` outcome is
reported as an ``n/a`` skip. Neither counts as a pass.
"""

import json
import re
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.routes import presets, trading
from app.core.strategy_catalog import REDIS_STRATEGIES_KEY, compile_published
from app.models import StrategyFieldOptions, StrategyPreset
from tests.test_trading_contract import NOW, MemoryRedis, client, headers

ROOT = Path(__file__).resolve().parents[2]
DOCUMENT: dict[str, Any] = json.loads(
    (ROOT / "tests/fixtures/validation-edge-cases.json").read_text()
)
BASELINE: dict[str, Any] = DOCUMENT["baseline"]
ERROR_MAP: dict[str, Any] = DOCUMENT["error_map"]["backend"]
CASES: list[dict[str, Any]] = DOCUMENT["cases"]
STRATEGY = "tripple_screen"
ENTRY_POINTS = (
    "backend_start",
    "backend_commands_start",
    "backend_preset_post",
    "backend_preset_put",
)
STAGES: dict[str, str] = {
    key: value for key, value in DOCUMENT["stages"].items() if key != "$comment"
}

__all__ = ["client"]  # the imported fixture, used by name below


def start_body(params: str) -> str:
    return (
        f'{{"network": {json.dumps(BASELINE["network"])}, '
        f'"strategy": "{STRATEGY}", "symbols": {json.dumps(BASELINE["symbols"])}, '
        f'"params": {params}}}'
    )


ROUTES = {
    "backend_start": ("/api/v1/trading/start/", start_body),
    "backend_commands_start": (
        "/api/v1/trading/commands/",
        lambda params: f'{{"type": "start", "payload": {start_body(params)}}}',
    ),
    "backend_preset_post": (f"/api/v1/trading/strategies/{STRATEGY}/presets/", None),
    "backend_preset_put": (f"/api/v1/trading/strategies/{STRATEGY}/presets/", None),
}
PRESET_ENTRY_POINTS = ("backend_preset_post", "backend_preset_put")
CATALOG = compile_published((ROOT / BASELINE["catalog"]).read_text())


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> MemoryRedis:
    store = MemoryRedis()
    store.values[REDIS_STRATEGIES_KEY] = (ROOT / BASELINE["catalog"]).read_text()
    monkeypatch.setattr(trading, "get_redis", lambda: store)
    monkeypatch.setattr(presets, "get_redis", lambda: store)
    monkeypatch.setattr(trading, "_now", lambda: NOW)
    return store


def stored(client: TestClient) -> dict[uuid.UUID, StrategyPreset]:
    """The mocked session keeps saved presets and serves the fixture's lists."""
    session = client.app_state["session"]
    rows: dict[uuid.UUID, StrategyPreset] = {}
    users = session.get.side_effect
    session.get.side_effect = lambda model, key: (
        rows.get(key) if model is StrategyPreset else users(model, key)
    )
    session.add.side_effect = lambda row: (
        rows.__setitem__(row.id, row) if isinstance(row, StrategyPreset) else None
    )
    session.exec.return_value.all.return_value = [
        StrategyFieldOptions(strategy=STRATEGY, field=field, options=options)
        for field, options in BASELINE["options"].items()
    ]
    return rows


def preset_body(name: str, params: str) -> str:
    return f'{{"name": {json.dumps(name)}, "params": {params}}}'


def apply(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in patch.items():
        if value == {"$delete": True}:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


def params_text(case: dict[str, Any]) -> str:
    """The exact params JSON a case sends: ``params_raw``, or baseline + patch."""
    if (case["params_patch"] is None) == (case["params_raw"] is None):
        raise AssertionError(f"{case['id']}: set exactly one of params_patch/raw")
    if case["params_raw"] is not None:
        return str(case["params_raw"])
    return json.dumps(apply(BASELINE["params"], case["params_patch"]))


def category(error: dict[str, Any]) -> tuple[str, str | None]:
    """Map one 422 ``detail`` item to the fixture's ``(category, field)`` pair."""
    loc = list(error.get("loc") or [])
    # A Start reports params errors under ["body", "params", ...] (plan
    # §4.3.4); ["body", <position>] (broken JSON) has no field. Anything else —
    # a bare [<field>] or an error in the wrapper (symbols, network, ...) —
    # fails the runner instead of reading as a matching error.
    if loc[:2] == ["body", "params"]:
        loc = loc[2:]
    elif len(loc) == 2 and loc[0] == "body" and isinstance(loc[1], int):
        loc = []
    else:
        raise AssertionError(f"error outside body.params: {error}")
    field = loc[0] if loc and isinstance(loc[0], str) else None
    kind = error["type"]
    if kind in ERROR_MAP["types"]:
        return ERROR_MAP["types"][kind], field
    if kind == "value_error":
        for rule in ERROR_MAP["messages"]:
            if re.search(rule["pattern"], error["msg"]):
                return rule["category"], field
    raise AssertionError(f"unmapped backend error: {error}")


def test_every_backend_entry_point_is_run_or_deferred() -> None:
    """A missing outcome, or an entry point with neither route nor owner, fails."""
    for entry_point in ENTRY_POINTS:
        assert (entry_point in ROUTES) != (entry_point in STAGES), entry_point
    for case in CASES:
        assert set(ENTRY_POINTS) <= set(case["outcomes"]), case["id"]


@pytest.mark.parametrize("entry_point", ENTRY_POINTS)
@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_backend_entry_point(
    client: TestClient,
    redis: MemoryRedis,
    case: dict[str, Any],
    entry_point: str,
) -> None:
    outcome = case["outcomes"][entry_point]
    if entry_point in STAGES:
        pytest.skip(f"deferred: {entry_point} runs from {STAGES[entry_point]}")
    if outcome == "n/a":
        pytest.skip(f"n/a: {entry_point} does not apply to {case['id']}")
    if entry_point in PRESET_ENTRY_POINTS:
        run_preset(client, redis, case, entry_point, outcome)
        return
    path, wrap = ROUTES[entry_point]
    assert wrap is not None
    params = params_text(case)
    response = client.post(
        path,
        content=wrap(params),
        headers={**headers(client), "Content-Type": "application/json"},
    )
    if outcome["result"] == "ok":
        assert response.status_code == 202, response.json()
        assert [
            publication[1]["payload"]["params"] for publication in redis.publications
        ] == [json.loads(params)]
        return
    assert response.status_code == 422, response.json()
    assert not redis.publications
    assert trading.REDIS_RUNTIME_KEY not in redis.values
    got = {category(error) for error in response.json()["detail"]}
    assert got == {(item["category"], item["field"]) for item in outcome["errors"]}


def run_preset(
    client: TestClient,
    redis: MemoryRedis,
    case: dict[str, Any],
    entry_point: str,
    outcome: dict[str, Any],
) -> None:
    """POST the case as a new preset, or PUT it over one made from the baseline."""
    rows = stored(client)
    path, _ = ROUTES[entry_point]
    params = params_text(case)
    send = {**headers(client), "Content-Type": "application/json"}
    before: dict[str, Any] | None = None
    if entry_point == "backend_preset_put":
        created = client.post(
            path,
            content=preset_body("baseline", json.dumps(BASELINE["params"])),
            headers=send,
        )
        assert created.status_code == 201, created.json()
        preset_id = uuid.UUID(created.json()["id"])
        before = dict(rows[preset_id].params)
        response = client.put(
            f"{path}{preset_id}", content=preset_body(case["id"], params), headers=send
        )
        expected_status = 200
    else:
        response = client.post(
            path, content=preset_body(case["id"], params), headers=send
        )
        expected_status = 201
    assert not redis.publications  # saving a preset never reaches the agent
    if outcome["result"] == "ok":
        assert response.status_code == expected_status, response.json()
        row = rows[uuid.UUID(response.json()["id"])]
        assert row.name == case["id"]
        sent = json.loads(params)
        editable = CATALOG.strategy(STRATEGY)
        assert editable is not None
        assert set(row.params) == editable.editable_fields()
        assert {key: row.params[key] for key in sent} == sent
        return
    assert response.status_code == 422, response.json()
    if before is None:
        assert rows == {}
    else:
        assert [(row.name, row.params) for row in rows.values()] == [
            ("baseline", before)
        ]
    got = {category(error) for error in response.json()["detail"]}
    assert got == {(item["category"], item["field"]) for item in outcome["errors"]}
