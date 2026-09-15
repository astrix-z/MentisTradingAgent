"""Marking working symbols: validation, persistence and publishing.

The persistence layer is exercised against the real database in the container
run (``docker compose exec -T agent python -`` with a live selection); here the
table is stubbed out and the focus is the logic — what counts as a usable
symbol, what the published document says about a mark that stopped qualifying,
and what the command reports back.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from agent.common import commands
from agent.selection import (
    NO_LONGER_LISTED,
    apply_selection,
    load_selected,
    save_selected,
    validate_selection,
)
from agent.symbols import build_universe, publish_universe


def document(*entries: dict[str, Any]) -> dict[str, Any]:
    """A published-universe-shaped document with the given entries."""
    return {
        "version": 1,
        "quote_asset": "USDT",
        "usable": sum(1 for e in entries if e.get("excluded_reason") is None),
        "total": len(entries),
        "symbols": list(entries),
    }


def entry(symbol: str, reason: str | None = None) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "base_asset": symbol.removesuffix("USDT"),
        "quote_asset": "USDT",
        "status": "TRADING",
        "spot_allowed": True,
        "quote_volume_24h": "1",
        "classification": "crypto",
        "classification_label": None,
        "excluded_reason": reason,
    }


# --- validate_selection -----------------------------------------------------


def test_only_usable_symbols_are_accepted() -> None:
    published = document(
        entry("BTCUSDT"),
        entry("DOGEUSDT"),
        entry("AAPLBUSDT", "tokenized equity"),
        entry("MATICUSDT", "trading halted or delisted"),
    )
    assert validate_selection(["BTCUSDT", "DOGEUSDT"], published) == []
    assert validate_selection(["AAPLBUSDT", "MATICUSDT"], published) == [
        "AAPLBUSDT",
        "MATICUSDT",
    ]
    # Unknown symbols are rejected too, and the result is normalised and sorted.
    assert validate_selection([" zzzusdt ", "btcusdt"], published) == ["ZZZUSDT"]


# --- apply_selection --------------------------------------------------------


def test_selection_flags_every_entry() -> None:
    published = document(entry("BTCUSDT"), entry("ETHUSDT"))
    merged = apply_selection(published, {"BTCUSDT"})
    flags = {e["symbol"]: e["selected"] for e in merged["symbols"]}
    assert flags == {"BTCUSDT": True, "ETHUSDT": False}
    assert merged["selected"] == 1
    assert merged["selected_unusable"] == 0


def test_a_marked_symbol_that_vanished_is_reported_not_dropped() -> None:
    """The user must see that a symbol they chose is gone, not lose it silently."""
    merged = apply_selection(document(entry("BTCUSDT")), {"BTCUSDT", "GONEUSDT"})
    by_symbol = {e["symbol"]: e for e in merged["symbols"]}
    assert by_symbol["GONEUSDT"]["selected"] is True
    assert by_symbol["GONEUSDT"]["excluded_reason"] == NO_LONGER_LISTED
    assert by_symbol["GONEUSDT"]["status"] == "MISSING"
    assert merged["selected"] == 2
    assert merged["selected_unusable"] == 1


def test_a_mark_that_stopped_qualifying_is_counted_as_unusable() -> None:
    published = document(entry("DOGEUSDT"), entry("AAPLBUSDT", "tokenized equity"))
    merged = apply_selection(published, {"DOGEUSDT", "AAPLBUSDT"})
    assert merged["selected"] == 2
    assert merged["selected_unusable"] == 1


# --- persistence degrades without a database --------------------------------


def test_reading_marks_without_a_database_yields_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agent.core.db.settings_engine", lambda: None)
    assert asyncio.run(load_selected()) == set()


def test_writing_marks_without_a_database_fails_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller must be able to report failure instead of claiming success."""
    monkeypatch.setattr("agent.core.db.settings_engine", lambda: None)
    with pytest.raises(RuntimeError):
        asyncio.run(save_selected(["BTCUSDT"]))


# --- publishing merges the marks --------------------------------------------


def test_publishing_merges_the_marks(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(
            "agent.symbols.load_selected", AsyncMock(return_value={"BTCUSDT"})
        )
        redis = Mock(set=AsyncMock())
        published = await publish_universe(
            redis, document=document(entry("BTCUSDT"), entry("ETHUSDT"))
        )
        assert published is not None
        payload = json.loads(redis.set.await_args.args[1])
        assert payload["selected"] == 1
        assert {e["symbol"]: e["selected"] for e in payload["symbols"]} == {
            "BTCUSDT": True,
            "ETHUSDT": False,
        }

    asyncio.run(scenario())


def test_publishing_does_not_mutate_the_universe_it_was_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cached universe is reused by validation, so marks must not leak in."""

    async def scenario() -> None:
        monkeypatch.setattr(
            "agent.symbols.load_selected", AsyncMock(return_value={"BTCUSDT"})
        )
        source = document(entry("BTCUSDT"))
        await publish_universe(Mock(set=AsyncMock()), document=source)
        assert "selected" not in source["symbols"][0]
        assert "selected" not in source

    asyncio.run(scenario())


# --- the command ------------------------------------------------------------


def redis_mock() -> tuple[Mock, Mock]:
    """A Redis double whose pipeline captures what a handler reports."""
    pipe = Mock()
    pipe.execute = AsyncMock()
    context = Mock()
    context.__aenter__ = AsyncMock(return_value=pipe)
    context.__aexit__ = AsyncMock(return_value=False)
    redis = Mock()
    redis.hget = AsyncMock(return_value=None)  # no terminal state yet
    redis.pipeline = Mock(return_value=context)
    redis.xack = AsyncMock()
    return redis, pipe


def reported(pipe: Mock) -> dict[str, Any]:
    """The command result a handler published, as parsed JSON."""
    for call in reversed(pipe.set.call_args_list):
        key, payload = call.args
        if key == "agent:result":
            return json.loads(payload)
    raise AssertionError("no command result was reported")


def envelope(payload: dict[str, Any], type_: str = "symbols") -> str:
    return json.dumps(
        {
            "id": str(uuid.uuid4()),
            "type": type_,
            "payload": payload,
            "issued_at": datetime.now(UTC).isoformat(),
        }
    )


def run_command(raw: str, redis: Mock) -> None:
    asyncio.run(commands.dispatch(Mock(), redis, raw))


def test_the_symbols_command_stores_and_republishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis, pipe = redis_mock()
    monkeypatch.setattr(
        commands, "latest_universe", lambda: document(entry("BTCUSDT"), entry("ETHUSDT"))
    )
    save = AsyncMock(return_value=["BTCUSDT", "ETHUSDT"])
    monkeypatch.setattr(commands, "save_selected", save)
    republish = AsyncMock()
    monkeypatch.setattr(commands, "publish_universe", republish)

    run_command(envelope({"symbols": ["ethusdt", "BTCUSDT", "btcusdt"]}), redis)

    result = reported(pipe)
    assert result["ok"] is True
    assert result["data"]["symbols"] == ["BTCUSDT", "ETHUSDT"]
    save.assert_awaited_once_with(["BTCUSDT", "ETHUSDT"])  # normalised, deduplicated
    republish.assert_awaited_once()


def test_the_symbols_command_rejects_what_the_exchange_disallows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The agent validates: a marked symbol has to still qualify."""
    redis, pipe = redis_mock()
    monkeypatch.setattr(
        commands,
        "latest_universe",
        lambda: document(entry("BTCUSDT"), entry("AAPLBUSDT", "tokenized equity")),
    )
    save = AsyncMock()
    monkeypatch.setattr(commands, "save_selected", save)

    run_command(envelope({"symbols": ["BTCUSDT", "AAPLBUSDT"]}), redis)

    result = reported(pipe)
    assert result["ok"] is False
    assert "AAPLBUSDT" in result["message"]
    save.assert_not_awaited()  # nothing is stored when part of it is rejected


def test_the_symbols_command_rejects_a_malformed_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis, pipe = redis_mock()
    monkeypatch.setattr(commands, "latest_universe", lambda: document())
    run_command(envelope({"symbols": "BTCUSDT"}), redis)
    result = reported(pipe)
    assert result["ok"] is False
    assert result["message"] == "symbols must be a list"


def test_the_symbols_command_clears_the_selection_with_an_empty_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis, pipe = redis_mock()
    monkeypatch.setattr(commands, "latest_universe", lambda: document(entry("BTCUSDT")))
    save = AsyncMock(return_value=[])
    monkeypatch.setattr(commands, "save_selected", save)
    monkeypatch.setattr(commands, "publish_universe", AsyncMock())

    run_command(envelope({"symbols": []}), redis)

    assert reported(pipe)["ok"] is True
    save.assert_awaited_once_with([])


def test_the_symbols_command_reports_a_storage_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis, pipe = redis_mock()
    monkeypatch.setattr(commands, "latest_universe", lambda: document(entry("BTCUSDT")))
    monkeypatch.setattr(
        commands, "save_selected", AsyncMock(side_effect=RuntimeError("no database"))
    )
    monkeypatch.setattr(commands, "publish_universe", AsyncMock())

    run_command(envelope({"symbols": ["BTCUSDT"]}), redis)

    result = reported(pipe)
    assert result["ok"] is False
    assert "no database" in result["message"]


def test_the_command_is_registered() -> None:
    assert "symbols" in commands.HANDLERS


def test_the_universe_builder_is_what_validation_reads() -> None:
    """Guard against the two drifting apart: validation reads what is published."""
    published = build_universe(
        [
            type(
                "S",
                (),
                {
                    "symbol": "BTCUSDT",
                    "base_asset": "BTC",
                    "quote_asset": "USDT",
                    "status": "TRADING",
                    "is_spot_trading_allowed": True,
                },
            )()
        ],
        {},
    )
    assert validate_selection(["BTCUSDT"], published) == []
