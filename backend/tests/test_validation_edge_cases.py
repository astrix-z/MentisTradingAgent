"""Shared validation edge cases, backend side (presets plan §6).

``tests/fixtures/validation-edge-cases.json`` is read by the agent, backend and
UI runners alike. This one posts each case to both Start routes with the
agent's real published catalog (``frontend/tests/fixtures/strategy-catalog.json``,
regenerated from ``agent.protocol``) in the in-memory Redis of
``test_trading_contract``, so it needs neither a database nor the agent
package and is never skipped:

    pytest --noconftest tests/test_validation_edge_cases.py

An accepted Start must enqueue the params exactly as sent (the backend
forwards them verbatim); a rejected one must enqueue nothing.

Every backend entry point the fixture declares is collected. One whose
endpoint does not exist yet is listed under ``stages`` with its owning task
and is reported as a ``deferred`` skip (``pytest -rs``); an ``n/a`` outcome is
reported as an ``n/a`` skip. Neither counts as a pass.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.routes import trading
from app.core.strategy_catalog import REDIS_STRATEGIES_KEY
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
}


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> MemoryRedis:
    store = MemoryRedis()
    store.values[REDIS_STRATEGIES_KEY] = (ROOT / BASELINE["catalog"]).read_text()
    monkeypatch.setattr(trading, "get_redis", lambda: store)
    monkeypatch.setattr(trading, "_now", lambda: NOW)
    return store


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
    # ["body", "params", <field>] or the compiled model's bare [<field>];
    # ["body", <position>] (broken JSON) has no field. Any other body location
    # is an error in the wrapper (symbols, network, ...), not in params: the
    # cases never expect one, so it fails the runner instead of reading as a
    # field-less rule error.
    if loc[:1] == ["body"]:
        if loc[1:2] == ["params"]:
            loc = loc[2:]
        elif len(loc) == 2 and isinstance(loc[1], int):
            loc = []
        else:
            raise AssertionError(f"error outside params: {error}")
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
    path, wrap = ROUTES[entry_point]
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
