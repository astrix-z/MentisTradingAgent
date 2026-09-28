"""Saved presets and option lists of a strategy (plan §4.3.2–§4.3.5, PRESET-06).

``/trading/strategies/{strategy}/presets/`` and ``…/options/``. Common rules,
in order (§4.3.4): superuser only (``403``); the catalog is unavailable when it
is missing, unusable or Redis fails — one outcome; a strategy is known when the
catalog has it or, without a catalog, when it has option lists (``404``
otherwise); a database error is ``503``; ``409`` only for the preset-name
index, any other integrity error is a server error.

A preset is a full snapshot of the non-const parameters (§4.3.3): saving
fills defaults and stores every one, so a later default change does not move
it. Saving runs the same pipeline as a Start, lists included (T1: only a
launchable preset can be saved).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Annotated, Any

import redis
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from redis.exceptions import RedisError
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlmodel import Session, col, select

from app.api.deps import SessionDep
from app.api.routes.trading import (
    SuperUserDep,
    database_unavailable,
    strategy_params_errors,
)
from app.core.builtin_presets import sync_builtin_presets
from app.core.field_options import (
    OPTION_FIELDS,
    normalize_options,
    read_options,
    supported,
    unsupported,
)
from app.core.redis_client import get_redis, get_sync_redis
from app.core.strategy_catalog import (
    CatalogError,
    CompiledStrategy,
    preset_name,
    read_catalog,
)
from app.models import (
    StrategyBuiltinSync,
    StrategyFieldOptions,
    StrategyPreset,
    get_datetime_utc,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/trading/strategies", tags=["presets"])

#: The index that makes a name unique per strategy, ignoring case.
NAME_INDEX = "ux_strategy_preset_name"
#: Names of the built-in presets, reserved for users even before any sync.
RESERVED_NAMES = frozenset({"default", "fast"})

SyncRedisDep = Annotated[redis.Redis, Depends(get_sync_redis)]


# --- Documents -------------------------------------------------------------


class PresetIn(BaseModel):
    name: str
    params: dict[str, Any] = Field(default_factory=dict)


class PresetOut(BaseModel):
    id: uuid.UUID
    name: str
    is_builtin: bool
    params: dict[str, Any]
    #: ``null`` when the catalog is unavailable: validity is then unknown.
    valid: bool | None
    errors: list[dict[str, Any]] = Field(default_factory=list)


class BuiltinSyncState(BaseModel):
    status: str
    synced_at: datetime | None = None
    errors: list[dict[str, Any]] = Field(default_factory=list)


class PresetList(BaseModel):
    items: list[PresetOut]
    builtin_sync: BuiltinSyncState


class OptionsIn(BaseModel):
    options: list[str]


class FieldOptionsOut(BaseModel):
    options: list[str]
    #: ``options − S``; ``null`` when the catalog is unavailable (unknown).
    unsupported: list[str] | None


# --- Context -----------------------------------------------------------------


async def _catalog_strategy(strategy: str) -> tuple[bool, CompiledStrategy | None]:
    """``(catalog available, the strategy in it)``; any failure is unavailable."""
    try:
        catalog = await read_catalog(get_redis())
    except RedisError as exc:
        logger.warning("strategy catalog unavailable: %s", exc)
        return False, None
    except CatalogError as exc:
        logger.error("strategy catalog unusable: %s", exc)
        return False, None
    if catalog is None:
        return False, None
    return True, catalog.strategy(strategy)


def _has_option_lists(session: Session, strategy: str) -> bool:
    try:
        return bool(read_options(session, strategy))
    except SQLAlchemyError as exc:
        raise database_unavailable(exc) from exc


async def _strategy_context(strategy: str, session: Session) -> CompiledStrategy | None:
    """The compiled strategy, ``None`` without a catalog, or ``404`` (rule 3)."""
    available, compiled = await _catalog_strategy(strategy)
    if compiled is not None:
        return compiled
    if not available and _has_option_lists(session, strategy):
        return None
    raise HTTPException(status_code=404, detail=f"unknown strategy {strategy!r}")


def _require_catalog(compiled: CompiledStrategy | None) -> CompiledStrategy:
    if compiled is None:
        raise HTTPException(
            status_code=503,
            detail="the agent has not published a usable strategy catalog; "
            "refusing to validate the preset",
        )
    return compiled


def _valid_name(raw: str) -> str:
    name = preset_name(raw)
    if name is None:
        raise HTTPException(
            status_code=422,
            detail=[
                {
                    "loc": ["body", "name"],
                    "msg": "a name of 1-64 characters without control "
                    "characters is required",
                    "type": "value_error",
                }
            ],
        )
    return name


def _reserved(name: str) -> None:
    if name.lower() in RESERVED_NAMES:
        raise HTTPException(
            status_code=409,
            detail=f"the name {name!r} is reserved for a built-in preset",
        )


def _checked_params(
    compiled: CompiledStrategy, params: dict[str, Any], session: Session
) -> dict[str, Any]:
    """The full snapshot to store, or ``422``/``503`` (§4.3.3, §4.3.5)."""
    try:
        errors = strategy_params_errors(compiled, params, session)
    except CatalogError as exc:
        logger.error("strategy catalog not evaluable: %s", exc)
        raise HTTPException(
            status_code=503, detail="the agent's strategy catalog cannot be evaluated"
        ) from exc
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    return compiled.materialize(params)


def _commit(session: Session) -> None:
    """Commit; ``409`` for a taken name only, ``503`` for a database failure."""
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
        if constraint == NAME_INDEX:
            raise HTTPException(
                status_code=409, detail="a preset with this name already exists"
            ) from exc
        raise
    except SQLAlchemyError as exc:
        session.rollback()
        raise database_unavailable(exc) from exc


def _find(session: Session, strategy: str, preset_id: uuid.UUID) -> StrategyPreset:
    try:
        row = session.get(StrategyPreset, preset_id)
    except SQLAlchemyError as exc:
        raise database_unavailable(exc) from exc
    if row is None or row.strategy != strategy or row.retired_at is not None:
        raise HTTPException(status_code=404, detail="preset not found")
    return row


def _drift_errors(
    compiled: CompiledStrategy, params: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """A stored snapshot read against today's schema (§4.3.3).

    Old names are moved by ``legacy_keys`` first; then a non-const field the
    snapshot lacks is ``missing_in_preset`` and a key that is no longer one is
    ``unknown_param``.
    """
    moved, errors = compiled.apply_legacy_keys(params)
    editable = compiled.editable_fields()
    for name in sorted(editable - moved.keys()):
        errors.append(
            {
                "loc": ["body", "params", name],
                "msg": f"the preset has no value for the new parameter {name!r}",
                "type": "missing_in_preset",
            }
        )
    extra = sorted(moved.keys() - editable)
    if extra:
        errors.append(
            {
                "loc": ["body", "params"],
                "msg": f"unknown parameter(s): {', '.join(extra)}",
                "type": "unknown_param",
            }
        )
    return moved, errors


def _preset_out(
    row: StrategyPreset,
    compiled: CompiledStrategy | None,
    session: Session,
    options: dict[str, list[str]] | None,
) -> PresetOut:
    """Served with today's parameter names (old ones moved by ``legacy_keys``,
    R115): what the UI may send back. The row itself keeps its keys until the
    next ``PUT``."""
    if compiled is None:
        return _plain_out(row, None, [])
    moved, errors = _drift_errors(compiled, row.params)
    if not errors:
        try:
            errors = strategy_params_errors(compiled, moved, session, options)
        except CatalogError as exc:
            logger.error("strategy catalog not evaluable: %s", exc)
            return _plain_out(row, None, [], moved)
    return _plain_out(row, not errors, errors, moved)


def _plain_out(
    row: StrategyPreset,
    valid: bool | None,
    errors: list[dict[str, Any]],
    params: dict[str, Any] | None = None,
) -> PresetOut:
    return PresetOut(
        id=row.id,
        name=row.name,
        is_builtin=row.is_builtin,
        params=row.params if params is None else params,
        valid=valid,
        errors=errors,
    )


# --- Presets -----------------------------------------------------------------


@router.get("/{strategy}/presets/", response_model=PresetList)
async def list_presets(
    strategy: str, session: SessionDep, sync_redis: SyncRedisDep, _: SuperUserDep
) -> PresetList:
    """Saved and built-in presets, each checked against today's catalog.

    The built-in sync is blocking (it holds a row lock while it reads Redis)
    and runs in the threadpool, so this worker's event loop keeps serving
    (§4.3.2, R87). Everything below is read after it returned. Its failure
    is not an error here: the last recorded sync state is served.
    """
    compiled = await _strategy_context(strategy, session)
    if compiled is not None:
        await run_in_threadpool(sync_builtin_presets, session, sync_redis, strategy)
        compiled = (await _catalog_strategy(strategy))[1]
    try:
        rows = session.exec(
            select(StrategyPreset)
            .where(
                col(StrategyPreset.strategy) == strategy,
                col(StrategyPreset.retired_at).is_(None),
            )
            .order_by(
                col(StrategyPreset.is_builtin).desc(),
                func.lower(col(StrategyPreset.name)),
            )
        ).all()
        state = session.get(StrategyBuiltinSync, strategy)
        options = read_options(session, strategy) if compiled is not None else None
        items = [_preset_out(row, compiled, session, options) for row in rows]
    except SQLAlchemyError as exc:
        raise database_unavailable(exc) from exc
    builtin_sync = (
        BuiltinSyncState(
            status=state.status, synced_at=state.synced_at, errors=state.errors
        )
        if state is not None
        else BuiltinSyncState(status="never")
    )
    return PresetList(items=items, builtin_sync=builtin_sync)


@router.post("/{strategy}/presets/", status_code=201, response_model=PresetOut)
async def create_preset(
    strategy: str, body: PresetIn, session: SessionDep, user: SuperUserDep
) -> PresetOut:
    compiled = await _strategy_context(strategy, session)
    name = _valid_name(body.name)
    _reserved(name)
    params = _checked_params(_require_catalog(compiled), body.params, session)
    row = StrategyPreset(
        strategy=strategy, name=name, params=params, created_by=user.id
    )
    # Built before the commit: reading the expired row afterwards is another
    # SELECT, which could fail after the row was saved (R117).
    out = _plain_out(row, True, [])
    session.add(row)
    _commit(session)
    return out


@router.put("/{strategy}/presets/{preset_id}", response_model=PresetOut)
async def update_preset(
    strategy: str,
    preset_id: uuid.UUID,
    body: PresetIn,
    session: SessionDep,
    _: SuperUserDep,
) -> PresetOut:
    compiled = await _strategy_context(strategy, session)
    row = _find(session, strategy, preset_id)
    if row.is_builtin:
        raise HTTPException(status_code=409, detail="a built-in preset is read-only")
    name = _valid_name(body.name)
    if name.lower() != row.name.lower():
        _reserved(name)
    params = _checked_params(_require_catalog(compiled), body.params, session)
    row.name = name
    row.params = params
    row.updated_at = get_datetime_utc()
    out = _plain_out(row, True, [])  # before the commit, as in create_preset
    session.add(row)
    _commit(session)
    return out


@router.delete("/{strategy}/presets/{preset_id}", status_code=204)
async def delete_preset(
    strategy: str, preset_id: uuid.UUID, session: SessionDep, _: SuperUserDep
) -> Response:
    """Works without a catalog: deleting needs no validation."""
    await _strategy_context(strategy, session)
    row = _find(session, strategy, preset_id)
    if row.is_builtin:
        raise HTTPException(status_code=409, detail="a built-in preset is read-only")
    session.delete(row)
    _commit(session)
    return Response(status_code=204)


# --- Option lists --------------------------------------------------------------


@router.get("/{strategy}/options/", response_model=dict[str, FieldOptionsOut])
async def list_options(
    strategy: str, session: SessionDep, _: SuperUserDep
) -> dict[str, FieldOptionsOut]:
    compiled = await _strategy_context(strategy, session)
    try:
        options = read_options(session, strategy)
    except SQLAlchemyError as exc:
        raise database_unavailable(exc) from exc
    return {
        field: FieldOptionsOut(
            options=options[field],
            unsupported=unsupported(
                options[field],
                supported(compiled, field) if compiled is not None else None,
            ),
        )
        for field in OPTION_FIELDS
        if field in options
    }


@router.put("/{strategy}/options/{field}", response_model=FieldOptionsOut)
async def update_options(
    strategy: str, field: str, body: OptionsIn, session: SessionDep, _: SuperUserDep
) -> FieldOptionsOut:
    """Saved even without a catalog, values outside S included (D3)."""
    compiled = await _strategy_context(strategy, session)
    if field not in OPTION_FIELDS:
        raise HTTPException(status_code=404, detail=f"field {field!r} has no list")
    allowed = supported(compiled, field) if compiled is not None else None
    values, errors = normalize_options(field, body.options, allowed)
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    try:
        row = session.get(StrategyFieldOptions, (strategy, field))
        if row is None:
            row = StrategyFieldOptions(strategy=strategy, field=field, options=values)
        else:
            row.options = values
            row.updated_at = get_datetime_utc()
        session.add(row)
    except SQLAlchemyError as exc:
        raise database_unavailable(exc) from exc
    _commit(session)
    return FieldOptionsOut(options=values, unsupported=unsupported(values, allowed))


__all__ = ["router"]
