"""Classification of the tradeable universe.

The traps here are real symbols taken from the live exchange on 2026-09-15, not
invented ones: crypto assets whose names end in ``B`` (BB, ARB, BNB) must not be
mistaken for the tokenized equities that share that suffix (AAPLB, TSLAB), and
the leveraged tokens must be rejected for not being spot-tradable rather than
for their name.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from binance_sdk_spot.rest_api.models import (
    ExchangeInfoResponseSymbolsInner,
    Ticker24hrResponse2Inner,
)
from redis.exceptions import RedisError

from agent.engine import REDIS_SYMBOLS_KEY
from agent.exchange import ExchangeError
from agent.symbols import (
    QUOTE_ASSET,
    UNIVERSE_VERSION,
    build_universe,
    classify,
    publish_universe,
)


def symbol(name: str, **overrides: Any) -> ExchangeInfoResponseSymbolsInner:
    """An exchange-info row, built the way the SDK builds it from the wire."""
    base = name[: -len(QUOTE_ASSET)]
    payload: dict[str, Any] = {
        "symbol": name,
        "status": "TRADING",
        "baseAsset": base,
        "quoteAsset": QUOTE_ASSET,
        "isSpotTradingAllowed": True,
        "isMarginTradingAllowed": True,
    }
    payload.update(overrides)
    return ExchangeInfoResponseSymbolsInner.from_dict(payload)


def ticker(name: str, volume: str) -> Ticker24hrResponse2Inner:
    return Ticker24hrResponse2Inner.from_dict({"symbol": name, "quoteVolume": volume})


# --- classify ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("asset", "expected"),
    [
        # Ordinary crypto, including names that merely look suspicious.
        ("BTC", "crypto"),
        ("ETH", "crypto"),
        ("SOL", "crypto"),
        ("BB", "crypto"),  # ends with B, but is BounceBit
        ("ARB", "crypto"),  # Arbitrum
        ("BNB", "crypto"),  # not a tokenized "BN"
        ("CKB", "crypto"),  # Nervos — caught by the suffix rule until allow-listed
        ("DGB", "crypto"),  # DigiByte
        ("TRB", "crypto"),  # Tellor
        ("", "crypto"),
        # The suffix rule.
        ("AAPLB", "tokenized_equity"),
        ("TSLAB", "tokenized_equity"),
        ("QQQB", "tokenized_equity"),
        ("SPYB", "tokenized_equity"),
        # Cash and its tokens.
        ("USDC", "stablecoin_or_fiat"),
        ("FDUSD", "stablecoin_or_fiat"),
        ("EUR", "stablecoin_or_fiat"),
        ("BFUSD", "stablecoin_or_fiat"),
        # Tokenized metal.
        ("PAXG", "gold"),
        ("XAUT", "gold"),
        # Wrapped/staked.
        ("WBTC", "wrapped_or_staked"),
        ("WBETH", "wrapped_or_staked"),
        ("BNSOL", "wrapped_or_staked"),
        # Curated, and only a label: memecoins stay selectable.
        ("DOGE", "memecoin"),
        ("SHIB", "memecoin"),
        ("PEPE", "memecoin"),
    ],
)
def test_classify(asset: str, expected: str) -> None:
    assert classify(asset) == expected


def test_a_new_equity_listing_is_caught_by_the_suffix_rule() -> None:
    """The point of the rule: no ticker list to keep up to date.

    It reports a guess as a guess, because the same pattern also matches coins
    like CKB, DGB and TRB.
    """
    assert classify("MSFTB") == "tokenized_equity"
    assert classify("SOMENEWTICKERB") == "suspected_tokenized_equity"


def test_a_suspected_equity_says_so_in_the_published_document() -> None:
    document = build_universe([symbol("SOMENEWTICKERBUSDT")], {})
    entry = document["symbols"][0]
    assert entry["classification"] == "suspected_tokenized_equity"
    assert entry["excluded_reason"] == "tokenized equity (name pattern, please review)"
    assert document["usable"] == 0


# --- build_universe ---------------------------------------------------------


def test_universe_keeps_only_quoted_symbols_and_labels_each_one() -> None:
    document = build_universe(
        [
            symbol("BTCUSDT"),
            symbol("ETHBTC", quoteAsset="BTC"),
            symbol("USDCUSDT"),
            symbol("AAPLBUSDT"),
            symbol("PAXGUSDT"),
            symbol("WBTCUSDT"),
            symbol("DOGEUSDT"),
            symbol("币安人生USDT"),
            symbol("BTCUPUSDT", isSpotTradingAllowed=False),
            symbol("MATICUSDT", status="BREAK"),
        ],
        {"BTCUSDT": ticker("BTCUSDT", "1234.50000000")},
    )
    reasons = {entry["symbol"]: entry["excluded_reason"] for entry in document["symbols"]}
    assert reasons == {
        "AAPLBUSDT": "tokenized equity",
        "BTCUPUSDT": "spot trading not allowed",
        "BTCUSDT": None,
        "DOGEUSDT": None,  # a memecoin, but it is the user who leaves it unchecked
        "MATICUSDT": "trading halted or delisted",
        "PAXGUSDT": "gold-backed",
        "USDCUSDT": "stablecoin or fiat",
        "WBTCUSDT": "wrapped or staked",
        "币安人生USDT": "non-latin name",
    }
    # A symbol quoted in something else is not part of this universe at all.
    assert "ETHBTC" not in reasons


def test_a_memecoin_is_usable_but_still_labelled() -> None:
    """Filtering is not the same as labelling: the UI shows the mark, the user
    decides. A non-Latin name is the one name-based exclusion."""
    document = build_universe([symbol("DOGEUSDT"), symbol("币安人生USDT")], {})
    by_symbol = {entry["symbol"]: entry for entry in document["symbols"]}
    assert by_symbol["DOGEUSDT"]["classification"] == "memecoin"
    assert by_symbol["DOGEUSDT"]["classification_label"] == "memecoin"
    assert by_symbol["DOGEUSDT"]["excluded_reason"] is None
    assert by_symbol["币安人生USDT"]["excluded_reason"] == "non-latin name"
    assert document["usable"] == 1


def test_universe_reports_volume_as_a_decimal_string() -> None:
    document = build_universe(
        [symbol("BTCUSDT"), symbol("ETHUSDT")],
        {"BTCUSDT": ticker("BTCUSDT", "1234.50000000")},
    )
    by_symbol = {entry["symbol"]: entry for entry in document["symbols"]}
    assert by_symbol["BTCUSDT"]["quote_volume_24h"] == "1234.50000000"
    # Unknown or unusable volume degrades to zero rather than to a crash.
    assert by_symbol["ETHUSDT"]["quote_volume_24h"] == "0"


def test_universe_keeps_the_exchange_status_verbatim() -> None:
    document = build_universe(
        [symbol("MATICUSDT", status="BREAK"), symbol("BTCUSDT")], {}
    )
    by_symbol = {entry["symbol"]: entry for entry in document["symbols"]}
    assert by_symbol["MATICUSDT"]["status"] == "BREAK"
    assert by_symbol["MATICUSDT"]["spot_allowed"] is True
    assert by_symbol["BTCUSDT"]["status"] == "TRADING"


def test_universe_counts_what_is_usable_and_sorts_it() -> None:
    document = build_universe(
        [symbol("WBTCUSDT"), symbol("BTCUSDT"), symbol("MATICUSDT", status="BREAK")], {}
    )
    assert document["version"] == UNIVERSE_VERSION
    assert document["quote_asset"] == QUOTE_ASSET
    assert document["total"] == 3
    assert document["usable"] == 1
    assert [entry["symbol"] for entry in document["symbols"]] == [
        "BTCUSDT",
        "MATICUSDT",
        "WBTCUSDT",
    ]
    assert document["generated_at"]


def test_a_symbol_without_a_name_is_skipped() -> None:
    document = build_universe([symbol("BTCUSDT", symbol=None)], {})
    assert document["symbols"] == []


# --- publishing -------------------------------------------------------------


def test_publish_writes_the_universe_to_its_key(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        document = build_universe([symbol("BTCUSDT")], {})
        monkeypatch.setattr(
            "agent.symbols.fetch_universe", AsyncMock(return_value=document)
        )
        redis = Mock(set=AsyncMock())
        published = await publish_universe(redis)
        assert published is not None
        key, payload = redis.set.await_args.args
        assert key == REDIS_SYMBOLS_KEY
        assert json.loads(payload)["symbols"][0]["symbol"] == "BTCUSDT"

    asyncio.run(scenario())


def test_publish_keeps_the_previous_document_when_the_exchange_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exchange hiccup must not empty the page."""

    async def scenario() -> None:
        monkeypatch.setattr(
            "agent.symbols.fetch_universe",
            AsyncMock(side_effect=ExchangeError("exchange down")),
        )
        redis = Mock(set=AsyncMock())
        assert await publish_universe(redis) is None
        redis.set.assert_not_awaited()

    asyncio.run(scenario())


def test_publish_survives_redis_being_down(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(
            "agent.symbols.fetch_universe",
            AsyncMock(return_value=build_universe([symbol("BTCUSDT")], {})),
        )
        redis = Mock(set=AsyncMock(side_effect=RedisError("down")))
        assert await publish_universe(redis) is None

    asyncio.run(scenario())
