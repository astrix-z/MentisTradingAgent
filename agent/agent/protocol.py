"""Published strategy catalog — the backend/UI contract for strategy settings.

Why this exists
---------------
``TripleScreenSettings`` is owned by the agent alone. Rather than the backend
importing the agent package (which made the backend image depend on agent
source) or the UI mirroring the field list by hand, the agent *pushes* a
versioned description of every registered strategy over Redis, and the backend
validates ``start`` params against it. Redis stays the only channel between the
two services.

The document is written to ``agent:strategies`` with no TTL: liveness comes
from the heartbeat key, and a schema does not go stale merely because the agent
restarted. It is rewritten on every boot and periodically thereafter so a
Redis restart cannot leave the backend without it::

    {
      "version": 1,
      "schema_hash": "<sha256 over `strategies`>",
      "published_at": "<ISO 8601>",
      "lookups": {"intervals_ms": {"1m": 60000, ...}},
      "execution": {...},             # execution settings, same entry shape
      "strategies": [
        {
          "name": "tripple_screen",
          "label": "Triple Screen",
          "description": "...",
          "settings_schema": {...},   # JSON Schema 2020-12 for `params`
          "constraints": [ ... ],     # cross-field rules, see below
          "legacy_keys": {"<old>": "<new>"},  # renamed parameters
          "presets": [{"name": "...", "params": {...}}]  # built-in presets
        }
      ]
    }

``settings_schema`` carries everything JSON Schema can express: field names,
types, bounds, enums, fixed-length arrays and defaults. It is published with
``additionalProperties: false``: the *contract* is strict about unknown
parameters even though the agent itself tolerates them when reading a command
(``TripleScreenSettings.from_params`` filters to known keys, which is what lets
a payload carry shared keys).

Four things JSON Schema cannot express travel beside it:

* ``constraints`` — cross-field rules as a small, strategy-agnostic AST, so the
  backend and the UI enforce the *same* rule and render the *same* message
  instead of each mirroring domain logic;
* ``lookups`` — named value tables a rule may index by field value (the
  interval -> milliseconds map), so no consumer needs the exchange SDK;
* ``legacy_keys`` — renamed parameters, old name -> new name. The schema has
  only the new names; the agent still accepts the old ones (a runtime snapshot
  or a queued command may carry them), the backend names the new one when it
  rejects an old one, and the UI reads old names from saved params;
* ``presets`` — built-in presets, each a full snapshot of the non-const
  parameters in JSON form (decimals as strings). Optional for consumers: an
  older agent simply has none, so ``CATALOG_VERSION`` stays 1.

Constraint grammar
------------------
Expressions:

* ``{"const": <number>}``
* ``{"field": "<name>"}`` — value of a scalar field
* ``{"field": ["<name>", <index>]}`` — element of a fixed-length array field
* ``{"lookup": "<table>", "field": "<name>"}`` — ``table[value_of(field)]``
* ``{"op": "add" | "mul" | "max", "args": [expr, ...]}`` — n-ary fold
* ``{"op": "sub", "args": [expr, ...]}`` — left fold subtraction

Rules:

* ``{"cmp": "lt" | "lte" | "gt" | "gte" | "eq" | "ne", "lhs": expr,
   "rhs": expr, "message": "..."}``

``message`` may reference ``{lhs}`` and ``{rhs}`` (the evaluated values), which
is how "kline_window must be at least {rhs}" reports the real minimum.

Only these forms are supported. Everything else is a contract error and must be
rejected rather than ignored: a consumer that cannot evaluate a rule cannot
claim a payload is valid. The backend implements the same grammar in
``backend/app/core/strategy_catalog.py``; a change here needs a matching change
there.

Like ``agent:runtime``/``trading:heartbeat``, the key name is shared with the
backend — keep in sync with ``backend/app/api/routes/trading.py``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
import unicodedata
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

from agent.engine import REDIS_STRATEGIES_KEY
from agent.execution.settings import ExecutionSettings
from agent.strategies import STRATEGIES
from agent.strategies.base import Strategy

logger = logging.getLogger(__name__)

CATALOG_VERSION = 1

#: Widget hint value that takes its allowed values from the ``intervals_ms``
#: lookup (see :attr:`Strategy.settings_lookups`).
INTERVAL_WIDGET = "interval"
INTERVAL_LOOKUP = "intervals_ms"

_COMPARISONS = ("lt", "lte", "gt", "gte", "eq", "ne")
_OPERATORS = ("add", "sub", "mul", "max")


class CatalogError(ValueError):
    """A strategy declared settings the catalog cannot describe consistently."""


def _as_int(value: Any) -> int | None:
    """Return ``value`` as an int if it is an integral number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if float(value).is_integer() else None


def _addressable_fields(
    schema: dict[str, Any],
) -> tuple[set[str], dict[str, int]]:
    """Split published properties into scalar fields and fixed-length arrays.

    Raises:
        CatalogError: if a property is not a shape the constraint grammar can
            address (only scalars and fixed-length arrays are supported).
    """
    scalars: set[str] = set()
    arrays: dict[str, int] = {}
    for name, prop in (schema.get("properties") or {}).items():
        prefix = prop.get("prefixItems")
        if isinstance(prefix, list):
            length = len(prefix)
            if prop.get("maxItems") != length or prop.get("minItems") != length:
                raise CatalogError(
                    f"{name}: fixed-length arrays must set minItems = maxItems"
                )
            arrays[name] = length
            continue
        if (
            prop.get("type") in ("integer", "number", "string", "boolean")
            or "const" in prop
            or "enum" in prop
            or "anyOf" in prop
        ):
            scalars.add(name)
            continue
        raise CatalogError(
            f"{name}: unsupported property shape for constraint addressing: "
            f"{sorted(prop)}"
        )
    return scalars, arrays


def _check_expr(
    expr: Any,
    *,
    where: str,
    scalars: set[str],
    arrays: dict[str, int],
    lookups: dict[str, dict[str, int]],
) -> None:
    """Validate one expression, failing fast on anything unevaluable."""
    if not isinstance(expr, dict):
        raise CatalogError(f"{where}: expression must be an object, got {expr!r}")
    if "const" in expr:
        if set(expr) != {"const"} or _as_int(expr["const"]) is None:
            raise CatalogError(f"{where}: malformed const expression {expr!r}")
        return
    if "lookup" in expr:
        if set(expr) != {"lookup", "field"}:
            raise CatalogError(f"{where}: malformed lookup expression {expr!r}")
        table = expr["lookup"]
        field = expr["field"]
        if table not in lookups:
            raise CatalogError(
                f"{where}: unknown lookup table {table!r} "
                f"(known: {', '.join(sorted(lookups))})"
            )
        if field not in scalars:
            raise CatalogError(f"{where}: lookup key field {field!r} is not scalar")
        return
    if "field" in expr:
        target, index = _split_field(expr, where)
        if index is None:
            if target not in scalars:
                raise CatalogError(
                    f"{where}: {target!r} is not a scalar parameter "
                    f"(known: {', '.join(sorted(scalars))})"
                )
        else:
            if target not in arrays:
                raise CatalogError(f"{where}: {target!r} is not an array parameter")
            if index >= arrays[target]:
                raise CatalogError(
                    f"{where}: {target!r} has {arrays[target]} items, "
                    f"index {index} is out of range"
                )
        return
    if "op" in expr:
        op = expr["op"]
        if set(expr) != {"op", "args"}:
            raise CatalogError(f"{where}: malformed operator expression {expr!r}")
        if op not in _OPERATORS:
            raise CatalogError(f"{where}: unsupported operator {op!r}")
        args = expr["args"]
        if not isinstance(args, list) or len(args) < 2:
            raise CatalogError(f"{where}: operator {op!r} needs at least two args")
        for arg in args:
            _check_expr(
                arg, where=where, scalars=scalars, arrays=arrays, lookups=lookups
            )
        return
    raise CatalogError(f"{where}: unsupported expression {expr!r}")


def _split_field(expr: dict[str, Any], where: str) -> tuple[str, int | None]:
    if set(expr) != {"field"}:
        raise CatalogError(f"{where}: malformed field expression {expr!r}")
    field = expr["field"]
    if isinstance(field, str):
        return field, None
    if (
        isinstance(field, list)
        and len(field) == 2
        and isinstance(field[0], str)
        and _as_int(field[1]) is not None
    ):
        return field[0], _as_int(field[1])
    raise CatalogError(f"{where}: malformed field reference {field!r}")


def _check_rule(
    rule: Any,
    *,
    where: str,
    scalars: set[str],
    arrays: dict[str, int],
    lookups: dict[str, dict[str, int]],
) -> None:
    if not isinstance(rule, dict) or set(rule) != {"cmp", "lhs", "rhs", "message"}:
        raise CatalogError(f"{where}: malformed rule {rule!r}")
    if rule["cmp"] not in _COMPARISONS:
        raise CatalogError(f"{where}: unsupported comparison {rule['cmp']!r}")
    if not isinstance(rule["message"], str) or not rule["message"]:
        raise CatalogError(f"{where}: rule needs a non-empty message")
    for side in ("lhs", "rhs"):
        _check_expr(
            rule[side], where=where, scalars=scalars, arrays=arrays, lookups=lookups
        )


def _settings_schema(
    model: type[Any],
    *,
    where: str,
    lookups: dict[str, dict[str, int]],
) -> dict[str, Any]:
    """The model's JSON Schema, made strict and widget-annotated."""
    schema: dict[str, Any] = model.model_json_schema()
    # The published contract rejects unknown parameters (see module docstring).
    schema["additionalProperties"] = False
    for name, prop in (schema.get("properties") or {}).items():
        widget = (prop.get("ui") or {}).get("widget")
        if widget is None:
            continue
        if widget != INTERVAL_WIDGET:
            raise CatalogError(f"{where}.{name}: unsupported ui widget {widget!r}")
        table = lookups.get(INTERVAL_LOOKUP)
        if not table:
            raise CatalogError(
                f"{where}.{name}: the {INTERVAL_WIDGET!r} widget needs a "
                f"{INTERVAL_LOOKUP!r} lookup on the strategy"
            )
        prop["enum"] = list(table)
    return schema


def _description(cls: type[Strategy]) -> str:
    """First line of the strategy's docstring, else of its module's."""
    for doc in (cls.__doc__, getattr(sys.modules.get(cls.__module__), "__doc__", None)):
        if doc and doc.strip():
            return doc.strip().split("\n")[0]
    return ""


def describe_strategy(name: str, cls: type[Strategy]) -> dict[str, Any]:
    """Describe one registered strategy as the catalog entry the UI renders."""
    where = f"strategy {name!r}"
    model = cls.settings_model
    entry: dict[str, Any] = {
        "name": name,
        "label": cls.label or name,
        "description": _description(cls),
        "settings_schema": None,
        "constraints": [],
        "legacy_keys": {},
        "presets": [],
    }
    if model is None:
        if cls.settings_constraints:
            raise CatalogError(f"{where}: constraints without a settings model")
        if cls.settings_legacy_keys:
            raise CatalogError(f"{where}: legacy keys without a settings model")
        if cls.settings_presets:
            raise CatalogError(f"{where}: presets without a settings model")
        return entry
    schema = _settings_schema(model, where=where, lookups=cls.settings_lookups)
    scalars, arrays = _addressable_fields(schema)
    for index, rule in enumerate(cls.settings_constraints):
        _check_rule(
            rule,
            where=f"{where} constraint #{index}",
            scalars=scalars,
            arrays=arrays,
            lookups=cls.settings_lookups,
        )
    properties = schema.get("properties") or {}
    for old, new in cls.settings_legacy_keys.items():
        if old in properties or new not in properties:
            raise CatalogError(
                f"{where}: legacy key {old!r} -> {new!r} must map a retired "
                "name to a published parameter"
            )
    entry["settings_schema"] = schema
    entry["constraints"] = [dict(rule) for rule in cls.settings_constraints]
    entry["legacy_keys"] = dict(cls.settings_legacy_keys)
    entry["presets"] = _presets(cls, model, schema, where=where)
    return entry


#: Same rule as the backend's preset envelope and API (plan §4.2, §4.3.4).
PRESET_NAME_MAX = 64


def _presets(
    cls: type[Strategy], model: type[Any], schema: dict[str, Any], *, where: str
) -> list[dict[str, Any]]:
    """Published built-in presets, each checked to be a full valid snapshot.

    A preset must name exactly the non-const parameters (a const one is fixed
    by the schema, a missing one would be filled by a default and silently
    change with it) and must pass the settings model. Params are published as
    the model dumps them in JSON mode, so every consumer reads the same form.
    """
    properties = schema.get("properties") or {}
    editable = {name for name, prop in properties.items() if "const" not in prop}
    published: list[dict[str, Any]] = []
    seen: set[str] = set()
    for name, params in cls.settings_presets.items():
        label = f"{where} preset {name!r}"
        # The backend's envelope (plan §4.2): a string of 1–64 characters after
        # trim, without control characters or lone surrogates. Anything else
        # it would mark unreadable for good.
        if (
            not isinstance(name, str)
            or not 1 <= len(name.strip()) <= PRESET_NAME_MAX
            or any(unicodedata.category(char) in ("Cc", "Cs") for char in name.strip())
        ):
            raise CatalogError(
                f"{label}: name must be 1-{PRESET_NAME_MAX} characters "
                "without control characters"
            )
        identity = name.strip().lower()
        if identity in seen:
            raise CatalogError(f"{label}: names must be unique")
        seen.add(identity)
        keys = set(params)
        if keys != editable:
            raise CatalogError(
                f"{label}: must set exactly the non-const parameters; "
                f"missing {sorted(editable - keys)}, extra {sorted(keys - editable)}"
            )
        try:
            dumped = model.model_validate(params).model_dump(mode="json")
        except ValueError as exc:
            raise CatalogError(f"{label}: invalid: {exc}") from exc
        published.append(
            {
                "name": name,
                "params": {key: dumped[key] for key in properties if key in editable},
            }
        )
    return published


def _merge_lookups() -> dict[str, dict[str, int]]:
    """Union the strategies' lookup tables, rejecting conflicting definitions."""
    merged: dict[str, dict[str, int]] = {}
    for name, cls in STRATEGIES.items():
        for table, values in cls.settings_lookups.items():
            existing = merged.get(table)
            if existing is not None and existing != values:
                raise CatalogError(
                    f"lookup table {table!r} is declared differently by {name!r}"
                )
            merged[table] = dict(values)
    return merged


def describe_execution() -> dict[str, Any]:
    """The execution settings section, in the same shape as a strategy entry.

    One section for every strategy: sizing, slippage and the protection
    deadline belong to the account, not to a strategy. A Start carries these
    values as ``execution`` next to ``params``.
    """
    return {
        "name": "execution",
        "label": "Execution",
        "description": "Real order execution settings (plan §4.3, §7).",
        "settings_schema": _settings_schema(
            ExecutionSettings, where="execution", lookups={}
        ),
        "constraints": [],
    }


def describe_catalog() -> dict[str, Any]:
    """Build the published document for every registered strategy."""
    strategies = [
        describe_strategy(name, cls) for name, cls in sorted(STRATEGIES.items())
    ]
    execution = describe_execution()
    canonical = json.dumps(
        {"strategies": strategies, "execution": execution},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return {
        "version": CATALOG_VERSION,
        "schema_hash": hashlib.sha256(canonical.encode()).hexdigest(),
        "published_at": datetime.now(UTC).isoformat(),
        "lookups": _merge_lookups(),
        "strategies": strategies,
        "execution": execution,
    }


async def publish_catalog(redis: Redis) -> dict[str, Any] | None:
    """Publish the catalog to ``agent:strategies`` (best effort).

    Returns the published document, or ``None`` if Redis was unavailable; a
    failed publish must never take the agent down, and the refresh loop retries.
    """
    try:
        catalog = describe_catalog()
    except CatalogError:
        # A contract error is a programming mistake, not a transient failure:
        # log it loudly and keep the agent running (it can still trade).
        logger.exception("strategy catalog is inconsistent; not publishing")
        return None
    payload = json.dumps(catalog)
    try:
        await redis.set(REDIS_STRATEGIES_KEY, payload)
    except RedisError:
        logger.warning("cannot publish strategy catalog (Redis unavailable)")
        return None
    logger.info(
        "published strategy catalog: hash=%s strategies=%s",
        catalog["schema_hash"][:12],
        ",".join(entry["name"] for entry in catalog["strategies"]),
    )
    return catalog


__all__ = [
    "CATALOG_VERSION",
    "INTERVAL_LOOKUP",
    "INTERVAL_WIDGET",
    "CatalogError",
    "describe_catalog",
    "describe_execution",
    "describe_strategy",
    "publish_catalog",
]
