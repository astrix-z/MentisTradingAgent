"""Shared validation edge cases, agent side (presets plan §6).

``tests/fixtures/validation-edge-cases.json`` is read by the agent, backend and
UI runners alike. This one drives the three agent entry points: the model
itself, a ``start`` command through :func:`commands.dispatch`, and a runtime
snapshot through :func:`commands.resume_from_runtime`. The command and resume
paths use a real :class:`Engine` with only the network part of the run
(``_start_run``) stubbed, and a spy on ``from_params`` proves validation was
actually reached: returning before it would be a false pass.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from test_commands_lifecycle import redis_mock, results

from agent.common import commands
from agent.engine import Engine
from agent.strategies.tripple_screen.settings import TripleScreenSettings

FIXTURE = (
    Path(__file__).resolve().parents[2] / "tests/fixtures/validation-edge-cases.json"
)
DOCUMENT: dict[str, Any] = json.loads(FIXTURE.read_text())
BASELINE: dict[str, Any] = DOCUMENT["baseline"]
ERROR_MAP: dict[str, Any] = DOCUMENT["error_map"]["agent"]
CASES: list[dict[str, Any]] = DOCUMENT["cases"]
ENTRY_POINTS = (
    "ui",
    "backend_start",
    "backend_commands_start",
    "backend_preset_post",
    "agent_model_validate",
    "agent_command",
    "agent_resume",
)
AGENT_ENTRY_POINTS = ("agent_model_validate", "agent_command", "agent_resume")
STRATEGY = "tripple_screen"


def params_text(case: dict[str, Any]) -> str:
    """The exact params JSON a case sends: ``params_raw``, or baseline + patch."""
    if (case["params_patch"] is None) == (case["params_raw"] is None):
        raise AssertionError(f"{case['id']}: set exactly one of params_patch/raw")
    if case["params_raw"] is not None:
        return str(case["params_raw"])
    return json.dumps(apply(BASELINE["params"], case["params_patch"]))


def apply(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in patch.items():
        if value == {"$delete": True}:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


def category(error: Any) -> tuple[str, str | None]:
    """Map one pydantic error to the fixture's ``(category, field)`` pair."""
    loc = error.get("loc") or ()
    field = loc[0] if loc and isinstance(loc[0], str) else None
    kind = error["type"]
    if kind in ERROR_MAP["types"]:
        return ERROR_MAP["types"][kind], field
    if kind == "value_error":
        for rule in ERROR_MAP["messages"]:
            if re.search(rule["pattern"], error["msg"]):
                return rule["category"], field
    raise AssertionError(f"unmapped agent error: {error}")


def expected_errors(outcome: dict[str, Any]) -> set[tuple[str, str | None]]:
    return {(item["category"], item["field"]) for item in outcome.get("errors", [])}


def verdict(outcome: Any) -> Any:
    """What must agree across layers: ok/error and which errors, not values."""
    if outcome == "n/a":
        return outcome
    return [outcome["result"], sorted(expected_errors(outcome), key=str)]


def expected_normalized(case: dict[str, Any], outcome: dict[str, Any]) -> Any:
    projection = outcome.get("normalized", case["expected_normalized"])
    full = {**BASELINE["params"], "volume_window": "24h"}
    return apply(full, projection or {})


class Spy:
    """Wraps ``from_params``: records what validation produced or raised."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls = 0
        self.settings: TripleScreenSettings | None = None
        self.error: ValidationError | None = None
        original = TripleScreenSettings.from_params

        def spy(params: dict[str, Any]) -> TripleScreenSettings:
            self.calls += 1
            try:
                self.settings = original(params)
            except ValidationError as exc:
                self.error = exc
                raise
            return self.settings

        monkeypatch.setattr(TripleScreenSettings, "from_params", spy)


def engine_without_network(redis: Any) -> Engine:
    engine = Engine(redis)
    engine._start_run = AsyncMock()  # type: ignore[method-assign]
    return engine


def run_model_validate(text: str, _: pytest.MonkeyPatch) -> Any:
    try:
        return TripleScreenSettings.model_validate(json.loads(text))
    except ValidationError as exc:
        return exc


def run_command(text: str, monkeypatch: pytest.MonkeyPatch) -> Any:
    spy = Spy(monkeypatch)
    redis = redis_mock()
    envelope = (
        '{"id": "edge", "type": "start", "issued_at": "2026-09-26T00:00:00+00:00", '
        f'"payload": {{"network": {json.dumps(BASELINE["network"])}, '
        f'"strategy": "{STRATEGY}", "symbols": {json.dumps(BASELINE["symbols"])}, '
        f'"params": {text}}}}}'
    )
    asyncio.run(commands.dispatch(engine_without_network(redis), redis, envelope))
    assert spy.calls == 1, "the command never reached settings validation"
    result = results(redis)["agent:result:edge"]
    assert result["ok"] is (spy.error is None), result
    return spy.error or spy.settings


def run_resume(text: str, monkeypatch: pytest.MonkeyPatch) -> Any:
    spy = Spy(monkeypatch)
    redis = redis_mock()
    redis.get.return_value = (
        f'{{"running": true, "network": {json.dumps(BASELINE["network"])}, '
        f'"strategy": "{STRATEGY}", "symbols": {json.dumps(BASELINE["symbols"])}, '
        f'"params": {text}}}'
    )
    engine = engine_without_network(redis)
    asyncio.run(commands.resume_from_runtime(engine, redis))
    assert spy.calls == 1, "resume never reached settings validation"
    started = engine._start_run.await_count  # type: ignore[attr-defined]
    assert started == (0 if spy.error else 1)
    return spy.error or spy.settings


RUNNERS = {
    "agent_model_validate": run_model_validate,
    "agent_command": run_command,
    "agent_resume": run_resume,
}


def test_every_case_declares_every_entry_point() -> None:
    """A missing outcome is a runner error, never a silent skip."""
    ids = [case["id"] for case in CASES]
    assert len(ids) == len(set(ids))
    for case in CASES:
        assert set(case["outcomes"]) == set(ENTRY_POINTS), case["id"]
        verdicts = {
            json.dumps(verdict(case["outcomes"][ep]))
            for ep in (*AGENT_ENTRY_POINTS, "backend_start")
        }
        if len(verdicts) > 1:
            assert case["intentional_difference"], case["id"]


@pytest.mark.parametrize("entry_point", AGENT_ENTRY_POINTS)
@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_agent_entry_point(
    case: dict[str, Any], entry_point: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome = case["outcomes"][entry_point]
    if outcome == "n/a":
        return
    got = RUNNERS[entry_point](params_text(case), monkeypatch)
    if outcome["result"] == "ok":
        assert isinstance(got, TripleScreenSettings), got
        assert got.model_dump(mode="json") == expected_normalized(case, outcome)
    else:
        assert isinstance(got, ValidationError), got
        assert {category(error) for error in got.errors()} == expected_errors(outcome)
