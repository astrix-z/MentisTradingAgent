"""Built-in presets: bring ``strategy_preset`` in line with the agent's catalog.

docs/STRATEGY_PRESETS_PLAN.md §4.3.2 (PRESET-05b). The agent publishes its
built-in presets (``Default``, ``Fast``) in the catalog; this module copies the
accepted ones into the database, one transaction per call:

1. lock the strategy's ``strategy_builtin_sync`` row (created on first use);
2. under the lock, re-read the catalog from Redis and compute the idempotency
   key from the raw inputs of the parse;
3. same key and no ``name_conflict`` → ``noop``; otherwise parse ``presets``
   and settle identity groups by the database's own ``lower()``;
4. upsert every accepted preset (built-in rows only; a user's row with the
   same name is a ``name_conflict`` and is left alone);
5. retire built-in rows missing from an authoritative list;
6. record the key, ``status`` and ``errors``; commit.

Nothing is cached in the process: whether there is work to do is decided by the
locked row alone, so every worker converges on what Redis holds now. A failure
(Redis, database, an unusable catalog) rolls everything back and is retried on
the next call; it never blocks a Start.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session

from app.core.strategy_catalog import (
    REDIS_STRATEGIES_KEY,
    CatalogError,
    PresetsParse,
    compile_published,
)

logger = logging.getLogger(__name__)

#: Version of the rules that turn a published ``presets`` list into records
#: (the parse of plan §4.2 and the pipeline of §4.3.5). Part of the idempotency
#: key: raising it re-parses an unchanged list, so a preset rejected by old
#: rules is not stuck. Raise it whenever those rules change.
PRESETS_PIPELINE_VERSION = 1

#: sha256 of ``strategy_catalog.py`` (the parse and the pipeline live there)
#: for each pipeline version. A test fails when the file no longer matches the
#: current version's entry: then add a new version with the new hash rather
#: than editing an old entry.
PIPELINE_SOURCE_SHA256: dict[int, str] = {
    1: "80a1c6b0ef063270ad18d05db611e9da1a263e96723ef136c1ae2881a7006da1",
}

#: How long a Redis read may hold the lock: a hung Redis must not queue every
#: worker's ``GET presets/`` behind this row.
REDIS_READ_TIMEOUT_S = 2.0

SyncResult = Literal[
    "noop",  # the locked row already reflects this catalog
    "applied",  # parsed and written (status ok or partial)
    "no_catalog",  # nothing published, or not this strategy
    "absent",  # the strategy publishes no presets (an older agent)
    "malformed",  # presets is not a list
    "failed",  # Redis, the database or the catalog failed; nothing changed
]


#: What a sync survives: logged, rolled back, retried by the next call.
_SYNC_FAILURES = (RedisError, TimeoutError, SQLAlchemyError, CatalogError)


@dataclass(frozen=True)
class SyncOutcome:
    result: SyncResult
    #: Written with ``applied``: ``ok`` or ``partial``.
    status: str | None = None
    errors: list[dict[str, Any]] = field(default_factory=list)


def content_hash(document: dict[str, Any], entry: dict[str, Any]) -> str:
    """The idempotency key: sha256 of the canonical JSON of the parse inputs.

    ``published_at`` and ``schema_hash`` are left out, so a republished
    unchanged catalog is a ``noop``; a changed schema, rule, legacy key, lookup
    or pipeline version re-parses an unchanged ``presets``.
    """
    inputs = {
        "presets": entry.get("presets"),
        "settings_schema": entry.get("settings_schema"),
        "constraints": entry.get("constraints"),
        "legacy_keys": entry.get("legacy_keys"),
        "lookups": document.get("lookups"),
        "pipeline_version": PRESETS_PIPELINE_VERSION,
    }
    canonical = json.dumps(
        inputs, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


async def sync_builtin_presets(
    session: Session, redis: Redis, strategy: str
) -> SyncOutcome:
    """Run one sync of ``strategy``'s built-in presets (plan §4.3.2).

    Owns the session's transaction: call it with no pending work, and read the
    presets for a response only after it returns (commit or rollback done).
    """
    try:
        outcome = await _sync(session, redis, strategy)
    except _SYNC_FAILURES:
        session.rollback()
        logger.exception("strategy %r: built-in preset sync failed", strategy)
        return SyncOutcome("failed")
    except BaseException:
        session.rollback()
        raise
    if outcome.result == "applied":
        session.commit()
    else:
        session.rollback()
    return outcome


async def _sync(session: Session, redis: Redis, strategy: str) -> SyncOutcome:
    # 1. The lock row; every worker's sync of this strategy queues behind it.
    session.execute(
        text(
            "INSERT INTO strategy_builtin_sync (strategy, content_hash, status) "
            "VALUES (:s, '', 'never') ON CONFLICT DO NOTHING"
        ),
        {"s": strategy},
    )
    row = (
        session.execute(
            text(
                "SELECT content_hash, errors FROM strategy_builtin_sync "
                "WHERE strategy = :s FOR UPDATE"
            ),
            {"s": strategy},
        )
        .mappings()
        .one()
    )

    # 2. What Redis holds now, read under the lock, and its key.
    raw = await asyncio.wait_for(
        redis.get(REDIS_STRATEGIES_KEY), timeout=REDIS_READ_TIMEOUT_S
    )
    if not raw:
        return SyncOutcome("no_catalog")
    try:
        document = json.loads(raw)
    except ValueError as exc:
        raise CatalogError(f"published catalog is not valid JSON: {exc}") from exc
    entry = _strategy_entry(document, strategy)
    if entry is None:
        return SyncOutcome("no_catalog")
    key = content_hash(document, entry)

    # 3. Same inputs and nothing that depends on the database → nothing to do.
    stored_errors = row["errors"] or []
    if key == row["content_hash"] and not any(
        error.get("type") == "name_conflict" for error in stored_errors
    ):
        return SyncOutcome("noop")
    compiled = compile_published(raw).strategy(strategy)
    if compiled is None:
        return SyncOutcome("no_catalog")
    parsed = compiled.parse_presets()
    if parsed.form == "absent":
        return SyncOutcome("absent")
    if parsed.form == "malformed":
        return SyncOutcome("malformed")
    parsed = _regroup_by_database(session, parsed)

    # 4. Upsert what was accepted; a user's row with that name is not touched.
    conflicts: list[dict[str, Any]] = []
    for record in parsed.accepted:
        written = session.execute(
            text(
                "INSERT INTO strategy_preset (strategy, name, params, is_builtin) "
                "VALUES (:s, :name, CAST(:params AS jsonb), true) "
                "ON CONFLICT (strategy, lower(name)) DO UPDATE "
                "SET params = excluded.params, retired_at = NULL, updated_at = now() "
                "WHERE strategy_preset.is_builtin AND ("
                "strategy_preset.params IS DISTINCT FROM excluded.params "
                "OR strategy_preset.retired_at IS NOT NULL) "
                "RETURNING id"
            ),
            {"s": strategy, "name": record.name, "params": json.dumps(record.params)},
        ).first()
        if written is not None:
            continue
        # Nothing returned: either our own unchanged row, or someone else's —
        # read after the upsert, so a row inserted concurrently is seen too.
        is_builtin = session.execute(
            text(
                "SELECT is_builtin FROM strategy_preset "
                "WHERE strategy = :s AND lower(name) = lower(:name)"
            ),
            {"s": strategy, "name": record.name},
        ).scalar_one_or_none()
        if is_builtin is False:
            conflicts.append(
                {"index": record.index, "name": record.name, "type": "name_conflict"}
            )

    # 5. Retire only by an authoritative list: a list with parse errors may
    # have lost a preset to a defect, and hiding it would be wrong.
    if not parsed.has_errors:
        session.execute(
            text(
                "UPDATE strategy_preset SET retired_at = now(), updated_at = now() "
                "WHERE strategy = :s AND is_builtin AND retired_at IS NULL "
                "AND lower(name) <> ALL ("
                "SELECT lower(n) FROM unnest(CAST(:names AS text[])) AS n)"
            ),
            {"s": strategy, "names": [record.name for record in parsed.accepted]},
        )

    # 6. The new state of the sync.
    errors = parsed.errors + conflicts
    status = "partial" if errors else "ok"
    session.execute(
        text(
            "UPDATE strategy_builtin_sync SET content_hash = :key, status = :status, "
            "errors = CAST(:errors AS jsonb), synced_at = now() WHERE strategy = :s"
        ),
        {"s": strategy, "key": key, "status": status, "errors": json.dumps(errors)},
    )
    for error in errors:
        logger.warning("strategy %r: built-in preset not synced: %s", strategy, error)
    return SyncOutcome("applied", status=status, errors=errors)


def _strategy_entry(document: Any, strategy: str) -> dict[str, Any] | None:
    if not isinstance(document, dict):
        raise CatalogError("published catalog must be a JSON object")
    for entry in document.get("strategies") or []:
        if isinstance(entry, dict) and entry.get("name") == strategy:
            return entry
    return None


def _regroup_by_database(session: Session, parsed: PresetsParse) -> PresetsParse:
    """Settle identity groups with PostgreSQL's ``lower()`` (plan §4.2, R83).

    Python's ``str.lower()`` and the index's ``lower()`` disagree on some
    letters (``İ``, final sigma), so two names the parse kept apart may be one
    row for the index; the second upsert would then overwrite the first. Every
    member of a group repeated by the database's identity becomes
    ``duplicate_name``, on top of the parse's own groups.
    """
    named = [record for record in parsed.records if record.name is not None]
    if len(named) < 2:
        return parsed
    result = session.execute(
        text(
            "SELECT i, lower(n) FROM unnest(CAST(:names AS text[])) "
            "WITH ORDINALITY AS t(n, i)"
        ),
        {"names": [record.name for record in named]},
    )
    identities: dict[int, str] = {int(i): identity for i, identity in result.all()}
    counts: dict[str, int] = {}
    for identity in identities.values():
        counts[identity] = counts.get(identity, 0) + 1
    repeated = {
        record.index
        for position, record in enumerate(named, start=1)
        if counts[identities[position]] > 1
    }
    if not repeated:
        return parsed
    return replace(
        parsed,
        records=[
            replace(record, status="duplicate_name")
            if record.index in repeated
            else record
            for record in parsed.records
        ],
    )


__all__ = [
    "PIPELINE_SOURCE_SHA256",
    "PRESETS_PIPELINE_VERSION",
    "REDIS_READ_TIMEOUT_S",
    "SyncOutcome",
    "content_hash",
    "sync_builtin_presets",
]
