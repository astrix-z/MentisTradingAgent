"""Compiled view of the strategy catalog the agent publishes.

The agent owns strategy settings (``agent/agent/protocol.py``): it publishes a
JSON Schema per strategy plus cross-field rules in a small declarative grammar,
because ``TripleScreenSettings`` — and every domain rule about it — belongs to
the agent alone. The backend is still the *authoritative* validator for a
Start: nothing invalid may reach Redis. It just has no domain knowledge of its
own — it compiles whatever the agent declared and enforces exactly that.

Compilation does two things:

* turns ``settings_schema`` into a pydantic model, so field names, types,
  bounds, enums, fixed-length arrays, defaults and unknown-parameter rejection
  all come out of the agent's own declaration;
* turns ``constraints`` into evaluated rules, filling schema defaults for
  parameters the caller omitted exactly the way the agent's model does.

Two JSON Schema constructs are translated rather than mapped one-to-one, so
that the result stays statically typed: an ``enum``/``const`` becomes an
annotated membership check instead of a dynamically built ``Literal``, and a
fixed-length ``prefixItems`` array becomes a ``list`` with ``min_length`` equal
to ``max_length`` plus per-item bounds. Positional arrays whose items do not
share one schema are rejected outright rather than approximated — a consumer
that cannot express a rule exactly must not pretend it enforced it.

The grammar is implemented here a second time (the agent has its own copy in
``agent/protocol.py``) because the two services share a wire format, not code.
Both sides reject anything they cannot evaluate rather than guessing: a
consumer that cannot evaluate a rule cannot claim a payload is valid.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    create_model,
)
from redis.asyncio import Redis

logger = logging.getLogger(__name__)

# Shared with the agent; keep in sync with agent/agent/engine.py.
REDIS_STRATEGIES_KEY = "agent:strategies"

#: Catalog format this backend understands. A newer agent is a contract
#: change, not something to interpret optimistically.
CATALOG_VERSION = 1

_COMPARISONS: dict[str, Any] = {
    "lt": lambda lhs, rhs: lhs < rhs,
    "lte": lambda lhs, rhs: lhs <= rhs,
    "gt": lambda lhs, rhs: lhs > rhs,
    "gte": lambda lhs, rhs: lhs >= rhs,
    "eq": lambda lhs, rhs: lhs == rhs,
    "ne": lambda lhs, rhs: lhs != rhs,
}
_BOUNDS = {
    "exclusiveMinimum": "gt",
    "minimum": "ge",
    "exclusiveMaximum": "lt",
    "maximum": "le",
}
_PRIMITIVES: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
}


class CatalogError(ValueError):
    """The published catalog is missing, malformed, or not evaluable.

    Distinct from a rejected *payload*: this means the backend cannot tell
    whether a request is valid, so it must not pretend that it is.
    """


def json_safe_errors(exc: ValidationError) -> list[dict[str, Any]]:
    """Pydantic errors as plain JSON, with ``loc`` as a list.

    Pydantic carries implementation objects in the error context: the raw
    exception for a custom validator's ``ValueError``, and the actual ``Decimal``
    bounds for a Decimal field. Neither survives JSON, and an ``HTTPException``
    detail is not passed through ``jsonable_encoder`` the way FastAPI's own 422
    handling is, so both are stringified here.
    """
    errors: list[dict[str, Any]] = []
    for raw_error in exc.errors(include_url=False):
        error: dict[str, Any] = dict(raw_error)
        error["loc"] = list(raw_error.get("loc") or ())
        ctx = error.get("ctx")
        if ctx:
            error["ctx"] = {
                key: value
                if value is None or isinstance(value, (str, int, float, bool))
                else str(value)
                for key, value in ctx.items()
            }
        errors.append(error)
    return errors


# --- Schema -> pydantic model ----------------------------------------------


def _leaf(schema: dict[str, Any], where: str) -> tuple[Any, dict[str, Any]]:
    """Base annotation plus the node bounds come from, for one leaf node.

    Bounds are returned separately from the annotation because a top-level
    field must merge them with its default into a single ``Field``, while an
    array item has to carry them itself.
    """
    if "enum" in schema or "const" in schema:
        base = _primitive(schema, where)
        check = _membership(
            schema["enum"] if "enum" in schema else [schema["const"]], where
        )
        return Annotated[base, AfterValidator(check)], {}
    if "anyOf" in schema:
        # Pydantic emits ``anyOf: [number, string]`` for Decimal, which is
        # exactly what Decimal accepts; the numeric branch holds the bounds.
        branches = schema["anyOf"]
        if not isinstance(branches, list):
            raise CatalogError(f"{where}: anyOf must be a list")
        kinds = {
            str(branch.get("type")) for branch in branches if isinstance(branch, dict)
        }
        numeric = [
            branch
            for branch in branches
            if isinstance(branch, dict) and branch.get("type") == "number"
        ]
        if kinds == {"number", "string"} and len(numeric) == 1:
            return Decimal, numeric[0]
        raise CatalogError(f"{where}: unsupported anyOf branches {sorted(kinds)}")
    return _primitive(schema, where), schema


def _primitive(schema: dict[str, Any], where: str) -> Any:
    """The base Python type a leaf schema declares."""
    kind = schema.get("type")
    if kind not in _PRIMITIVES:
        raise CatalogError(
            f"{where}: unsupported schema node {sorted(schema)} (no usable 'type')"
        )
    return _PRIMITIVES[kind]


def _membership(allowed: Any, where: str) -> Callable[[Any], Any]:
    """A pydantic ``AfterValidator`` enforcing enum/const membership."""
    if not isinstance(allowed, list) or not allowed:
        raise CatalogError(f"{where}: enum must be a non-empty list")
    values = tuple(allowed)

    def check(value: Any) -> Any:
        if value not in values:
            raise ValueError(f"must be one of: {', '.join(map(str, values))}")
        return value

    return check


def _bounds(schema: dict[str, Any], where: str) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    for keyword, name in _BOUNDS.items():
        if keyword in schema:
            value = schema[keyword]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise CatalogError(f"{where}: {keyword} must be a number")
            kwargs[name] = value
    return kwargs


def _item_annotation(schema: dict[str, Any], where: str) -> Any:
    """Annotation for an array item, keeping that item's own bounds."""
    base, bounds_source = _leaf(schema, where)
    kwargs = _bounds(bounds_source, where)
    return Annotated[base, Field(**kwargs)] if kwargs else base


def _array_prefix(schema: dict[str, Any], where: str) -> list[Any] | None:
    """The validated ``prefixItems`` of a fixed-length array, if it is one.

    A published array is only compiled when its declared length is exactly
    fixed and its items share one schema: anything else would make the compiled
    rule stricter or looser than the declaration it came from.
    """
    prefix = schema.get("prefixItems")
    if prefix is None:
        return None
    if not isinstance(prefix, list) or not prefix:
        raise CatalogError(f"{where}: prefixItems must be a non-empty list")
    length = len(prefix)
    if schema.get("minItems") != length or schema.get("maxItems") != length:
        raise CatalogError(
            f"{where}: a fixed-length array must set minItems = maxItems = {length}"
        )
    if any(item != prefix[0] for item in prefix):
        raise CatalogError(
            f"{where}: positional array items must share one schema, so that the "
            f"constraint can be expressed exactly"
        )
    return prefix


def _array_annotation(prefix: list[Any], where: str) -> Any:
    """A fixed-length list whose items are validated against their own schema.

    Every item shares one schema (enforced by :func:`_array_prefix`), so the
    item type is compiled once into a ``TypeAdapter`` instead of subscripting a
    dynamically built type. Length is pinned in the same annotation so the whole
    array rule lives in one place.
    """
    item = TypeAdapter(_item_annotation(prefix[0], f"{where}[]"))
    length = len(prefix)

    def check(values: list[Any]) -> list[Any]:
        return [item.validate_python(value) for value in values]

    return Annotated[
        list[Any],
        Field(min_length=length, max_length=length),
        AfterValidator(check),
    ]


def _annotation(schema: dict[str, Any], where: str) -> Any:
    prefix = _array_prefix(schema, where)
    if prefix is not None:
        return _array_annotation(prefix, where)
    return _leaf(schema, where)[0]


def _field_kwargs(schema: dict[str, Any], where: str) -> dict[str, Any]:
    """Bounds for a top-level field, merged with its default into one Field."""
    if _array_prefix(schema, where) is not None:
        # Length and item bounds are part of the array annotation.
        return {}
    return _bounds(_leaf(schema, where)[1], where)


def _compile_model(entry: dict[str, Any]) -> type[BaseModel] | None:
    """Compile the entry's settings schema, or ``None`` if it declares none."""
    where = f"strategy {entry.get('name')!r}"
    schema = entry.get("settings_schema")
    if schema is None:
        return None
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise CatalogError(f"{where}: settings schema must describe an object")
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise CatalogError(f"{where}: settings schema has no properties")
    if schema.get("additionalProperties") is not False:
        # Without this the backend would silently accept parameters the agent
        # never declared, and the strictness would live nowhere.
        raise CatalogError(f"{where}: settings schema must forbid extra properties")
    fields: dict[str, Any] = {}
    for name, prop in properties.items():
        prop_where = f"{where}.{name}"
        if not isinstance(prop, dict):
            raise CatalogError(f"{prop_where}: property must be an object")
        annotation = _annotation(prop, prop_where)
        kwargs = _field_kwargs(prop, prop_where)
        if "default" in prop:
            fields[name] = (annotation, Field(default=prop["default"], **kwargs))
        else:
            fields[name] = (annotation, Field(**kwargs))
    return create_model(
        f"Compiled_{entry.get('name')}",
        __config__=ConfigDict(
            extra="forbid",
            # Mirrors the agent's model: the numeric branch of a Decimal field
            # must not smuggle in NaN/Infinity.
            allow_inf_nan=False,
        ),
        **fields,
    )


# --- Constraint evaluation -------------------------------------------------


def _number_of(value: Any, where: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise CatalogError(f"{where}: {value!r} is not a number")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation as exc:
            raise CatalogError(f"{where}: {value!r} is not a number") from exc
    raise CatalogError(f"{where}: {type(value).__name__} is not a number")


def _render(value: Decimal) -> str:
    """Integral decimals render without a trailing ``.0`` in rule messages."""
    return str(int(value)) if value == value.to_integral_value() else str(value)


def _eval_expr(expr: Any, values: dict[str, Any], lookups: dict[str, Any]) -> Decimal:
    if not isinstance(expr, dict):
        raise CatalogError(f"malformed expression {expr!r}")
    if "const" in expr:
        return _number_of(expr["const"], "const")
    if "lookup" in expr:
        table_name = expr["lookup"]
        if "field" not in expr:
            raise CatalogError(f"malformed lookup expression {expr!r}")
        table = lookups.get(table_name)
        if not isinstance(table, dict):
            raise CatalogError(f"unknown lookup table {table_name!r}")
        key = values.get(expr["field"])
        if str(key) not in table:
            raise CatalogError(f"{key!r} is not a value of lookup {table_name!r}")
        return _number_of(table[str(key)], f"lookup {table_name!r}")
    if "field" in expr:
        ref = expr["field"]
        if isinstance(ref, list):
            name, index = ref
            value = values.get(name)
            if not isinstance(value, (list, tuple)) or index >= len(value):
                raise CatalogError(f"cannot index {name!r} at {index}")
            return _number_of(value[index], f"field {name!r}")
        return _number_of(values.get(ref), f"field {ref!r}")
    op = expr.get("op")
    args = expr.get("args")
    if not isinstance(args, list) or len(args) < 2:
        raise CatalogError(f"operator {op!r} needs at least two args")
    numbers = [_eval_expr(arg, values, lookups) for arg in args]
    if op == "add":
        return sum(numbers, Decimal(0))
    if op == "mul":
        product = Decimal(1)
        for number in numbers:
            product *= number
        return product
    if op == "max":
        return max(numbers)
    if op == "sub":
        result = numbers[0]
        for number in numbers[1:]:
            result -= number
        return result
    raise CatalogError(f"unsupported operator {op!r}")


def _format_message(message: str, lhs: Decimal, rhs: Decimal) -> str:
    """Substitute ``{lhs}``/``{rhs}``, leaving any other braces untouched."""
    if "{" not in message:
        return message
    return message.format_map(_Placeholders(lhs=_render(lhs), rhs=_render(rhs)))


class _Placeholders(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _check_rule(rule: Any) -> None:
    if not isinstance(rule, dict) or set(rule) != {"cmp", "lhs", "rhs", "message"}:
        raise CatalogError(f"malformed rule {rule!r}")
    if rule["cmp"] not in _COMPARISONS:
        raise CatalogError(f"unsupported comparison {rule['cmp']!r}")
    if not isinstance(rule["message"], str) or not rule["message"]:
        raise CatalogError("rule needs a non-empty message")


# --- Compiled catalog -------------------------------------------------------


class CompiledStrategy:
    """One strategy's settings, as declared by the agent."""

    def __init__(self, entry: dict[str, Any], lookups: dict[str, Any]) -> None:
        self.name = str(entry.get("name"))
        self.label = str(entry.get("label") or self.name)
        self.description = str(entry.get("description") or "")
        self.settings_schema: dict[str, Any] | None = entry.get("settings_schema")
        self.lookups = lookups
        self._model = _compile_model(entry)
        raw_rules = entry.get("constraints") or []
        if not isinstance(raw_rules, list):
            raise CatalogError(f"strategy {self.name!r}: constraints must be a list")
        for rule in raw_rules:
            _check_rule(rule)
        self.constraints: list[dict[str, Any]] = list(raw_rules)
        self._defaults: dict[str, Any] = {
            name: prop.get("default")
            for name, prop in (
                (self.settings_schema or {}).get("properties") or {}
            ).items()
            if "default" in prop
        }
        if self._model is None and self.constraints:
            raise CatalogError(f"strategy {self.name!r}: constraints without settings")

    def validate(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Field-level and cross-field errors for a Start's ``params``.

        Empty means the agent will accept these parameters. Raises
        :class:`CatalogError` when a rule cannot be evaluated — the caller must
        not treat that as either valid or invalid.
        """
        if self._model is None:
            return [] if not params else [self._unknown_params(params.keys())]
        try:
            self._model.model_validate(params)
        except ValidationError as exc:
            errors = json_safe_errors(exc)
            return [
                self._unknown_params(self._extra_keys(error))
                if error["type"] == "extra_forbidden"
                else error
                for error in errors
            ]
        # Field-level rules passed, so fill the defaults the agent's own model
        # would fill and evaluate the cross-field rules against that.
        values = {**self._defaults, **params}
        failures: dict[tuple[Any, ...], dict[str, Any]] = {}
        for rule in self.constraints:
            try:
                lhs = _eval_expr(rule["lhs"], values, self.lookups)
                rhs = _eval_expr(rule["rhs"], values, self.lookups)
            except CatalogError as exc:
                raise CatalogError(
                    f"strategy {self.name!r}: rule {rule['message']!r} cannot be "
                    f"evaluated: {exc}"
                ) from exc
            if not _COMPARISONS[rule["cmp"]](lhs, rhs):
                failure = {
                    "loc": ["body", "params"],
                    "msg": _format_message(rule["message"], lhs, rhs),
                    "type": "value_error",
                }
                # Two rules may carry the same text (the ratio chain is two
                # comparisons); report it once, as the agent's own validator
                # would, instead of repeating an identical message.
                failures.setdefault((tuple(failure["loc"]), failure["msg"]), failure)
        return list(failures.values())

    @staticmethod
    def _extra_keys(error: dict[str, Any]) -> list[str]:
        return [str(part) for part in error.get("loc", [])[-1:]]

    def _unknown_params(self, keys: Any) -> dict[str, Any]:
        known = ", ".join(sorted(self._defaults)) or "<none>"
        return {
            "loc": ["body", "params"],
            "msg": (
                f"unknown parameter(s) for {self.name!r}: {', '.join(keys)}; "
                f"known: {known}"
            ),
            "type": "value_error",
        }


class CompiledCatalog:
    """The agent's published strategy declaration, ready to enforce."""

    def __init__(self, payload: dict[str, Any]) -> None:
        version = payload.get("version")
        if version != CATALOG_VERSION:
            raise CatalogError(
                f"unsupported catalog version {version!r} "
                f"(this backend understands {CATALOG_VERSION})"
            )
        hash_ = payload.get("schema_hash")
        if not isinstance(hash_, str) or not hash_:
            raise CatalogError("catalog has no schema_hash")
        self.schema_hash = hash_
        self.published_at = str(payload.get("published_at") or "")
        lookups = payload.get("lookups")
        if lookups is None:
            lookups = {}
        if not isinstance(lookups, dict):
            raise CatalogError("catalog lookups must be an object")
        self.lookups: dict[str, Any] = dict(lookups)
        entries = payload.get("strategies")
        if not isinstance(entries, list) or not entries:
            raise CatalogError("catalog has no strategies")
        self.strategies: dict[str, CompiledStrategy] = {}
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("name"):
                raise CatalogError(f"malformed strategy entry {entry!r}")
            strategy = CompiledStrategy(entry, self.lookups)
            self.strategies[strategy.name] = strategy
        # Execution settings travel next to the strategies in the same entry
        # shape and are enforced the same way. An agent that does not publish
        # them yet simply has none; a malformed section is a contract error.
        execution = payload.get("execution")
        self.execution: CompiledStrategy | None = None
        if execution is not None:
            if not isinstance(execution, dict):
                raise CatalogError("catalog execution section must be an object")
            self.execution = CompiledStrategy(execution, self.lookups)

    def strategy(self, name: str) -> CompiledStrategy | None:
        return self.strategies.get(name)


#: Compiled catalogs, keyed by the *raw* published document rather than by its
#: self-reported ``schema_hash``: the hash is the agent's claim, and trusting it
#: as a cache key would let a document that merely quotes a known hash reuse a
#: compiled catalog that does not match it. Keying on the document means any
#: change recompiles, and an unchanged one (the common case, since the agent
#: republishes periodically) is compiled once.
_CACHE: dict[str, CompiledCatalog] = {}
_CACHE_LIMIT = 8


def compile_catalog(payload: dict[str, Any]) -> CompiledCatalog:
    """Compile a published catalog document (pure; no caching)."""
    return CompiledCatalog(payload)


async def read_catalog(redis: Redis) -> CompiledCatalog | None:
    """Read, parse and compile the catalog, or ``None`` if unpublished.

    Raises:
        CatalogError: the published document is unusable.
        RedisError: Redis could not be reached (propagated to the caller,
            which owns the HTTP mapping).
    """
    raw = await redis.get(REDIS_STRATEGIES_KEY)
    if not raw:
        return None
    cached = _CACHE.get(raw)
    if cached is not None:
        return cached
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise CatalogError(f"published catalog is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise CatalogError("published catalog must be a JSON object")
    try:
        compiled = compile_catalog(payload)
    except CatalogError:
        raise
    except Exception as exc:  # a malformed document must not 500 the route
        raise CatalogError(f"cannot compile published catalog: {exc}") from exc
    if len(_CACHE) >= _CACHE_LIMIT:
        _CACHE.clear()
    _CACHE[raw] = compiled
    return compiled


__all__ = [
    "CATALOG_VERSION",
    "REDIS_STRATEGIES_KEY",
    "CatalogError",
    "CompiledCatalog",
    "CompiledStrategy",
    "compile_catalog",
    "json_safe_errors",
    "read_catalog",
]
