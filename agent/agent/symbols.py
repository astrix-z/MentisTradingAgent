"""The tradeable universe, and our verdict on which part of it is usable.

What the exchange actually publishes
------------------------------------
Binance's public API has no equivalent of the marks the website shows
("monitoring", seed tags, volatility warnings): ``exchangeInfo`` has no ``tags``
field, and every one of the 3699 symbols came back with an empty
``additional_properties`` catch-all. Delisting announcements are not in the spot
API either. So a column of *exchange* marks cannot be built from the SDK
(``agent/exchange/rest.py`` is the only exchange adapter, by project rule).

What it does publish is enough to derive the list that was asked for:

* a delisted or halted symbol is not ``status == "TRADING"`` (212 USDT symbols
  are stuck in ``BREAK`` — BCCUSDT, MATICUSDT, XMRUSDT, ...);
* a leveraged token is ``not is_spot_trading_allowed`` (the 40 ``*UP``/``*DOWN``
  symbols);
* the rest is identifiable from the base asset, which is why the tables below
  exist.

Everything here is *our* classification, not the exchange's, and it is one
editable table per rule rather than logic spread through the code. The one
structural rule worth knowing: Binance lists tokenized equities as
``<TICKER>B`` (``AAPLB``, ``TSLAB``, ``QQQB``), so rather than allow-listing
stock tickers — which would let a new listing through — the rule is "ends with
B, unless it is one of the very few crypto assets that legitimately does".
Missing a new equity is worse than the rare false positive, and a false
positive is visible in the published document with its reason.
"""

from __future__ import annotations

import copy
import json
import logging
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

from agent.engine import REDIS_SYMBOLS_KEY
from agent.exchange import ExchangeError, ExchangeRest
from agent.selection import apply_selection, load_selected

logger = logging.getLogger(__name__)

UNIVERSE_VERSION = 1

#: The quote asset the universe is expressed in. The UI trades spot against
#: USDT, and the exchange offers no server-side filter for it.
QUOTE_ASSET = "USDT"

#: The universe is exchange-wide, so it is read from the live endpoint even when
#: the agent trades on testnet: testnet lists a small subset, and choosing from
#: it would hide the symbols the user actually wants to configure.
UNIVERSE_NETWORK = "live"

#: Base assets that are not a crypto asset: cash and its tokens.
STABLE_OR_FIAT = frozenset(
    {
        "AEUR", "ARS", "BFUSD", "BRL", "BUSD", "COP", "CZK", "DAI", "EUR", "EURC",
        "EURI", "FDUSD", "FRAX", "GBP", "IDRT", "JPY", "MXN", "PEN", "PLN", "PYUSD",
        "RLUSD", "RON", "TRY", "TUSD", "UAH", "USD", "USD1", "USDC", "USDE", "USDP",
        "USDS", "USTC", "XUSD", "ZAR",
    }
)

#: Tokenized metal.
GOLD = frozenset({"PAXG", "XAUT"})

#: Wrapped or staked representations of another asset.
WRAPPED_OR_STAKED = frozenset(
    {"BETH", "BNSOL", "BSOL", "STETH", "WBETH", "WBTC", "WETH", "WSTETH"}
)

#: Crypto assets whose name legitimately ends in ``B``. Every one of these was
#: silently mis-classified as an equity by the suffix rule before it was listed
#: here, which is exactly why the rule reports its guesses separately (below)
#: instead of presenting them as facts.
CRYPTO_ENDING_IN_B = frozenset({"ARB", "BB", "BNB", "CKB", "DGB", "TRB"})

#: Stock and ETF tickers we know Binance tokenizes as ``<TICKER>B``. Only used to
#: tell a confident classification from a guess; the rule does not need updating
#: for a new listing.
EQUITY_TICKERS = frozenset(
    {
        "AAPL", "AMAT", "AMD", "AMZN", "ARM", "ASML", "ASTS", "AVGO", "AXTI",
        "BABA", "BE", "BMNR", "COHR", "COIN", "CRCL", "CRDO", "CRM", "CRWD",
        "CRWV", "DELL", "DJT", "EWY", "FLNC", "GLW", "GME", "GOOGL", "GS",
        "HIMS", "HOOD", "IBM", "INTC", "INTU", "IREN", "KORU", "LITE", "META",
        "MRNA", "MRVL", "MSFT", "MSTR", "MU", "NBIS", "NFLX", "NOK", "NVDA",
        "ORCL", "PLTR", "PYPL", "QCOM", "QQQ", "RKLB", "SKHY", "SMCI", "SMH",
        "SNDK", "SOXL", "SOXS", "SPCX", "SPY", "SQQQ", "STX", "TQQQ", "TSLA",
        "TSM", "WDC",
    }
)

#: Memecoins have no structural signal at all — DOGE, SHIB and PEPE are ordinary
#: spot symbols. This label is therefore *informational*: the curated list is
#: seeded from the 491 USDT symbols listed on 2026-09-15, it is expected to be
#: edited, and a memecoin missing from it is simply reported as crypto. Memecoins
#: are deliberately **not** filtered out of the universe: the working-symbols
#: page leaves them unchecked by hand instead.
MEMECOINS = frozenset(
    {
        "1000CAT", "1000CHEEMS", "1000SATS", "1MBABYDOGE", "BANANAS31", "BOME",
        "BONK", "BROCCOLI714", "DOGE", "DOGS", "FLOKI", "GIGGLE", "HMSTR",
        "MARSCOIN", "MUBARAK", "NEIRO", "PENGU", "PEPE", "PNUT", "SHIB", "TRUMP",
        "TURBO", "TUT", "WIF",
    }
)

#: Classifications that keep a symbol out of the universe. Memecoins are absent
#: on purpose (see above).
EXCLUDED_CLASSES = frozenset(
    {
        "stablecoin_or_fiat",
        "gold",
        "wrapped_or_staked",
        "tokenized_equity",
        "suspected_tokenized_equity",
    }
)

#: Labels, so the UI never has to know the sets above.
CRYPTO = "crypto"
CLASSIFICATIONS = {
    "stablecoin_or_fiat": "stablecoin or fiat",
    "gold": "gold-backed",
    "wrapped_or_staked": "wrapped or staked",
    "tokenized_equity": "tokenized equity",
    "suspected_tokenized_equity": "tokenized equity (name pattern, please review)",
    "memecoin": "memecoin",
}


def classify(base_asset: str | None) -> str:
    """Our classification of a base asset, or ``"crypto"`` for the rest.

    A name ending in ``B`` is only *evidence* of a tokenized equity, so a known
    ticker is reported as fact and everything else that merely matches the
    pattern is reported as a guess: silently dropping a coin that happens to
    end in B is worse than showing a reviewer one line too many.
    """
    asset = (base_asset or "").upper()
    if not asset:
        return CRYPTO
    if asset in STABLE_OR_FIAT:
        return "stablecoin_or_fiat"
    if asset in GOLD:
        return "gold"
    if asset in WRAPPED_OR_STAKED:
        return "wrapped_or_staked"
    if asset in MEMECOINS:
        return "memecoin"
    if asset.endswith("B") and asset not in CRYPTO_ENDING_IN_B:
        return (
            "tokenized_equity"
            if asset[:-1] in EQUITY_TICKERS
            else "suspected_tokenized_equity"
        )
    return CRYPTO


def _has_non_latin_name(asset: str) -> bool:
    """True for a base asset written in a non-Latin script.

    Binance lists a few symbols named in Chinese (币安人生, 牛来). They are hard to
    read and easy to mistake for one another, so the universe keeps them out.
    """
    return any(ord(char) > 0x7F for char in asset)


def _volume(value: Any) -> str:
    """24h quote volume as a Decimal string, never a binary float."""
    try:
        return str(Decimal(str(value))) if value not in (None, "") else "0"
    except (InvalidOperation, TypeError, ValueError):
        return "0"


def build_universe(
    symbols: list[Any],
    tickers: dict[str, Any],
    *,
    quote_asset: str = QUOTE_ASSET,
) -> dict[str, Any]:
    """The document the UI renders, one entry per quoted symbol.

    Every symbol is listed with our classification and, when it is not usable,
    the reason — the UI decides whether to hide those or show them greyed out.
    """
    entries: list[dict[str, Any]] = []
    for symbol in symbols:
        if symbol.quote_asset != quote_asset or not symbol.symbol:
            continue
        classification = classify(symbol.base_asset)
        spot_allowed = bool(symbol.is_spot_trading_allowed)
        if symbol.status != "TRADING":
            reason = "trading halted or delisted"
        elif not spot_allowed:
            reason = "spot trading not allowed"
        elif _has_non_latin_name(symbol.base_asset or ""):
            reason = "non-latin name"
        elif classification in EXCLUDED_CLASSES:
            reason = CLASSIFICATIONS[classification]
        else:
            reason = None
        ticker = tickers.get(symbol.symbol)
        entries.append(
            {
                "symbol": symbol.symbol,
                "base_asset": symbol.base_asset,
                "quote_asset": symbol.quote_asset,
                "status": symbol.status,
                "spot_allowed": spot_allowed,
                "quote_volume_24h": _volume(getattr(ticker, "quote_volume", None)),
                "classification": classification,
                "classification_label":
                    None if classification == CRYPTO else CLASSIFICATIONS[classification],
                "excluded_reason": reason,
            }
        )
    entries.sort(key=lambda entry: entry["symbol"])
    return {
        "version": UNIVERSE_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "network": UNIVERSE_NETWORK,
        "quote_asset": quote_asset,
        "usable": sum(1 for entry in entries if entry["excluded_reason"] is None),
        "total": len(entries),
        "symbols": entries,
    }


#: The last universe read from the exchange, kept so a command can validate a
#: selection without two more HTTP calls. Selection flags are *not* stored here:
#: they are merged into a copy at publish time.
_LAST_UNIVERSE: dict[str, Any] | None = None


def latest_universe() -> dict[str, Any] | None:
    """The last universe fetched from the exchange, or ``None`` if never."""
    return _LAST_UNIVERSE


async def fetch_universe(network: str = UNIVERSE_NETWORK) -> dict[str, Any]:
    """Read the universe from the exchange and classify it."""
    global _LAST_UNIVERSE
    rest = ExchangeRest.connect_public(network)  # type: ignore[arg-type]
    symbols = await rest.symbols()
    tickers = await rest.tickers_24h()
    _LAST_UNIVERSE = build_universe(symbols, tickers)
    return _LAST_UNIVERSE


async def publish_universe(
    redis: Redis, *, document: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Publish the universe, merged with the symbols the user marked.

    Pass ``document`` to republish a known universe (a selection change) without
    asking the exchange again. A failed refresh keeps the previously published
    document, and never takes the agent down.
    """
    if document is None:
        try:
            document = await fetch_universe()
        except ExchangeError as exc:
            logger.warning("cannot refresh the symbol universe: %s", exc)
            return None
    merged = apply_selection(copy.deepcopy(document), await load_selected())
    payload = json.dumps(merged)
    try:
        await redis.set(REDIS_SYMBOLS_KEY, payload)
    except RedisError:
        logger.warning("cannot publish the symbol universe (Redis unavailable)")
        return None
    logger.info(
        "published symbol universe: %s usable of %s %s symbols, %s marked (%s unusable)",
        merged["usable"],
        merged["total"],
        merged["quote_asset"],
        merged["selected"],
        merged["selected_unusable"],
    )
    return merged


__all__ = [
    "CRYPTO",
    "MEMECOINS",
    "QUOTE_ASSET",
    "UNIVERSE_VERSION",
    "build_universe",
    "classify",
    "fetch_universe",
    "latest_universe",
    "publish_universe",
]
