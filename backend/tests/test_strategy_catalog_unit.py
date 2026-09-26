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
                    "anyOf": [
                        {"type": "number", "exclusiveMinimum": 0.0},
                        {"type": "string"},
                    ],
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
            lambda doc: doc["strategies"][0].update(constraints=[rule(message="")]),
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


# --- The Start pipeline (plan §4.3.5, PRESET-04a) -----------------------------


def interval_field(**extra: Any) -> dict[str, Any]:
    """An interval-widget field, optionally without the enum a real one has."""
    field: dict[str, Any] = {
        "type": "string",
        "default": "1h",
        "ui": {"widget": "interval"},
    }
    field.update(extra)
    return field


def with_properties(**properties: Any) -> dict[str, Any]:
    document = payload()
    document["strategies"][0]["settings_schema"]["properties"].update(properties)
    return document


@pytest.mark.parametrize("value", [True, "5", 5.0, None])
def test_an_integer_field_takes_only_a_json_integer(value: Any) -> None:
    strategy = compiled()
    assert [error["type"] for error in strategy.validate({"window": value})] == [
        "int_type"
    ]


@pytest.mark.parametrize("value", [True, None])
def test_a_decimal_field_takes_a_number_or_a_string_only(value: Any) -> None:
    strategy = compiled()
    assert strategy.validate({"ratio": "2.5"}) == []
    assert strategy.validate({"ratio": 2.5}) == []
    assert [error["type"] for error in strategy.validate({"ratio": value})] == [
        "decimal_type"
    ]


def test_an_array_item_is_strict_too() -> None:
    strategy = compiled()
    assert [error["type"] for error in strategy.validate({"periods": [1, "2"]})] == [
        "int_type"
    ]


@pytest.mark.parametrize(
    ("value", "inside"),
    [
        ("1" + "0" * 64, True),  # one significant digit
        ("1e64", True),
        ("0E-100", True),  # zero is always inside
        ("1." + "0" * 70, True),
        ("1" + "0" * 63 + "1", False),  # 65 significant digits
        ("1" * 64 + "5", False),  # 65 digits, exponent 64: no rounding (R63)
        ("1" * 100 + "e-160", False),  # 100 digits at exponent -61
        ("1e65", False),
        ("1e-65", False),
    ],
)
def test_the_numeric_domain_is_judged_by_value(value: str, inside: bool) -> None:
    errors = compiled().validate({"ratio": value})
    if inside:
        assert "too_large" not in [error["type"] for error in errors]
    else:
        assert [error["type"] for error in errors] == ["too_large"]


def test_the_domain_runs_before_the_bounds_and_reports_once() -> None:
    # -1e65 breaks both the domain and exclusiveMinimum: only too_large.
    errors = compiled().validate({"ratio": "-1e65"})
    assert [(error["loc"], error["type"]) for error in errors] == [
        (["ratio"], "too_large")
    ]


def test_a_huge_integer_is_too_large_but_a_float_in_it_is_a_type_error() -> None:
    strategy = compiled()
    assert [e["type"] for e in strategy.validate({"window": 10**70})] == ["too_large"]
    assert [e["type"] for e in strategy.validate({"window": 1e65})] == ["int_type"]


def test_an_enum_miss_is_unsupported_value() -> None:
    errors = compiled().validate({"interval": "2h"})
    assert [(error["loc"], error["type"]) for error in errors] == [
        (["interval"], "unsupported_value")
    ]


def test_an_interval_field_is_checked_even_without_an_enum() -> None:
    """A catalog without enum must not turn 1M into a 503 at the lookup."""
    document = with_properties(tide=interval_field())
    document["strategies"][0]["constraints"] = [
        rule(
            cmp="gt",
            lhs={"lookup": "intervals_ms", "field": "tide"},
            rhs={"const": 0},
            message="tide must be positive",
        )
    ]
    strategy = compile_catalog(document).strategy("unit_strategy")
    assert strategy is not None
    assert strategy.validate({"tide": "4h"}) == []
    errors = strategy.validate({"tide": "1M"})
    assert [(error["loc"], error["type"]) for error in errors] == [
        (["tide"], "unsupported_value")
    ]


def test_rules_read_canonical_values_not_the_raw_payload() -> None:
    """A decimal sent as a JSON number and as a string compares the same."""
    strategy = compiled(
        constraints=[
            rule(
                cmp="lt",
                lhs={"field": "ratio"},
                rhs={"const": 2},
                message="ratio must be below 2",
            )
        ]
    )
    assert strategy.validate({"ratio": 1.5}) == []
    assert strategy.validate({"ratio": "1.5"}) == []
    assert [e["msg"] for e in strategy.validate({"ratio": 2.0})] == [
        "ratio must be below 2"
    ]


def test_rules_are_exact_beyond_the_default_decimal_precision() -> None:
    # 10**28 + 1 needs 29 digits: the default 28-digit context would round it
    # to 10**28 and accept.
    strategy = compiled(
        constraints=[
            rule(
                cmp="gte",
                lhs={"field": "window"},
                rhs={"op": "add", "args": [{"field": ["periods", 0]}, {"const": 1}]},
                message="window must be at least {rhs}",
            )
        ]
    )
    errors = strategy.validate({"window": 10**28, "periods": [10**28, 1]})
    assert [error["msg"] for error in errors] == [
        f"window must be at least {10**28 + 1}"
    ]


def test_a_catalog_constant_outside_the_domain_is_a_catalog_error() -> None:
    strategy = compiled(constraints=[rule(rhs={"const": 10**70})])
    with pytest.raises(CatalogError, match="outside the numeric domain"):
        strategy.validate({})


@pytest.mark.parametrize(
    ("expr", "digits"),
    [
        ({"const": 1}, 192),
        ({"op": "add", "args": [{"const": 1}, {"const": 2}]}, 192 + 1 + 1),
        ({"op": "max", "args": [{"const": 1}] * 11}, 192 + 2 + 1),
        ({"op": "mul", "args": [{"const": 1}, {"const": 2}]}, 384),
        (
            {
                "op": "mul",
                "args": [{"op": "add", "args": [{"const": 1}] * 2}, {"const": 3}],
            },
            194 + 192,
        ),
    ],
)
def test_rule_precision_follows_the_ast(expr: dict[str, Any], digits: int) -> None:
    from app.core.strategy_catalog import _precision

    assert _precision(expr) == digits


def test_apply_legacy_keys_moves_and_compares_after_validation() -> None:
    document = payload()
    document["strategies"][0]["legacy_keys"] = {"size": "window"}
    strategy = compile_catalog(document).strategy("unit_strategy")
    assert strategy is not None
    assert strategy.apply_legacy_keys({"size": 7}) == ({"window": 7}, [])
    assert strategy.apply_legacy_keys({"size": 7, "window": 7}) == ({"window": 7}, [])
    moved, errors = strategy.apply_legacy_keys({"size": 7, "window": 9})
    assert moved == {"window": 9}
    assert [error["type"] for error in errors] == ["legacy_conflict"]
    # An invalid old value is left under the new name for the field's error.
    moved, errors = strategy.apply_legacy_keys({"size": 0, "window": 7})
    assert (moved, errors) == ({"window": 0}, [])
    assert [e["type"] for e in strategy.validate(moved)] == ["greater_than"]


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        (True, "float_type"),
        ("5", "float_type"),
        (1e-300, "too_large"),
        (1e300, "too_large"),
    ],
)
def test_a_number_field_is_strict_and_in_the_domain(value: Any, kind: str) -> None:
    """R66: a float field must not reach the rules with 1e-300 (that was a 503)."""
    document = with_properties(x={"type": "number", "default": 1.0})
    document["strategies"][0]["constraints"] = [
        rule(
            cmp="gt",
            lhs={"op": "add", "args": [{"field": "x"}, {"const": 1}]},
            rhs={"const": 0},
            message="x must be above -1",
        )
    ]
    strategy = compile_catalog(document).strategy("unit_strategy")
    assert strategy is not None
    assert strategy.validate({"x": 2}) == []
    assert strategy.validate({"x": 2.5}) == []
    assert [error["type"] for error in strategy.validate({"x": value})] == [kind]


def test_an_interval_field_without_its_lookup_fails_at_compile_time() -> None:
    """R67: not a 503 on every Start of a catalog that reads as available."""
    document = with_properties(tide=interval_field())
    document["lookups"] = {}
    with pytest.raises(CatalogError, match="need an intervals_ms lookup"):
        compile_catalog(document)


@pytest.mark.parametrize(
    "prop",
    [
        {"type": "integer", "default": 10**70},
        {
            "anyOf": [{"type": "number"}, {"type": "string"}],
            "default": "1e70",
        },
        {
            "type": "array",
            "default": [1, 10**70],
            "minItems": 2,
            "maxItems": 2,
            "prefixItems": [{"type": "integer"}, {"type": "integer"}],
        },
    ],
)
def test_a_default_outside_the_domain_fails_at_compile_time(prop: Any) -> None:
    with pytest.raises(CatalogError, match="invalid defaults: big"):
        compile_catalog(with_properties(big=prop))


@pytest.mark.parametrize(
    "prop",
    [
        {"type": "integer", "default": "5"},  # wrong type
        {"type": "integer", "default": 0, "exclusiveMinimum": 0},  # out of bounds
    ],
)
def test_any_invalid_default_fails_at_compile_time(prop: Any) -> None:
    """R75: not only the domain — every default must pass its own field."""
    with pytest.raises(CatalogError, match="invalid defaults: bad"):
        compile_catalog(with_properties(bad=prop))


def test_a_field_without_a_default_is_not_a_catalog_defect() -> None:
    strategy = compile_catalog(with_properties(needed={"type": "integer"})).strategy(
        "unit_strategy"
    )
    assert strategy is not None
    assert [e["type"] for e in strategy.validate({})] == ["missing"]


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_a_numeric_array_rejects_non_finite_items(value: float) -> None:
    """R74: array items share the model's allow_inf_nan=False."""
    document = with_properties(
        w={
            "type": "array",
            "default": [1.0, 2.0],
            "minItems": 2,
            "maxItems": 2,
            "prefixItems": [{"type": "number"}, {"type": "number"}],
        }
    )
    strategy = compile_catalog(document).strategy("unit_strategy")
    assert strategy is not None
    assert strategy.validate({"w": [1.0, 2.0]}) == []
    assert [e["type"] for e in strategy.validate({"w": [1.0, value]})] == [
        "finite_number"
    ]


def test_non_finite_input_is_reported_as_plain_json() -> None:
    """R74: the error must survive a strict JSON writer (else a 500)."""
    errors = compiled().validate({"ratio": float("nan"), "periods": [1, float("inf")]})
    json.dumps(errors, allow_nan=False)
    assert {e["type"] for e in errors} == {"finite_number", "int_type"}


def test_apply_legacy_keys_compares_after_strict_validation() -> None:
    """R68: unlike the lax agent, "7" beside 7 is the field's type error."""
    document = payload()
    document["strategies"][0]["legacy_keys"] = {"size": "window"}
    strategy = compile_catalog(document).strategy("unit_strategy")
    assert strategy is not None
    moved, errors = strategy.apply_legacy_keys({"size": "7", "window": 7})
    assert (moved, errors) == ({"window": "7"}, [])
    assert [e["type"] for e in strategy.validate(moved)] == ["int_type"]


# --- Built-in presets: isolated parse (plan §4.2, PRESET-04) -----------------

GOOD = {"interval": "1h", "window": 10, "ratio": "1.5", "periods": [1, 2]}


def with_presets(presets: Any, **properties: Any) -> Any:
    document = with_properties(**properties)
    document["strategies"][0]["presets"] = presets
    document["strategies"][0]["constraints"] = [
        rule(
            cmp="gte",
            lhs={"field": "window"},
            rhs={"const": 5},
            message="window must be at least {rhs}",
        )
    ]
    return compile_catalog(document).strategy("unit_strategy")


def statuses(strategy: Any) -> list[tuple[int, str | None, str]]:
    return [(r.index, r.name, r.status) for r in strategy.presets.records]


def test_a_catalog_without_presets_reads_as_absent() -> None:
    strategy = compiled()
    assert strategy.presets.form == "absent"
    assert strategy.presets.records == []
    assert not strategy.presets.has_errors


def test_a_bad_preset_beside_a_good_one_leaves_catalog_and_start_working() -> None:
    strategy = with_presets(
        [
            {"name": "Good", "params": GOOD},
            {"name": "Bad", "params": {**GOOD, "window": 3}},  # breaks the rule
        ]
    )
    assert statuses(strategy) == [(0, "Good", "accepted"), (1, "Bad", "rejected")]
    assert [r.params for r in strategy.presets.accepted] == [GOOD]
    assert strategy.presets.errors == [{"index": 1, "name": "Bad", "type": "rejected"}]
    assert strategy.presets.has_errors
    assert strategy.validate({}) == []  # Start still validates


@pytest.mark.parametrize("presets", ["oops", {"name": "Good"}, 5])
def test_presets_that_are_not_a_list_are_malformed(presets: Any) -> None:
    strategy = with_presets(presets)
    assert strategy.presets.form == "malformed"
    assert strategy.presets.accepted == []
    assert strategy.presets.has_errors
    assert strategy.validate({}) == []


def test_a_preset_must_be_a_full_snapshot_without_const() -> None:
    strategy = with_presets(
        [
            {"name": "Empty", "params": {}},
            {"name": "Partial", "params": {"window": 10}},
            {"name": "Const", "params": {**GOOD, "fixed": "x"}},
            {"name": "Good", "params": GOOD},
        ],
        fixed={"type": "string", "const": "x", "default": "x"},
    )
    assert statuses(strategy) == [
        (0, "Empty", "rejected"),
        (1, "Partial", "rejected"),
        (2, "Const", "rejected"),
        (3, "Good", "accepted"),
    ]


def test_an_unreadable_entry_is_reported_by_index() -> None:
    strategy = with_presets([{"name": "Good", "params": GOOD}, None])
    assert statuses(strategy) == [(0, "Good", "accepted"), (1, None, "unreadable")]
    assert strategy.presets.errors == [{"index": 1, "name": None, "type": "unreadable"}]


@pytest.mark.parametrize(
    "entry",
    [
        {"name": "x" * 65, "params": GOOD},
        {"name": "   ", "params": GOOD},
        {"name": 5, "params": GOOD},
        {"name": "NoParams"},
        {"name": "ListParams", "params": [1]},
    ],
)
def test_an_envelope_defect_is_unreadable_and_good_ones_stay(entry: Any) -> None:
    strategy = with_presets([entry, {"name": "Good", "params": GOOD}])
    assert [r.status for r in strategy.presets.records] == ["unreadable", "accepted"]


def test_a_64_character_name_is_fine_and_trimmed() -> None:
    strategy = with_presets([{"name": "  " + "x" * 64 + " ", "params": GOOD}])
    assert statuses(strategy) == [(0, "x" * 64, "accepted")]


@pytest.mark.parametrize(
    "second",
    [
        {"name": "fast", "params": GOOD},  # same identity, both valid
        {"name": " FAST ", "params": {**GOOD, "window": 3}},  # accepted + rejected
    ],
)
def test_a_repeated_identity_rejects_the_whole_group(second: Any) -> None:
    strategy = with_presets(
        [
            {"name": "Fast", "params": GOOD},
            second,
            {"name": "Default", "params": GOOD},
        ]
    )
    assert [r.status for r in strategy.presets.records] == [
        "duplicate_name",
        "duplicate_name",
        "accepted",
    ]


def test_a_preset_with_an_old_parameter_name_is_translated() -> None:
    document = with_properties()
    entry = document["strategies"][0]
    entry["legacy_keys"] = {"size": "window"}
    params = {key: value for key, value in GOOD.items() if key != "window"}
    entry["presets"] = [{"name": "Old", "params": {**params, "size": 10}}]
    strategy = compile_catalog(document).strategy("unit_strategy")
    assert strategy is not None
    assert [r.params for r in strategy.presets.accepted] == [GOOD]


def test_an_unexpected_parse_failure_is_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core import strategy_catalog

    def boom(*_: Any) -> Any:
        raise RuntimeError("parser bug")

    monkeypatch.setattr(strategy_catalog.CompiledStrategy, "_preset_record", boom)
    strategy = with_presets([{"name": "Good", "params": GOOD}])
    assert strategy.presets.form == "malformed"
    assert strategy.validate({}) == []
