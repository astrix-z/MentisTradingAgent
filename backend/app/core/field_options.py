"""Option lists of the select fields (plan §4.3.1 T2, §4.3.5 step 5, §4.4).

A list narrows what the agent supports (S, from the published catalog) to what
the operator offers. It is edited in the UI and kept in
``strategy_field_options``; nothing is cached, so an edit applies to every
worker at once. A value outside S may be saved into a list (D3) — it is then
reported as ``unsupported`` — but never into a preset or a Start.
"""

from __future__ import annotations

import re
from typing import Any

from sqlmodel import Session, col, select

from app.core.strategy_catalog import CompiledStrategy
from app.models import StrategyFieldOptions

#: The fields that have a list, in display order.
OPTION_FIELDS: tuple[str, ...] = (
    "quote_asset",
    "tide_interval",
    "wave_interval",
    "ripple_interval",
)

#: A UI label of an interval (``H1``, ``M15``, ``W1``): upper-case unit first.
_LABEL = re.compile(r"^([MHDW])([0-9]+)$")


def read_options(session: Session, strategy: str) -> dict[str, list[str]]:
    """The stored lists of ``strategy``; a field without a row has no list.

    Raises whatever the database raises: a failed read is ``503`` for the
    caller, never "no lists" (plan §4.3.5 step 5).
    """
    rows = session.exec(
        select(StrategyFieldOptions).where(
            col(StrategyFieldOptions.strategy) == strategy
        )
    ).all()
    return {row.field: list(row.options) for row in rows if row.field in OPTION_FIELDS}


def option_errors(
    options: dict[str, list[str]], canonical: dict[str, Any]
) -> list[dict[str, Any]]:
    """Step 5: each listed field's effective value must be on its list.

    ``canonical`` is the pipeline's output, so a field left out of the params
    is checked by its default (``{}`` with the list ``["USDC"]`` fails on the
    default ``USDT``).
    """
    errors: list[dict[str, Any]] = []
    for field in OPTION_FIELDS:
        allowed = options.get(field)
        if allowed is None or field not in canonical:
            continue
        value = canonical[field]
        if str(value) not in allowed:
            errors.append(
                {
                    "loc": [field],
                    "msg": f"value {value!r} is not in the option list of "
                    f"{field}: {', '.join(allowed)}",
                    "type": "option_value",
                }
            )
    return errors


def supported(compiled: CompiledStrategy, field: str) -> list[str] | None:
    """S(field): the values the agent serves for ``field``, or ``None``.

    The field's published ``enum``; for an interval field without one, the
    intervals of the ``intervals_ms`` lookup. ``None`` when the catalog does
    not declare the field at all.
    """
    properties = (compiled.settings_schema or {}).get("properties") or {}
    prop = properties.get(field)
    if not isinstance(prop, dict):
        return None
    enum = prop.get("enum")
    if isinstance(enum, list):
        return [str(value) for value in enum]
    if (prop.get("ui") or {}).get("widget") == "interval":
        table = compiled.lookups.get("intervals_ms")
        if isinstance(table, dict):
            return [str(code) for code in table]
    return None


def unsupported(options: list[str], allowed: list[str] | None) -> list[str] | None:
    """``options − S``; ``None`` when S is unknown (no catalog), not ``[]``."""
    if allowed is None:
        return None
    return [value for value in options if value not in allowed]


def normalize_value(field: str, raw: str, allowed: list[str] | None) -> str:
    """One entry of a list as the editor stores it (plan §4.4).

    Trimmed; an interval field then keeps an exact code of S (case matters:
    ``1m`` is a minute, ``1M`` a month), turns a strict label into its code
    (``H1`` → ``1h``, ``W1`` → ``1w``) and keeps anything else as it is, to be
    shown as unsupported (D3).
    """
    value = raw.strip()
    if field == "quote_asset":
        return value
    if allowed is not None and value in allowed:
        return value
    match = _LABEL.fullmatch(value)
    if match is not None:
        unit, count = match.groups()
        return f"{int(count)}{unit.lower()}"
    return value


def normalize_options(
    field: str, raw: list[str], allowed: list[str] | None
) -> tuple[list[str], list[dict[str, Any]]]:
    """The list to store, or ``422`` errors (empty list, empty entry, duplicate)."""
    loc = ["body", "options"]
    if not raw:
        return [], [{"loc": loc, "msg": "the list is empty", "type": "value_error"}]
    values: list[str] = []
    errors: list[dict[str, Any]] = []
    for index, entry in enumerate(raw):
        value = normalize_value(field, entry, allowed)
        if not value:
            errors.append(
                {"loc": [*loc, index], "msg": "empty value", "type": "value_error"}
            )
        elif value in values:
            errors.append(
                {
                    "loc": [*loc, index],
                    "msg": f"duplicate value {value!r} (after normalization)",
                    "type": "value_error",
                }
            )
        else:
            values.append(value)
    return values, errors


__all__ = [
    "OPTION_FIELDS",
    "normalize_options",
    "normalize_value",
    "option_errors",
    "read_options",
    "supported",
    "unsupported",
]
