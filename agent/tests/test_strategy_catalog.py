"""The published strategy catalog is the backend/UI contract — keep it honest.

``test_published_rules_reject_what_the_model_rejects`` is the load-bearing
test: it evaluates the published cross-field constraints with an independent
reference implementation of the documented grammar and asserts they accept and
reject exactly the payloads ``TripleScreenSettings`` does. Editing ``coherent``
without updating ``tripple_screen/schema.py`` (or the reverse) fails here.
"""

from __future__ import annotations

import asyncio
import json
import math
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import BaseModel, Field
from redis.exceptions import RedisError

from agent.common.intervals import interval_ms
from agent.engine import REDIS_STRATEGIES_KEY
from agent.protocol import (
    CATALOG_VERSION,
    CatalogError,
    describe_catalog,
    describe_strategy,
    publish_catalog,
)
from agent.strategies import STRATEGIES
from agent.strategies.base import Strategy, StrategyServices
from agent.strategies.tripple_screen.settings import TripleScreenSettings

PARAMS_ENTRY = describe_strategy("tripple_screen", STRATEGIES["tripple_screen"])


# --- A reference implementation of the published constraint grammar ---------


class RuleFailed(Exception):
    """One published constraint could not be evaluated for this payload."""


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise RuleFailed(f"not a number: {value!r}")
    return float(value)


def _eval_expr(expr: dict[str, Any], values: dict[str, Any], lookups: Any) -> float:
    if "const" in expr:
        return _number(expr["const"])
    if "lookup" in expr:
        table = lookups[expr["lookup"]]
        key = str(values[expr["field"]])
        if key not in table:
            raise RuleFailed(f"{key!r} is not a value of {expr['lookup']}")
        return _number(table[key])
    if "field" in expr:
        ref = expr["field"]
        if isinstance(ref, list):
            name, index = ref
            return _number(values[name][index])
        return _number(values[ref])
    args = [_eval_expr(arg, values, lookups) for arg in expr["args"]]
    op = expr["op"]
    if op == "add":
        return math.fsum(args)
    if op == "mul":
        return math.prod(args)
    if op == "max":
        return max(args)
    if op == "sub":
        result = args[0]
        for arg in args[1:]:
            result -= arg
        return result
    raise AssertionError(f"reference evaluator does not know operator {op!r}")


_COMPARE = {
    "lt": lambda a, b: a < b,
    "lte": lambda a, b: a <= b,
    "gt": lambda a, b: a > b,
    "gte": lambda a, b: a >= b,
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
}


def reference_check(params: dict[str, Any]) -> list[str]:
    """Messages of every published constraint this payload violates.

    Uses the schema's own defaults, so a payload may set only what it tests —
    the same way the backend and the UI fill a form.
    """
    values: dict[str, Any] = {
        name: prop.get("default")
        for name, prop in PARAMS_ENTRY["settings_schema"]["properties"].items()
    }
    values.update(params)
    lookups = describe_catalog()["lookups"]
    failures: list[str] = []
    for rule in PARAMS_ENTRY["constraints"]:
        try:
            lhs = _eval_expr(rule["lhs"], values, lookups)
            rhs = _eval_expr(rule["rhs"], values, lookups)
        except RuleFailed:
            failures.append(rule["message"])
            continue
        if not _COMPARE[rule["cmp"]](lhs, rhs):
            failures.append(rule["message"].format(lhs=int(lhs), rhs=int(rhs)))
    return failures


def model_rejects(params: dict[str, Any]) -> bool:
    try:
        TripleScreenSettings.from_params(params)
    except ValueError:
        return True
    return False


# --- Catalog shape ----------------------------------------------------------


def test_catalog_is_versioned_and_hashed() -> None:
    catalog = describe_catalog()
    assert catalog["version"] == CATALOG_VERSION
    assert len(catalog["schema_hash"]) == 64
    # A hash of the strategies alone, so republishing does not look like a change.
    assert describe_catalog()["schema_hash"] == catalog["schema_hash"]
    assert catalog["strategies"]


def test_catalog_describes_every_registered_strategy() -> None:
    catalog = describe_catalog()
    assert [entry["name"] for entry in catalog["strategies"]] == sorted(STRATEGIES)


def test_entry_carries_label_description_and_strict_schema() -> None:
    assert PARAMS_ENTRY["name"] == "tripple_screen"
    assert PARAMS_ENTRY["label"] == "Triple Screen"
    assert PARAMS_ENTRY["description"]
    schema = PARAMS_ENTRY["settings_schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == set(TripleScreenSettings.model_fields)


def test_interval_fields_are_limited_to_servable_intervals() -> None:
    """The UI gets a dropdown, not a free-text box, and it cannot drift."""
    lookups = describe_catalog()["lookups"]["intervals_ms"]
    assert lookups
    for name in ("tide_interval", "wave_interval", "ripple_interval"):
        prop = PARAMS_ENTRY["settings_schema"]["properties"][name]
        assert prop["enum"] == list(lookups)
    # Every advertised interval is one the agent can actually serve.
    for value, milliseconds in lookups.items():
        assert interval_ms(value) == milliseconds


def test_field_level_rules_survive_the_round_trip() -> None:
    """Spot-check the metadata a schema-driven form depends on."""
    props = PARAMS_ENTRY["settings_schema"]["properties"]
    assert props["quote_asset"]["enum"] == ["USDT", "USDC"]
    assert props["tp_pct"]["anyOf"][0]["exclusiveMinimum"] == 0
    assert props["rest_concurrency"]["minimum"] == 3
    assert props["rest_concurrency"]["maximum"] == 5
    assert props["kline_window"]["default"] == 1000
    ema = props["ema_tide_periods"]
    assert ema["minItems"] == ema["maxItems"] == len(ema["prefixItems"]) == 3
    # Labels are UI copy, so they are part of the contract too.
    assert props["tp_pct"]["title"] == "Take profit %"
    assert props["ema_tide_periods"]["title"] == "EMA tide periods"


def test_no_constraints_when_a_strategy_has_no_settings() -> None:
    class _Bare(Strategy):
        async def on_start(self, services: StrategyServices) -> None: ...

        async def on_stop(self) -> None: ...

    entry = describe_strategy("bare", _Bare)
    assert entry["settings_schema"] is None
    assert entry["constraints"] == []
    assert entry["label"] == "bare"


# --- The published rules must match the model ------------------------------


@pytest.mark.parametrize(
    "params",
    [
        {},  # every default
        {"ema_tide_periods": [5, 13, 34]},
        {"kline_window": 35},  # exactly the required minimum
        # The ratio rule admits other scales than the default 4h/1h/15m.
        {"tide_interval": "8h", "wave_interval": "2h", "ripple_interval": "30m"},
        {"tp_pct": "12"},  # Decimal as a string
    ],
)
def test_published_rules_accept_what_the_model_accepts(params: dict[str, Any]) -> None:
    assert not model_rejects(params), "fixture must be valid"
    assert reference_check(params) == []


@pytest.mark.parametrize(
    "params",
    [
        {"ema_tide_periods": [34, 13, 5]},  # decreasing
        {"ema_tide_periods": [5, 5, 34]},  # not strictly increasing
        {"wave_interval": "2h"},  # breaks the 16:4:1 ratio
        {"ripple_interval": "5m"},  # breaks the 16:4:1 ratio
        {"kline_window": 34},  # one below the required window
        {"stochastic_k_period": 1000},  # pushes the required window above 1000
    ],
)
def test_published_rules_reject_what_the_model_rejects(params: dict[str, Any]) -> None:
    assert model_rejects(params), "fixture must be invalid"
    assert reference_check(params), "published constraints must reject this payload"


def test_kline_window_message_reports_the_real_minimum() -> None:
    """The message is UI copy, so it must carry the number the model computed."""
    assert reference_check({"kline_window": 10}) == ["kline_window must be at least 35"]
    assert reference_check({"kline_window": 10, "swing_low_h1_n": 80}) == [
        "kline_window must be at least 80"
    ]


def test_ratio_failure_message_is_the_published_one() -> None:
    # Both ratio rules trip, and both carry the same published message.
    assert set(reference_check({"wave_interval": "2h"})) == {
        "timeframes must have tide:wave:ripple ratio 16:4:1"
    }


# --- Publishing -------------------------------------------------------------


def test_publish_writes_the_catalog_to_its_key() -> None:
    async def scenario() -> None:
        redis = Mock(set=AsyncMock())
        catalog = await publish_catalog(redis)
        assert catalog is not None
        redis.set.assert_awaited_once()
        key, payload = redis.set.await_args.args
        assert key == REDIS_STRATEGIES_KEY
        stored = json.loads(payload)
        assert stored["schema_hash"] == catalog["schema_hash"]
        assert stored["strategies"][0]["constraints"]

    asyncio.run(scenario())


def test_publish_is_best_effort_when_redis_is_down() -> None:
    """A Redis outage must not take down a running agent."""

    async def scenario() -> None:
        redis = Mock(set=AsyncMock(side_effect=RedisError("down")))
        assert await publish_catalog(redis) is None

    asyncio.run(scenario())


# --- Fail-fast on an inconsistent declaration -------------------------------


class _DummySettings(BaseModel):
    alpha: int = 1


def _dummy(**overrides: Any) -> type[Strategy]:
    attrs: dict[str, Any] = {
        "__doc__": "Dummy strategy.",
        "settings_model": _DummySettings,
        "settings_constraints": [],
        "settings_lookups": {},
        "on_start": AsyncMock(),
        "on_stop": AsyncMock(),
    }
    attrs.update(overrides)
    return type("_Dummy", (Strategy,), attrs)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        (
            {
                "settings_constraints": [
                    {
                        "cmp": "lt",
                        "lhs": {"field": "missing"},
                        "rhs": {"const": 1},
                        "message": "m",
                    }
                ]
            },
            "not a scalar parameter",
        ),
        (
            {
                "settings_constraints": [
                    {
                        "cmp": "lt",
                        "lhs": {"field": ["alpha", 0]},
                        "rhs": {"const": 1},
                        "message": "m",
                    }
                ]
            },
            "not an array parameter",
        ),
        (
            {
                "settings_constraints": [
                    {
                        "cmp": "lt",
                        "lhs": {"field": "alpha"},
                        "rhs": {
                            "op": "pow",
                            "args": [{"field": "alpha"}, {"const": 2}],
                        },
                        "message": "m",
                    }
                ]
            },
            "unsupported operator",
        ),
        (
            {
                "settings_constraints": [
                    {
                        "cmp": "lt",
                        "lhs": {"lookup": "nope", "field": "alpha"},
                        "rhs": {"const": 1},
                        "message": "m",
                    }
                ]
            },
            "unknown lookup table",
        ),
        (
            {
                "settings_constraints": [
                    {
                        "cmp": "lt",
                        "lhs": {"field": "alpha"},
                        "rhs": {"const": 1},
                        "message": "",
                    }
                ]
            },
            "needs a non-empty message",
        ),
        (
            {
                "settings_model": None,
                "settings_constraints": [
                    {
                        "cmp": "lt",
                        "lhs": {"const": 1},
                        "rhs": {"const": 2},
                        "message": "m",
                    }
                ],
            },
            "constraints without a settings model",
        ),
    ],
)
def test_an_unevaluable_constraint_fails_at_publish_time(
    overrides: dict[str, Any], expected: str
) -> None:
    """A consumer that cannot evaluate a rule cannot claim a payload is valid."""
    with pytest.raises(CatalogError, match=expected):
        describe_strategy("dummy", _dummy(**overrides))


def test_unknown_widget_fails_at_publish_time() -> None:
    class _WidgetSettings(BaseModel):
        thing: str = Field(default="x", json_schema_extra={"ui": {"widget": "slider"}})

    with pytest.raises(CatalogError, match="unsupported ui widget"):
        describe_strategy("dummy", _dummy(settings_model=_WidgetSettings))


def test_interval_widget_requires_the_intervals_lookup() -> None:
    class _WidgetSettings(BaseModel):
        interval: str = Field(
            default="1m", json_schema_extra={"ui": {"widget": "interval"}}
        )

    with pytest.raises(CatalogError, match="needs a 'intervals_ms' lookup"):
        describe_strategy("dummy", _dummy(settings_model=_WidgetSettings))


def test_conflicting_lookup_tables_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two strategies may share a table, but not disagree about its contents."""
    monkeypatch.setitem(
        STRATEGIES, "dummy", _dummy(settings_lookups={"intervals_ms": {"1m": 1}})
    )
    with pytest.raises(CatalogError, match="declared differently"):
        describe_catalog()
