"""Cross-service fidelity: the backend must compile what the agent publishes.

``test_trading_contract.py`` uses a hand-written fixture so it can stay
standalone. This closes the loop instead: it takes the *real* document
``agent.protocol`` generates and asserts the backend accepts it and reaches the
same verdicts the agent's own model does. That is the proof that the backend is
still authoritative while owning no domain rule of its own — and it replaces
the "keep KNOWN_STRATEGIES in sync with the agent registry by hand" comments
that nothing used to enforce.

Skipped where the agent package is not importable (a backend-only image).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

agent_protocol = pytest.importorskip("agent.protocol")
agent_strategies = pytest.importorskip("agent.strategies")
agent_settings = pytest.importorskip("agent.strategies.tripple_screen.settings")
catalog_module = pytest.importorskip("app.core.strategy_catalog")

TripleScreenSettings = agent_settings.TripleScreenSettings

COMPILED = catalog_module.compile_catalog(agent_protocol.describe_catalog())
STRATEGY = COMPILED.strategy("tripple_screen")


def agent_rejects(params: dict[str, Any]) -> bool:
    """Exactly what the agent's own validation does with these params."""
    try:
        TripleScreenSettings.from_params(params)
    except ValueError:
        return True
    return False


def test_backend_sees_exactly_the_strategies_the_agent_registers() -> None:
    """A new strategy needs no backend change — and cannot be forgotten."""
    assert set(COMPILED.strategies) == set(agent_strategies.STRATEGIES)


def test_backend_compiles_the_published_catalog() -> None:
    assert COMPILED.schema_hash == agent_protocol.describe_catalog()["schema_hash"]
    assert COMPILED.published_at
    assert STRATEGY is not None
    assert STRATEGY.label == "Triple Screen"
    assert STRATEGY.constraints


def test_backend_accepts_every_declared_default() -> None:
    """The defaults the UI prefills must be a starting point the agent accepts."""
    assert agent_rejects({}) is False
    assert STRATEGY.validate({}) == []


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"tp_pct": 12},
        {"tp_pct": "12.5"},
        {"kline_window": 35},
        {"ema_tide_periods": [5, 13, 34]},
        {"tide_interval": "8h", "wave_interval": "2h", "ripple_interval": "30m"},
        # Only the order is required (D1): non-multiple and day/week scales.
        {"tide_interval": "1h", "wave_interval": "15m", "ripple_interval": "5m"},
        {"tide_interval": "12h", "wave_interval": "6h", "ripple_interval": "4h"},
        {"tide_interval": "1w", "wave_interval": "1d", "ripple_interval": "4h"},
        {"wave_interval": "2h"},
        {"rest_concurrency": 5, "cooldown_minutes": 0},
        {"trail_cap_pct": 0},
        {"trail_cap_pct": "3", "tp_pct": "3.5"},
        {"max_hold_hours": "0.25"},
    ],
)
def test_backend_and_agent_accept_the_same_parameters(params: dict[str, Any]) -> None:
    assert agent_rejects(params) is False, "fixture must be valid"
    assert STRATEGY.validate(params) == []


@pytest.mark.parametrize(
    "params",
    [
        # Field-level rules, from the published JSON Schema.
        {"tp_pct": -1},
        {"tp_pct": "not-a-number"},
        {"rest_concurrency": 2},
        {"rest_concurrency": 6},
        {"quote_asset": "BTC"},
        {"cooldown_minutes": -1},
        {"ema_tide_periods": [5, 13]},
        {"ema_tide_periods": [0, 13, 34]},
        # Cross-field rules, from the published constraints.
        {"ema_tide_periods": [34, 13, 5]},
        {"wave_interval": "4h"},  # tide == wave
        {"tide_interval": "30m"},  # tide < wave
        {"ripple_interval": "1h"},  # wave == ripple
        {"kline_window": 34},
        {"stochastic_k_period": 1000},
        # The trailing cap must stay below the take-profit (spec §7.4).
        {"trail_cap_pct": 10},
        {"trail_cap_pct": 11},
        {"trail_cap_pct": -1},
        {"tp_pct": 2},
        {"max_hold_hours": 0},
        {"max_hold_hours": -1},
    ],
)
def test_backend_and_agent_reject_the_same_parameters(params: dict[str, Any]) -> None:
    """Same verdict, from one declaration: no rule is mirrored in the backend."""
    assert agent_rejects(params) is True, "fixture must be invalid"
    assert STRATEGY.validate(params), "backend must reject this too"


def test_backend_is_stricter_about_undeclared_parameters() -> None:
    """The published contract forbids extras, even though the agent ignores them.

    ``TripleScreenSettings.from_params`` deliberately filters to known keys so a
    command payload may carry shared keys; the backend rejects a *Start* that
    names a parameter that does not exist, which is the behaviour the API has
    always had.
    """
    assert agent_rejects({"unknown_param": 1}) is False
    errors = STRATEGY.validate({"unknown_param": 1})
    assert len(errors) == 1
    assert "unknown parameter" in errors[0]["msg"]


def test_backend_reports_the_agents_own_message() -> None:
    """The UI shows rule text, so the two services must agree on it verbatim."""
    messages = {
        rule["message"]
        for rule in agent_protocol.describe_catalog()["strategies"][0]["constraints"]
    }
    errors = STRATEGY.validate({"kline_window": 10})
    assert [error["msg"] for error in errors] == ["kline_window must be at least 35"]
    assert "kline_window must be at least {rhs}" in messages


def test_a_repeated_rule_message_is_reported_once() -> None:
    """The order chain is two comparisons sharing one message; say it once."""
    errors = STRATEGY.validate({"tide_interval": "15m", "ripple_interval": "4h"})
    assert [error["msg"] for error in errors] == [
        "timeframes must satisfy tide > wave > ripple"
    ]


def test_backend_serves_the_ui_metadata_the_form_needs() -> None:
    props = STRATEGY.settings_schema["properties"]
    labels = {name: prop["title"] for name, prop in props.items()}
    assert labels["tp_pct"] == "Take profit %"
    assert labels["ema_tide_periods"] == "EMA tide periods"
    intervals = COMPILED.lookups["intervals_ms"]
    assert props["wave_interval"]["enum"] == list(intervals)
    assert props["ema_tide_periods"]["minItems"] == 3


def test_backend_refuses_a_catalog_it_cannot_understand() -> None:
    """An unknown format is not interpreted optimistically."""
    with pytest.raises(catalog_module.CatalogError, match="unsupported catalog"):
        catalog_module.compile_catalog(
            {"version": 999, "schema_hash": "h", "strategies": [{"name": "x"}]}
        )


class _FakeRedis:
    """Just enough Redis for publish -> read, with the bytes left intact."""

    def __init__(self, store: dict[str, str] | None = None) -> None:
        self.store: dict[str, str] = dict(store or {})

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str) -> None:
        self.store[key] = value


def test_the_published_bytes_are_what_the_backend_compiles() -> None:
    """The end-to-end seam: agent publishes, backend reads the same bytes.

    Also pins the shared key name, which used to be a comment on both sides.
    """

    async def scenario() -> None:
        publisher = _FakeRedis()
        published = await agent_protocol.publish_catalog(publisher)
        assert published is not None
        # The agent wrote exactly one document, under the key the backend reads.
        assert list(publisher.store) == [catalog_module.REDIS_STRATEGIES_KEY]
        raw = publisher.store[catalog_module.REDIS_STRATEGIES_KEY]
        reader = _FakeRedis({catalog_module.REDIS_STRATEGIES_KEY: raw})
        compiled = await catalog_module.read_catalog(reader)
        assert compiled is not None
        assert compiled.schema_hash == published["schema_hash"]
        assert compiled.strategy("tripple_screen") is not None
        # And what came back through JSON behaves like the in-memory document.
        assert compiled.strategy("tripple_screen").validate({}) == []
        assert json.loads(raw)["published_at"] == compiled.published_at

    asyncio.run(scenario())


agent_execution = pytest.importorskip("agent.execution.settings")
EXECUTION = COMPILED.execution


def agent_rejects_execution(values: dict[str, Any]) -> bool:
    try:
        agent_execution.ExecutionSettings.from_payload(values)
    except ValueError:
        return True
    return False


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"enabled": True},
        {"enabled": True, "max_slippage_pct": "0.5", "max_symbols": 2},
        {"slot_quote": "10", "position_share": "0.8"},
    ],
)
def test_execution_settings_accepted_by_both(values: dict[str, Any]) -> None:
    assert EXECUTION is not None
    assert agent_rejects_execution(values) is False
    assert EXECUTION.validate(values) == []


@pytest.mark.parametrize(
    "values",
    [
        {"max_slippage_pct": 0},
        {"max_slippage_pct": 6},
        {"position_share": "1.5"},
        {"unprotected_deadline_s": 1},
        {"max_symbols": 0},
        {"enabled": "maybe"},
        {"unknown_setting": 1},
    ],
)
def test_execution_settings_rejected_by_both(values: dict[str, Any]) -> None:
    assert EXECUTION is not None
    assert agent_rejects_execution(values) is True
    assert EXECUTION.validate(values)


def test_execution_defaults_keep_real_execution_off() -> None:
    assert EXECUTION is not None
    props = EXECUTION.settings_schema["properties"]  # type: ignore[index]
    assert props["enabled"]["default"] is False
