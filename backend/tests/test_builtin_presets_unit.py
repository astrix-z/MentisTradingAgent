"""PRESET-05b without a database: the idempotency key and the version guard."""

import copy
import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.core import builtin_presets, strategy_catalog
from app.core.builtin_presets import (
    PIPELINE_SOURCE_SHA256,
    PRESETS_PIPELINE_VERSION,
    content_hash,
)


def test_the_pipeline_version_follows_the_parse_and_pipeline_source() -> None:
    """Plan §4.3.2: a rule change without a version bump would keep old
    ``rejected`` records forever, since an unchanged key is never re-parsed.

    If this fails, ``strategy_catalog.py`` changed: raise
    ``PRESETS_PIPELINE_VERSION`` and add its hash as a new entry.
    """
    source = Path(strategy_catalog.__file__).read_bytes()
    assert sorted(PIPELINE_SOURCE_SHA256) == list(
        range(1, PRESETS_PIPELINE_VERSION + 1)
    )
    assert PIPELINE_SOURCE_SHA256[PRESETS_PIPELINE_VERSION] == (
        hashlib.sha256(source).hexdigest()
    )
    assert len(set(PIPELINE_SOURCE_SHA256.values())) == len(PIPELINE_SOURCE_SHA256)


DOCUMENT: dict[str, Any] = {
    "version": 1,
    "schema_hash": "h1",
    "published_at": "2026-09-26T00:00:00+00:00",
    "lookups": {"intervals_ms": {"1h": 3_600_000}},
    "strategies": [
        {
            "name": "s",
            "label": "S",
            "settings_schema": {"type": "object", "properties": {"a": {}}},
            "constraints": [],
            "legacy_keys": {},
            "presets": [{"name": "P", "params": {"a": 1}}],
        }
    ],
}


def key_of(document: dict[str, Any]) -> str:
    return content_hash(document, document["strategies"][0])


def test_publication_metadata_is_not_part_of_the_key() -> None:
    other = copy.deepcopy(DOCUMENT)
    other["published_at"] = "2020-01-01T00:00:00+00:00"
    other["schema_hash"] = "h2"
    other["strategies"][0]["label"] = "Renamed"
    assert key_of(other) == key_of(DOCUMENT)


def test_every_parse_input_is_part_of_the_key() -> None:
    changes: list[Callable[[dict[str, Any]], None]] = [
        lambda d: d["strategies"][0]["presets"][0]["params"].update(a=2),
        lambda d: d["strategies"][0]["settings_schema"]["properties"].update(b={}),
        lambda d: d["strategies"][0]["constraints"].append({"cmp": "gt"}),
        lambda d: d["strategies"][0]["legacy_keys"].update(old="a"),
        lambda d: d["lookups"]["intervals_ms"].update({"4h": 14_400_000}),
    ]
    for change in changes:
        other = copy.deepcopy(DOCUMENT)
        change(other)
        assert key_of(other) != key_of(DOCUMENT)


def test_the_pipeline_version_is_part_of_the_key(monkeypatch: Any) -> None:
    before = key_of(DOCUMENT)
    monkeypatch.setattr(
        builtin_presets, "PRESETS_PIPELINE_VERSION", PRESETS_PIPELINE_VERSION + 1
    )
    assert key_of(DOCUMENT) != before


def test_the_key_ignores_key_order() -> None:
    reordered = copy.deepcopy(DOCUMENT)
    entry = reordered["strategies"][0]
    reordered["strategies"][0] = dict(reversed(list(entry.items())))
    assert key_of(reordered) == key_of(DOCUMENT)
