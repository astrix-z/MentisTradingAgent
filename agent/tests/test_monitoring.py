"""The exchange's Monitoring tag: reading it, storing it, and how it fails.

The feed behind this is unofficial and scraped (see ``agent/monitoring.py``), so
the tests are mostly about failure: a broken feed must never be read as "nothing
is monitored", and nothing about the tag may affect trading.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from agent.main import _monitoring_loop
from agent.monitoring import (
    MONITORING_TAG,
    MonitoringError,
    MonitoringFeed,
    MonitoringMarks,
    apply_monitoring,
    load_monitoring,
    parse_monitoring,
    refresh_monitoring,
    save_monitoring,
)


def product(symbol: str, *tags: str) -> dict[str, Any]:
    return {"s": symbol, "tags": list(tags)}


def feed_payload(*products: dict[str, Any]) -> dict[str, Any]:
    """A feed response shaped like the real one, with only the fields we read."""
    return {"code": "000000", "success": True, "data": list(products)}


def populated_feed() -> dict[str, Any]:
    return feed_payload(
        product("BTCUSDT", "Layer1_Layer2"),
        product("SYNUSDT", MONITORING_TAG, "innovation-zone", "Seed"),
        product("WIFUSDT", MONITORING_TAG),
    )


def reading(*symbols: str) -> MonitoringMarks:
    return MonitoringMarks(
        symbols=frozenset(symbols),
        refreshed_at=datetime(2026, 9, 15, 12, 0, tzinfo=UTC),
    )


# --- reading the feed -------------------------------------------------------


def test_only_the_tagged_symbols_are_read() -> None:
    assert parse_monitoring(populated_feed()) == frozenset({"SYNUSDT", "WIFUSDT"})


@pytest.mark.parametrize(
    "broken",
    [
        ["not", "an", "object"],
        {},
        {"data": "not a list"},
        # A valid response that tags nothing: the case that must not be believed.
        feed_payload(product("BTCUSDT", "Layer1_Layer2")),
        feed_payload(),
    ],
)
def test_a_payload_that_tags_nothing_is_an_error(broken: Any) -> None:
    """'I could not read it' must never be mistaken for 'nothing is monitored'."""
    with pytest.raises(MonitoringError):
        parse_monitoring(broken)


def test_the_feed_reads_through_an_injectable_transport() -> None:
    """The seam that keeps this adapter replaceable: no network, no patching."""

    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.host == "www.binance.com"
            return httpx.Response(200, json=populated_feed())

        feed = MonitoringFeed(transport=httpx.MockTransport(handler))
        assert await feed.fetch() == frozenset({"SYNUSDT", "WIFUSDT"})

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "response",
    [
        # What the real page answers a plain client: a WAF challenge, not data.
        httpx.Response(202, text="<html>window.awsWafCookie</html>"),
        httpx.Response(503, text="<html>maintenance</html>"),
        httpx.Response(200, text="<html>the page, not the feed</html>"),
    ],
)
def test_an_unusable_response_raises(response: httpx.Response) -> None:
    async def scenario() -> None:
        feed = MonitoringFeed(transport=httpx.MockTransport(lambda _: response))
        with pytest.raises(MonitoringError):
            await feed.fetch()

    asyncio.run(scenario())


def test_a_transport_failure_raises() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route to host")

        feed = MonitoringFeed(transport=httpx.MockTransport(handler))
        with pytest.raises(MonitoringError):
            await feed.fetch()

    asyncio.run(scenario())


# --- storing what was read --------------------------------------------------


def test_reading_without_a_database_knows_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agent.core.db.settings_engine", lambda: None)
    marks = asyncio.run(load_monitoring())
    assert marks.symbols == frozenset()
    assert marks.refreshed_at is None


def test_an_empty_list_is_refused_before_it_reaches_the_database() -> None:
    with pytest.raises(MonitoringError):
        asyncio.run(save_monitoring([]))


def test_writing_without_a_database_fails_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agent.core.db.settings_engine", lambda: None)
    with pytest.raises(RuntimeError):
        asyncio.run(save_monitoring(["SYNUSDT"]))


def test_a_failed_read_never_replaces_the_stored_marks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mistake that would matter: a broken feed must not clear the list."""
    stored = AsyncMock()
    monkeypatch.setattr("agent.monitoring.save_monitoring", stored)

    class Broken(MonitoringFeed):
        async def fetch(self) -> frozenset[str]:
            raise MonitoringError("feed changed shape")

    assert asyncio.run(refresh_monitoring(feed=Broken())) is None
    stored.assert_not_awaited()


def test_a_successful_read_is_stored(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        saved = reading("SYNUSDT", "WIFUSDT")
        save = AsyncMock(return_value=saved)
        monkeypatch.setattr("agent.monitoring.save_monitoring", save)
        feed = MonitoringFeed(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json=populated_feed())
            )
        )
        assert await refresh_monitoring(feed=feed) == saved
        # What reached storage is what the feed said, not what the caller guessed.
        forwarded = save.await_args
        assert forwarded is not None
        assert forwarded.args[0] == frozenset({"SYNUSDT", "WIFUSDT"})

    asyncio.run(scenario())


# --- what the document says -------------------------------------------------


def test_applying_the_marks_labels_entries_and_records_when_they_were_read() -> None:
    document = {"symbols": [{"symbol": "BTCUSDT"}, {"symbol": "SYNUSDT"}]}
    marked = apply_monitoring(document, reading("SYNUSDT"))

    assert [entry["monitoring"] for entry in marked["symbols"]] == [False, True]
    assert marked["monitoring_updated_at"] == "2026-09-15T12:00:00+00:00"


def test_applying_nothing_read_says_so_rather_than_looking_fresh() -> None:
    document = {"symbols": [{"symbol": "SYNUSDT"}]}
    marked = apply_monitoring(document, MonitoringMarks())

    assert marked["symbols"][0]["monitoring"] is False
    assert marked["monitoring_updated_at"] is None


def test_a_tagged_symbol_the_universe_does_not_list_is_not_invented() -> None:
    """Unlike a working-symbol mark, a tag does not earn a row of its own."""
    document = {"symbols": [{"symbol": "BTCUSDT"}]}
    marked = apply_monitoring(document, reading("GONEUSDT"))

    assert [entry["symbol"] for entry in marked["symbols"]] == ["BTCUSDT"]


# --- the daily loop ---------------------------------------------------------


def run_loop_once(refreshed: MonitoringMarks | None) -> tuple[AsyncMock, AsyncMock]:
    """Run the loop for a few ticks with a stubbed refresh, and report the calls."""

    async def scenario() -> tuple[AsyncMock, AsyncMock]:
        stop = asyncio.Event()
        refresh = AsyncMock(return_value=refreshed)
        publish = AsyncMock()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr("agent.main.MONITORING_REFRESH_SECONDS", 0.01)
            patch.setattr("agent.main.refresh_monitoring", refresh)
            patch.setattr("agent.main.publish_universe", publish)
            loop = asyncio.create_task(_monitoring_loop(Mock(), stop))
            try:
                await asyncio.sleep(0.05)
            finally:
                stop.set()
                await asyncio.wait_for(loop, 1)
        return refresh, publish

    return asyncio.run(scenario())


def test_the_loop_republishes_so_a_new_label_shows_up_at_once() -> None:
    refresh, publish = run_loop_once(reading("SYNUSDT"))
    assert refresh.await_count >= 1
    assert publish.await_count >= 1


def test_the_loop_publishes_nothing_when_the_read_failed() -> None:
    """The published document keeps the marks it had, and says when it got them."""
    refresh, publish = run_loop_once(None)
    assert refresh.await_count >= 1
    publish.assert_not_awaited()
