"""Unit coverage for the catalog compiler's fail-closed boundaries.

``test_strategy_catalog_fidelity.py`` proves the compiler agrees with the agent
on a *valid* document. This one drives the paths that only fire when a published
document is wrong: every one of them must refuse, with a message that says what
was wrong, because a consumer that cannot evaluate a rule must never report a
payload as valid.
"""

from __future__ import annotations

import asyncio
import copy
import json
from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel, Field, ValidationError, create_model
from redis.exceptions import RedisError

from app.core.strategy_catalog import (
    CATALOG_VERSION,
    REDIS_STRATEGIES_KEY,
    CatalogError,
    compile_catalog,
    json_safe_errors,
    read_catalog,
)


def payload(**entry: Any) -> dict[str, Any]:
    """A minimal but complete catalog, in the published wire format."""
    strategy: dict[str, Any] = {
        "name": "unit_strategy",
        "label": "Unit",
        "description": "A strategy used only by these tests.",
        "settings_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "interval": {
                    "type": "string",
                    "enum": ["1h", "4h"],
                    "default": "1h",
                },
                "window": {"type": "integer", "default": 10, "exclusiveMinimum": 0},
                "ratio": {
                    "anyOf": [{"type": "number", "exclusiveMinimum": 0.0}, {"type": "string"}],
                    "default": "1.5",
                },
                "periods": {
                    "type": "array",
                    "default": [1, 2],
                    "minItems": 2,
                    "maxItems": 2,
                    "prefixItems": [
                        {"type": "integer", "exclusiveMinimum": 0},
                        {"type": "integer", "exclusiveMinimum": 0},
                    ],
                },
            },
        },
        "constraints": [],
    }
    strategy.update(entry)
    return {
        "version": CATALOG_VERSION,
        "schema_hash": "unit-hash",
        "published_at": "2026-09-15T00:00:00+00:00",
        "lookups": {"intervals_ms": {"1h": 3_600_000, "4h": 14_400_000}},
        "strategies": [strategy],
    }


def compiled(**entry: Any):
    return compile_catalog(payload(**entry)).strategy("unit_strategy")


def rule(**overrides: Any) -> dict[str, Any]:
    definition: dict[str, Any] = {
        "cmp": "gte",
        "lhs": {"field": "window"},
        "rhs": {"const": 1},
        "message": "window is too small",
    }
    definition.update(overrides)
    return definition


# --- A valid document still compiles ----------------------------------------


def test_a_complete_document_compiles() -> None:
    strategy = compiled()
    assert strategy is not None
    assert strategy.name == "unit_strategy"
    assert strategy.label == "Unit"
    assert strategy.description.startswith("A strategy")
    # The defaults the compiler fills for constraints come from the schema.
    assert strategy.validate({}) == []
    assert strategy.validate({"window": 5, "ratio": "2.5"}) == []
    # Declared bounds and the fixed length are enforced.
    assert strategy.validate({"window": 0})
    assert strategy.validate({"ratio": "nope"})
    assert strategy.validate({"interval": "2h"})
    assert strategy.validate({"periods": [1]})
    assert strategy.validate({"periods": [1, 2, 3]})
    assert "unknown parameter" in strategy.validate({"nope": 1})[0]["msg"]


def test_a_lookup_rule_reads_the_published_table() -> None:
    strategy = compiled(
        constraints=[
            rule(
                cmp="eq",
                lhs={"lookup": "intervals_ms", "field": "interval"},
                rhs={"const": 14_400_000},
                message="interval must be 4h",
            )
        ]
    )
    assert strategy is not None
    # Default "1h" is 3_600_000, not 14_400_000.
    assert [error["msg"] for error in strategy.validate({})] == ["interval must be 4h"]
    assert strategy.validate({"interval": "4h"}) == []


def test_a_message_renders_lhs_rhs_including_fractions() -> None:
    strategy = compiled(
        constraints=[
            rule(
                cmp="gte",
                lhs={"field": "ratio"},
                rhs={"const": 2.5},
                message="ratio must be at least {rhs}, got {lhs}",
            )
        ]
    )
    assert strategy is not None
    assert [error["msg"] for error in strategy.validate({})] == [
        "ratio must be at least 2.5, got 1.5"
    ]


def test_integer_placeholders_render_without_a_decimal_point() -> None:
    strategy = compiled(
        constraints=[
            rule(lhs={"field": "window"}, rhs={"const": 20}, message="at least {rhs}")
        ]
    )
    assert strategy is not None
    assert [error["msg"] for error in strategy.validate({})] == ["at least 20"]


def test_a_repeated_message_is_reported_once() -> None:
    strategy = compiled(
        constraints=[
            rule(lhs={"field": "window"}, rhs={"const": 20}, message="too small"),
            rule(
                cmp="gte",
                lhs={"field": ["periods", 0]},
                rhs={"const": 20},
                message="too small",
            ),
        ]
    )
    assert strategy is not None
    assert [error["msg"] for error in strategy.validate({})] == ["too small"]


# --- Documents the compiler must refuse -------------------------------------


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda doc: doc.update(version=99), "unsupported catalog version"),
        (lambda doc: doc.pop("schema_hash"), "no schema_hash"),
        (lambda doc: doc.update(lookups=[]), "lookups must be an object"),
        (lambda doc: doc.update(strategies=[]), "has no strategies"),
        (lambda doc: doc.update(strategies=[{}]), "malformed strategy entry"),
        (
            lambda doc: doc["strategies"][0].update(settings_schema="not-a-schema"),
            "must describe an object",
        ),
        (
            lambda doc: doc["strategies"][0]["settings_schema"].update(type="array"),
            "must describe an object",
        ),
        (
            lambda doc: doc["strategies"][0]["settings_schema"].pop("properties"),
            "has no properties",
        ),
        (
            lambda doc: doc["strategies"][0]["settings_schema"].pop(
                "additionalProperties"
            ),
            "must forbid extra properties",
        ),
        (
            lambda doc: doc["strategies"][0]["settings_schema"]["properties"].update(
                window=["not", "an", "object"]
            ),
            "property must be an object",
        ),
        (
            lambda doc: doc["strategies"][0]["settings_schema"]["properties"][
                "interval"
            ].update(enum=[]),
            "enum must be a non-empty list",
        ),
        (
            lambda doc: doc["strategies"][0]["settings_schema"]["properties"][
                "window"
            ].pop("type"),
            "no usable 'type'",
        ),
        (
            lambda doc: doc["strategies"][0]["settings_schema"]["properties"][
                "window"
            ].update(anyOf=[{"type": "number"}, {"type": "boolean"}]),
            "unsupported anyOf branches",
        ),
        (
            lambda doc: doc["strategies"][0]["settings_schema"]["properties"].update(
                odd={"anyOf": "not-a-list"}
            ),
            "anyOf must be a list",
        ),
        (
            lambda doc: doc["strategies"][0]["settings_schema"]["properties"][
                "periods"
            ].update(prefixItems=[]),
            "prefixItems must be a non-empty list",
        ),
        (
            lambda doc: doc["strategies"][0]["settings_schema"]["properties"][
                "periods"
            ].update(maxItems=3),
            "must set minItems = maxItems",
        ),
        (
            lambda doc: doc["strategies"][0]["settings_schema"]["properties"][
                "periods"
            ].update(
                prefixItems=[{"type": "integer"}, {"type": "string"}],
            ),
            "must share one schema",
        ),
        (
            lambda doc: doc["strategies"][0]["settings_schema"]["properties"][
                "window"
            ].update(exclusiveMinimum="zero"),
            "exclusiveMinimum must be a number",
        ),
        (
            lambda doc: doc["strategies"][0].update(constraints="not-a-list"),
            "constraints must be a list",
        ),
        (
            lambda doc: doc["strategies"][0].update(constraints=[rule(cmp="is")]),
            "unsupported comparison",
        ),
        (
            lambda doc: doc["strategies"][0].update(constraints=[{"cmp": "lt"}]),
            "malformed rule",
        ),
        (
            lambda doc: doc["strategies"][0].update(
                constraints=[rule(message="")]
            ),
            "needs a non-empty message",
        ),
        (
            lambda doc: doc["strategies"][0].update(
                settings_schema=None,
                constraints=[rule(lhs={"const": 1}, rhs={"const": 2})],
            ),
            "constraints without settings",
        ),
    ],
)
def test_a_broken_document_is_refused_with_a_reason(mutate: Any, expected: str) -> None:
    document = payload()
    mutate(document)
    with pytest.raises(CatalogError, match=expected):
        compile_catalog(document)


# --- Rules that cannot be evaluated -----------------------------------------


@pytest.mark.parametrize(
    ("constraint", "expected"),
    [
        (rule(rhs={"op": "pow", "args": [{"const": 2}, {"const": 2}]}), "pow"),
        (
            rule(lhs={"lookup": "missing", "field": "window"}),
            "unknown lookup table",
        ),
        (
            rule(lhs={"lookup": "intervals_ms", "field": "window"}),
            "is not a value of lookup",
        ),
        (rule(rhs={"field": ["periods", 3]}), "cannot index"),
        (rule(lhs={"field": "interval"}), "is not a number"),
        (rule(lhs="not-an-expression"), "malformed expression"),
        (rule(lhs={"op": "add", "args": [{"const": 1}]}), "at least two args"),
        (rule(lhs={"lookup": "intervals_ms"}), "malformed lookup expression"),
    ],
)
def test_an_unevaluable_rule_makes_the_payload_unverifiable(
    constraint: dict[str, Any], expected: str
) -> None:
    """Not "invalid": the backend cannot tell, so it must not say "valid".

    Driven with the all-defaults payload rather than a broken one: field-level
    failures return early, so a ``CatalogError`` here can only come from
    evaluation itself — which is exactly the case that must never be reported
    as either valid or invalid.
    """
    strategy = compiled(constraints=[constraint])
    assert strategy is not None
    with pytest.raises(CatalogError, match=expected):
        strategy.validate({})


def test_a_strategy_without_settings_accepts_only_nothing() -> None:
    document = payload(settings_schema=None, constraints=[])
    strategy = compile_catalog(document).strategy("unit_strategy")
    assert strategy is not None
    assert strategy.validate({}) == []
    assert "unknown parameter" in strategy.validate({"anything": 1})[0]["msg"]


# --- Reading from Redis -----------------------------------------------------


class _FakeRedis:
    def __init__(self, raw: str | None = None, fail: bool = False) -> None:
        self.raw = raw
        self.fail = fail

    async def get(self, key: str) -> str | None:
        assert key == REDIS_STRATEGIES_KEY
        if self.fail:
            raise RedisError("down")
        return self.raw


def read(raw: str | None, fail: bool = False):
    async def scenario():
        return await read_catalog(_FakeRedis(raw, fail))

    return asyncio.run(scenario())


def test_an_unpublished_catalog_is_no_catalog() -> None:
    assert read(None) is None
    assert read("") is None


def test_reading_reports_what_is_wrong() -> None:
    with pytest.raises(CatalogError, match="not valid JSON"):
        read("{not json")
    with pytest.raises(CatalogError, match="must be a JSON object"):
        read("[1, 2, 3]")


def test_a_redis_failure_is_not_swallowed() -> None:
    """The route owns the HTTP mapping; the reader must not pretend it reads."""
    with pytest.raises(RedisError):
        read(None, fail=True)


def test_an_unchanged_document_is_compiled_once() -> None:
    raw = json.dumps(payload())
    first = read(raw)
    second = read(raw)
    assert first is not None and first is second
    # A document that merely quotes a known hash is still recompiled.
    other = copy.deepcopy(payload())
    other["schema_hash"] = first.schema_hash
    other["strategies"][0]["label"] = "Renamed"
    recompiled = read(json.dumps(other))
    assert recompiled is not None and recompiled is not first
    assert recompiled.strategy("unit_strategy").label == "Renamed"


def test_compiler_crashes_are_reported_as_catalog_errors() -> None:
    """A malformed document must surface as 503, never as a 500."""
    document = payload()
    document["strategies"][0]["settings_schema"]["properties"]["window"]["type"] = 5
    with pytest.raises(CatalogError):
        compile_catalog(document)


# --- Pydantic errors must survive JSON --------------------------------------


def test_error_context_is_json_safe() -> None:
    """Decimal bounds would otherwise blow up the HTTPException detail."""
    model = create_model("_Bounds", value=(Decimal, Field(gt=0)))
    with pytest.raises(ValidationError) as caught:
        model.model_validate({"value": -1})
    errors = json_safe_errors(caught.value)
    assert errors[0]["loc"] == ["value"]
    assert isinstance(errors[0]["ctx"]["gt"], str)
    assert json.loads(json.dumps(errors))  # serializes as-is


def test_error_context_keeps_plain_values() -> None:
    class _Model(BaseModel):
        value: int = Field(gt=0)

    with pytest.raises(ValidationError) as caught:
        _Model.model_validate({"value": 0})
    errors = json_safe_errors(caught.value)
    assert errors[0]["ctx"]["gt"] == 0
    assert json.dumps(errors)


@pytest.mark.parametrize(
    "legacy_keys",
    [None, [], "old", {"old": 1}, {"": "window"}, {"old": ""}],
)
def test_malformed_legacy_keys_are_dropped_not_fatal(legacy_keys: Any) -> None:
    """Optional metadata: a bad map must not take Start validation down."""
    document = payload()
    if legacy_keys is not None:
        document["strategies"][0]["legacy_keys"] = legacy_keys
    strategy = compile_catalog(document).strategy("unit_strategy")
    assert strategy is not None
    assert strategy.legacy_keys == {}
    assert strategy.validate({"window": 5}) == []


def test_a_renamed_parameter_names_its_new_name() -> None:
    document = payload()
    document["strategies"][0]["legacy_keys"] = {"size": "window"}
    strategy = compile_catalog(document).strategy("unit_strategy")
    assert strategy is not None
    assert strategy.legacy_keys == {"size": "window"}
    assert [error["msg"] for error in strategy.validate({"size": 5})] == [
        "parameter 'size' was renamed to 'window'; reload the page"
    ]
    # Other extras keep the generic message.
    assert "unknown parameter" in strategy.validate({"other": 5})[0]["msg"]
