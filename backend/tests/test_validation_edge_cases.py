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
``backend_preset_post`` is declared by every case but its endpoint arrives with
PRESET-06, which adds it to ``ROUTES``.
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
    if loc[:2] == ["body", "params"]:
        loc = loc[2:]
    field = loc[0] if loc and isinstance(loc[0], str) else None
    kind = error["type"]
    if kind in ERROR_MAP["types"]:
        return ERROR_MAP["types"][kind], field
    if kind == "value_error":
        for rule in ERROR_MAP["messages"]:
            if re.search(rule["pattern"], error["msg"]):
                return rule["category"], field
    raise AssertionError(f"unmapped backend error: {error}")


def test_every_case_declares_the_preset_endpoint() -> None:
    """Declared now, run from PRESET-06: a missing outcome is a runner error."""
    for case in CASES:
        assert "backend_preset_post" in case["outcomes"], case["id"]
        assert set(ROUTES) <= set(case["outcomes"]), case["id"]


@pytest.mark.parametrize("entry_point", sorted(ROUTES))
@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_backend_entry_point(
    client: TestClient,
    redis: MemoryRedis,
    case: dict[str, Any],
    entry_point: str,
) -> None:
    outcome = case["outcomes"][entry_point]
    if outcome == "n/a":
        return
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
