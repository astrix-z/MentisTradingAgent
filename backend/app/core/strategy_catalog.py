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
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal, Inexact, InvalidOperation, localcontext
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    Strict,
    TypeAdapter,
    ValidationError,
    create_model,
)
from pydantic_core import PydanticCustomError
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
# Strict where the wire type is unambiguous (plan §4.3.5 step 1): an integer
# field takes only a JSON integer — not a bool, a string or 5.0 — a number
# field a JSON number (integer or not, never a bool or a string), and a string
# or boolean field only its own type. Decimal fields (``anyOf`` number/string)
# stay lax on purpose: they take a JSON number or a decimal string.
_PRIMITIVES: dict[str, Any] = {
    "string": Annotated[str, Strict()],
    "integer": Annotated[int, Strict()],
    "number": Annotated[float, Strict()],
    "boolean": Annotated[bool, Strict()],
}

#: Numeric domain of the Start contract (plan §4.3.5 step 1): a non-zero
#: number has at most this many significant digits and an adjusted exponent
#: within ±DOMAIN_EXPONENT. Judged by value, not by its spelling.
DOMAIN_DIGITS = 64
DOMAIN_EXPONENT = 64
#: Precision a rule leaf needs: its significant digits plus alignment of any
#: two in-domain exponents to one fixed point (plan §4.3.5 step 4).
_LEAF_PRECISION = 3 * DOMAIN_DIGITS


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
    handling is, so both are stringified here. So is a non-finite float in the
    ``input`` or the context: the request body may carry ``NaN``/``Infinity``
    (Python's JSON parser accepts them), and the response writer does not.
    """
    errors: list[dict[str, Any]] = []
    for raw_error in exc.errors(include_url=False):
        error: dict[str, Any] = dict(raw_error)
        error["loc"] = list(raw_error.get("loc") or ())
        if "input" in error:
            error["input"] = _plain(error["input"])
        ctx = error.get("ctx")
        if ctx:
            error["ctx"] = {key: _plain(value) for key, value in ctx.items()}
        errors.append(error)
    return errors


def _plain(value: Any) -> Any:
    """``value`` as strict JSON: containers kept, anything else as a string."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    return str(value)


# --- Schema -> pydantic model ----------------------------------------------


def _as_decimal(value: Any) -> Decimal | None:
    """The finite numeric value of a wire value, or ``None`` if it is not one.

    A float is taken by its shortest decimal text, as a pydantic ``Decimal``
    field takes it, so the domain is judged on the value validation will see.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, int):
        number = Decimal(value)
    elif isinstance(value, float):
        number = Decimal(repr(value))
    elif isinstance(value, str):
        try:
            number = Decimal(value.strip())
        except InvalidOperation:
            return None
    else:
        return None
    return number if number.is_finite() else None


def in_domain(number: Decimal) -> bool:
    """Whether ``number`` is inside the numeric domain (zero always is)."""
    if number.is_zero():
        return True
    significant = len("".join(map(str, number.as_tuple().digits)).rstrip("0"))
    return (
        significant <= DOMAIN_DIGITS
        and -DOMAIN_EXPONENT <= number.adjusted() <= DOMAIN_EXPONENT
    )


def _domain(*kinds: type) -> BeforeValidator:
    """Reject an out-of-domain number before the field's own bounds run.

    Runs first so that such a value yields exactly one ``too_large`` error,
    whatever else is wrong with it. Values of other kinds pass through to the
    field's type check (a float in an integer field is a type error there).
    """

    def check(value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, kinds):
            return value
        number = _as_decimal(value)
        if number is not None and not in_domain(number):
            raise PydanticCustomError(
                "too_large",
                "number is outside the supported range: at most {digits} "
                "significant digits, exponent within ±{exponent}",
                {"digits": DOMAIN_DIGITS, "exponent": DOMAIN_EXPONENT},
            )
        return value

    return BeforeValidator(check)


def _leaf(schema: dict[str, Any], where: str) -> tuple[Any, dict[str, Any]]:
    """Base annotation plus the node bounds come from, for one leaf node.

    Bounds are returned separately from the annotation because a top-level
    field must merge them with its default into a single ``Field``, while an
    array item has to carry them itself.
    """
    if "enum" in schema or "const" in schema:
        base = _primitive(schema, where)
        is_enum = "enum" in schema
        check = _membership(
            schema["enum"] if is_enum else [schema["const"]], where, enum=is_enum
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
            return Annotated[Decimal, _domain(int, float, str, Decimal)], numeric[0]
        raise CatalogError(f"{where}: unsupported anyOf branches {sorted(kinds)}")
    base = _primitive(schema, where)
    if schema.get("type") == "integer":
        base = Annotated[base, _domain(int)]
    elif schema.get("type") == "number":
        base = Annotated[base, _domain(int, float)]
    return base, schema


def _primitive(schema: dict[str, Any], where: str) -> Any:
    """The base Python type a leaf schema declares."""
    kind = schema.get("type")
    if kind not in _PRIMITIVES:
        raise CatalogError(
            f"{where}: unsupported schema node {sorted(schema)} (no usable 'type')"
        )
    return _PRIMITIVES[kind]


def _membership(allowed: Any, where: str, *, enum: bool) -> Callable[[Any], Any]:
    """A pydantic ``AfterValidator`` enforcing enum/const membership.

    An enum miss is ``unsupported_value`` (the agent cannot serve the value,
    plan §4.3.5 step 1); a const miss stays a plain ``value_error``.
    """
    if not isinstance(allowed, list) or not allowed:
        raise CatalogError(f"{where}: enum must be a non-empty list")
    values = tuple(allowed)
    supported = ", ".join(map(str, values))

    def check(value: Any) -> Any:
        if value in values:
            return value
        if enum:
            raise PydanticCustomError(
                "unsupported_value",
                "unsupported value '{value}'; supported: {supported}",
                {"value": value, "supported": supported},
            )
        raise ValueError(f"must be one of: {supported}")

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
    # Items get the model's own rule: no NaN/Infinity in a numeric array.
    item = TypeAdapter(
        _item_annotation(prefix[0], f"{where}[]"),
        config=ConfigDict(allow_inf_nan=False),
    )
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
            # Defaults are published as JSON (a Decimal default is a string);
            # validating them makes every canonical value typed (step 2).
            validate_default=True,
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


def _catalog_number(value: Any, where: str) -> Decimal:
    """A number the catalog itself supplies (``const`` or a lookup value).

    It must be inside the domain too: the rule precision is derived from it, so
    an out-of-domain constant is a defect of the catalog, not of the request.
    """
    number = _number_of(value, where)
    if not in_domain(number):
        raise CatalogError(f"{where}: {value!r} is outside the numeric domain")
    return number


def _precision(expr: Any) -> int:
    """Digits that evaluate ``expr`` exactly for in-domain leaves (§4.3.5 step 4).

    A leaf needs ``_LEAF_PRECISION``; a sum, difference or maximum of ``n``
    arguments needs the widest argument plus ``ceil(log10(n)) + 1`` carry
    digits; a product needs the sum of its arguments' digits.
    """
    if not isinstance(expr, dict) or "op" not in expr:
        return _LEAF_PRECISION
    args = expr.get("args")
    if not isinstance(args, list) or not args:
        return _LEAF_PRECISION
    widths = [_precision(arg) for arg in args]
    if expr.get("op") == "mul":
        return sum(widths)
    return max(widths) + math.ceil(math.log10(len(widths))) + 1


def _render(value: Decimal) -> str:
    """Integral decimals render without a trailing ``.0`` in rule messages."""
    return str(int(value)) if value == value.to_integral_value() else str(value)


def _eval_expr(expr: Any, values: dict[str, Any], lookups: dict[str, Any]) -> Decimal:
    if not isinstance(expr, dict):
        raise CatalogError(f"malformed expression {expr!r}")
    if "const" in expr:
        return _catalog_number(expr["const"], "const")
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
        return _catalog_number(table[str(key)], f"lookup {table_name!r}")
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


# --- Built-in presets --------------------------------------------------------

#: Same rule as a preset name in the API (plan §4.3.4): trimmed, 1–64 chars.
PRESET_NAME_MAX = 64

PresetStatus = Literal["accepted", "rejected", "unreadable", "duplicate_name"]


@dataclass(frozen=True)
class PresetRecord:
    """One entry of a strategy's published ``presets`` list, as parsed."""

    index: int
    #: Trimmed name, or ``None`` when it fails the envelope (not a string,
    #: or not 1–64 characters after trim).
    name: str | None
    status: PresetStatus
    #: Params after ``legacy_keys`` translation; ``None`` if unreadable.
    params: dict[str, Any] | None = None
    #: Why it was not accepted (validation errors), for logs.
    reasons: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class PresetsParse:
    """What a strategy's ``presets`` metadata amounts to (plan §4.2).

    ``absent``: an older agent publishes none. ``malformed``: not a list,
    nothing accepted. ``list``: one record per entry. Any parse error makes the
    list non-authoritative (no retirement) and the sync status ``partial``.
    """

    form: Literal["absent", "malformed", "list"]
    records: list[PresetRecord] = field(default_factory=list)

    @property
    def accepted(self) -> list[PresetRecord]:
        return [record for record in self.records if record.status == "accepted"]

    @property
    def errors(self) -> list[dict[str, Any]]:
        return [
            {"index": record.index, "name": record.name, "type": record.status}
            for record in self.records
            if record.status != "accepted"
        ]

    @property
    def has_errors(self) -> bool:
        return self.form == "malformed" or bool(self.errors)


# --- Compiled catalog -------------------------------------------------------


def _legacy_keys(entry: dict[str, Any], name: str) -> dict[str, str]:
    """The strategy's renamed parameters (old -> new), or ``{}``.

    Optional metadata: the backend only carries it to the UI and names the new
    parameter when it rejects an old one, so a malformed map is logged and
    dropped rather than failing the whole catalog.
    """
    raw = entry.get("legacy_keys")
    if raw is None:
        return {}
    if isinstance(raw, dict) and all(
        isinstance(old, str) and old and isinstance(new, str) and new
        for old, new in raw.items()
    ):
        return dict(raw)
    logger.warning("strategy %r: ignoring malformed legacy_keys %r", name, raw)
    return {}


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
        self.legacy_keys = _legacy_keys(entry, self.name)
        self._defaults: dict[str, Any] = {
            name: prop.get("default")
            for name, prop in (
                (self.settings_schema or {}).get("properties") or {}
            ).items()
            if "default" in prop
        }
        if self._model is None and self.constraints:
            raise CatalogError(f"strategy {self.name!r}: constraints without settings")
        self._interval_fields: list[str] = [
            name
            for name, prop in (
                (self.settings_schema or {}).get("properties") or {}
            ).items()
            if isinstance(prop, dict)
            and (prop.get("ui") or {}).get("widget") == "interval"
        ]
        if self._interval_fields and not isinstance(
            self.lookups.get("intervals_ms"), dict
        ):
            raise CatalogError(
                f"strategy {self.name!r}: interval fields "
                f"{', '.join(self._interval_fields)} need an intervals_ms lookup"
            )
        self._check_defaults()
        self._raw_presets: Any = entry.get("presets")

    def parse_presets(self) -> PresetsParse:
        """Parse ``presets`` in isolation: a defect there never fails the catalog.

        Not part of compiling and not cached with it (plan §4.3.2): only the
        built-in sync calls this, after its idempotency check, so a Start and a
        sync ``noop`` never pay for it. Everything that goes wrong is logged
        and turned into a record status.
        """
        raw = self._raw_presets
        try:
            parsed = self._presets_from(raw)
        except Exception:  # isolation is the contract: log, never raise
            logger.exception("strategy %r: cannot parse presets", self.name)
            return PresetsParse(form="malformed")
        for error in parsed.errors:
            logger.warning(
                "strategy %r: built-in preset not accepted: %s", self.name, error
            )
        if parsed.form == "malformed":
            logger.warning("strategy %r: presets is not a list: %r", self.name, raw)
        return parsed

    def _presets_from(self, raw: Any) -> PresetsParse:
        if raw is None:
            return PresetsParse(form="absent")
        if not isinstance(raw, list):
            return PresetsParse(form="malformed")
        records = [self._preset_record(index, entry) for index, entry in enumerate(raw)]
        # One identity more than once rejects the whole group, whatever each
        # member's status: none of them may overwrite the stored row (plan
        # §4.2). A name that passed the envelope counts even when its params
        # are unreadable: a broken twin is an agent defect, not a winner.
        counts: dict[str, int] = {}
        for record in records:
            if record.name is not None:
                identity = record.name.lower()
                counts[identity] = counts.get(identity, 0) + 1
        return PresetsParse(
            form="list",
            records=[
                PresetRecord(
                    index=record.index,
                    name=record.name,
                    status="duplicate_name",
                    params=record.params,
                    reasons=record.reasons,
                )
                if record.name is not None and counts[record.name.lower()] > 1
                else record
                for record in records
            ],
        )

    def _preset_record(self, index: int, entry: Any) -> PresetRecord:
        """Envelope first (name, params object), then the preset itself."""
        if not isinstance(entry, dict):
            return PresetRecord(index=index, name=None, status="unreadable")
        raw_name = entry.get("name")
        name = raw_name.strip() if isinstance(raw_name, str) else None
        if name is not None and not 1 <= len(name) <= PRESET_NAME_MAX:
            name = None  # reported as null: only a valid name is an identity
        params = entry.get("params")
        if name is None or not isinstance(params, dict):
            return PresetRecord(index=index, name=name, status="unreadable")
        moved, reasons = self.apply_legacy_keys(params)
        editable = self.editable_fields()
        missing, extra = editable - moved.keys(), moved.keys() - editable
        if missing or extra:
            # A full snapshot of exactly the editable parameters: a missing one
            # would be filled by a default and change with it; a const one is
            # the schema's to fix.
            reasons.append(
                {
                    "msg": f"missing {sorted(missing)}, extra {sorted(extra)}",
                    "type": "incomplete_preset",
                }
            )
        if not reasons:
            try:
                reasons = self.validate(moved)
            except CatalogError as exc:
                reasons = [{"msg": str(exc), "type": "catalog_error"}]
        return PresetRecord(
            index=index,
            name=name,
            status="rejected" if reasons else "accepted",
            params=moved,
            reasons=reasons,
        )

    def editable_fields(self) -> set[str]:
        """The non-const parameters: what a preset must set, exactly."""
        properties = (self.settings_schema or {}).get("properties") or {}
        return {
            name
            for name, prop in properties.items()
            if isinstance(prop, dict) and "const" not in prop
        }

    def _check_defaults(self) -> None:
        """Every published default must pass its own field: a catalog defect
        found now, not dressed up as the user's error on each Start that omits
        the field (type, bounds and the numeric domain alike). A field without
        a default is the caller's to set and is not checked here.
        """
        if self._model is None:
            return
        try:
            self._model.model_validate({})
        except ValidationError as exc:
            defects = [
                error
                for error in exc.errors(include_url=False)
                if error["type"] != "missing"
            ]
            if defects:
                raise CatalogError(
                    f"strategy {self.name!r}: invalid defaults: "
                    + "; ".join(
                        f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in defects
                    )
                ) from exc

    def validate(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Errors for a Start's ``params``, through the pipeline of plan §4.3.5.

        1. fields — the compiled model: strict wire types, the numeric domain,
           bounds, enums (``unsupported_value``), unknown parameters;
        2. canonical values — the model's output with schema defaults; every
           later step reads these, never the raw payload;
        3. supported values — an interval-widget field must name an interval
           the agent serves (a guard for a catalog published without enum);
        4. cross-field rules, evaluated exactly.
        A step with errors stops the pipeline. (Step 5, the option lists kept
        in the database, is added by the preset API, PRESET-06.)

        Empty means the agent will accept these parameters. Raises
        :class:`CatalogError` when a rule cannot be evaluated — the caller must
        not treat that as either valid or invalid.
        """
        if self._model is None:
            return [] if not params else [self._unknown_params(params.keys())]
        try:
            canonical = self._model.model_validate(params).model_dump()
        except ValidationError as exc:
            return [
                self._extra_param(self._extra_keys(error))
                if error["type"] == "extra_forbidden"
                else error
                for error in json_safe_errors(exc)
            ]
        unsupported = self._unsupported(canonical)
        if unsupported:
            return unsupported
        return self._rule_failures(canonical)

    def _unsupported(self, values: dict[str, Any]) -> list[dict[str, Any]]:
        """Step 3: interval-widget fields must name a served interval."""
        errors: list[dict[str, Any]] = []
        table = self.lookups.get("intervals_ms") or {}  # present: checked at compile
        for name in self._interval_fields:
            value = values.get(name)
            if str(value) not in table:
                errors.append(
                    {
                        "loc": [name],
                        "msg": f"unsupported value {value!r}; supported: "
                        f"{', '.join(table)}",
                        "type": "unsupported_value",
                    }
                )
        return errors

    def _rule_failures(self, values: dict[str, Any]) -> list[dict[str, Any]]:
        """Step 4: every published rule, at a precision that makes it exact."""
        failures: dict[tuple[Any, ...], dict[str, Any]] = {}
        for rule in self.constraints:
            precision = max(_precision(rule["lhs"]), _precision(rule["rhs"]))
            try:
                with localcontext() as context:
                    context.prec = precision
                    context.traps[Inexact] = True
                    lhs = _eval_expr(rule["lhs"], values, self.lookups)
                    rhs = _eval_expr(rule["rhs"], values, self.lookups)
            except (CatalogError, Inexact) as exc:
                # In-domain values cannot round at this precision, so a trap
                # here means the catalog itself is out of contract.
                raise CatalogError(
                    f"strategy {self.name!r}: rule {rule['message']!r} cannot be "
                    f"evaluated exactly: {exc!r}"
                ) from exc
            if not _COMPARISONS[rule["cmp"]](lhs, rhs):
                failure = {
                    "loc": ["body", "params"],
                    "msg": _format_message(rule["message"], lhs, rhs),
                    "type": "value_error",
                }
                # Two rules may carry the same text (the order chain is two
                # comparisons); report it once, as the agent's own validator
                # would, instead of repeating an identical message.
                failures.setdefault((tuple(failure["loc"]), failure["msg"]), failure)
        return list(failures.values())

    def apply_legacy_keys(
        self, params: dict[str, Any]
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Move renamed parameters to their new names (agent rule, plan §4.1.1).

        For reading stored or built-in presets, never for a Start (a Start with
        an old name is refused, see :meth:`_extra_param`). An old name alone is
        moved as is and validated later like any value. With both names, each
        value is validated by the field's **strict** type first; an invalid one
        is left under the new name for the field's own error, and two valid
        values that differ are a ``legacy_conflict``. Strictness is where this
        differs from the agent on purpose: ``"7"`` beside ``7`` is a type error
        here, while the lax agent reads both as 7. Presets only ever reach this
        helper in their JSON form (saved through the strict API, or published
        with integers as numbers), so a string there is a defect worth showing.
        """
        moved = dict(params)
        errors: list[dict[str, Any]] = []
        if self._model is None:
            return moved, errors
        fields = self._model.model_fields
        for old, new in self.legacy_keys.items():
            if old not in moved:
                continue
            legacy = moved.pop(old)
            if new not in moved or new not in fields:
                moved[new] = legacy
                continue
            info = fields[new]
            adapter: TypeAdapter[Any] = TypeAdapter(
                Annotated[info.annotation, *info.metadata]
            )
            try:
                legacy_value = adapter.validate_python(legacy)
            except ValidationError:
                moved[new] = legacy
                continue
            try:
                current_value = adapter.validate_python(moved[new])
            except ValidationError:
                continue
            if legacy_value != current_value:
                errors.append(
                    {
                        "loc": ["body", "params"],
                        "msg": f"{old} (renamed to {new}) and {new} disagree: "
                        f"{legacy!r} != {moved[new]!r}",
                        "type": "legacy_conflict",
                    }
                )
        return moved, errors

    @staticmethod
    def _extra_keys(error: dict[str, Any]) -> list[str]:
        return [str(part) for part in error.get("loc", [])[-1:]]

    def _extra_param(self, keys: list[str]) -> dict[str, Any]:
        """An extra key; a renamed one says so, since only a stale UI sends it."""
        renamed = [key for key in keys if key in self.legacy_keys]
        if not renamed:
            return self._unknown_params(keys)
        return {
            "loc": ["body", "params"],
            "msg": (
                f"parameter {renamed[0]!r} was renamed to "
                f"{self.legacy_keys[renamed[0]]!r}; reload the page"
            ),
            "type": "value_error",
        }

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
    return compile_published(raw)


def compile_published(raw: str) -> CompiledCatalog:
    """Parse and compile a published document, cached by its raw text.

    Raises:
        CatalogError: the document is unusable.
    """
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
    "PresetRecord",
    "PresetsParse",
    "compile_catalog",
    "compile_published",
    "json_safe_errors",
    "read_catalog",
]
