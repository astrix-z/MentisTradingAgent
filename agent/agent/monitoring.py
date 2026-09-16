"""The exchange's "Monitoring" tag: a label read from a page that has no API.

Binance marks some listed assets with a Monitoring Tag. They trade normally, but
the exchange reviews them periodically and may delist them, so the mark is worth
knowing about — and it is not a reason to refuse a symbol.

The page a human opens is ``https://www.binance.com/en/markets/coinInfo-Monitoring``.
It does not answer a plain HTTP client: it sits behind an AWS WAF challenge, so a
credential-less request gets ``202`` and a JavaScript cookie page instead of HTML.
What that page itself renders from is a JSON feed, and *that* does answer, which
is what this module reads.

**This adapter is a stand-in, and it is written to look like one.** Binance
publishes no supported endpoint for the tag and nothing in ``binance_sdk_spot``
exposes it, so there is no correct adapter to write — only an unofficial one to
isolate. So it is kept behind a single narrow seam (:class:`MonitoringFeed`, one
``fetch`` method, an injectable transport), it names the page it stands in for,
and it can be replaced or deleted without touching anything else. It is also a
deliberate exception to the rule that exchange access goes through
``binance_sdk_spot``: this is a website feed, not the exchange API, and it is the
only such exception in the agent.

Failure mode, on purpose:

* every failure — HTTP error, timeout, non-JSON body, unexpected shape — raises
  :class:`MonitoringError`, and the caller keeps the list it already had. A failed
  read must never turn a monitored symbol into a clean one: "unknown" is not "not
  monitored";
* a valid feed that tags nothing is treated as a failure as well. Binance always
  has tagged assets, so an empty list means the feed changed shape rather than that
  every tag was lifted, and clearing the marks on that evidence would be the one
  mistake that matters. It would also silently destroy the freshness signal, which
  is derived from the stored rows;
* the marks are labels in the published document and nothing more. They never
  block a symbol, never stop trading, and never fail a command.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError

from agent.core.db import settings_session
from agent.models import MonitoredSymbol

logger = logging.getLogger(__name__)

#: The page a human would read. Named in failures so an operator knows which
#: listing is being talked about.
MONITORING_PAGE_URL = "https://www.binance.com/en/markets/coinInfo-Monitoring"

#: The feed that page renders from. Ours by imitation, not by contract.
MONITORING_FEED_URL = (
    "https://www.binance.com/bapi/asset/v2/public/asset-service/product/get-products"
)

#: How the tag is spelled in the feed.
MONITORING_TAG = "Monitoring"

#: The daily cadence the tag deserves: it changes when Binance announces a review,
#: not intraday.
MONITORING_REFRESH_SECONDS = 24 * 60 * 60


class MonitoringError(RuntimeError):
    """The monitoring tag list could not be read or stored."""


def parse_monitoring(payload: Any) -> frozenset[str]:
    """The symbols tagged ``Monitoring`` in a feed payload, or raise.

    Kept separate from the request so the parsing rules can be exercised against
    a captured payload without a network.
    """
    if not isinstance(payload, dict):
        raise MonitoringError("the monitoring feed did not return a JSON object")
    products = payload.get("data")
    if not isinstance(products, list):
        raise MonitoringError("the monitoring feed did not return a product list")
    tagged: set[str] = set()
    for product in products:
        if not isinstance(product, dict):
            continue
        symbol = product.get("s")
        tags = product.get("tags")
        if isinstance(symbol, str) and isinstance(tags, list):
            if MONITORING_TAG in tags:
                tagged.add(symbol)
    if not tagged:
        raise MonitoringError(
            "the monitoring feed tagged nothing; refusing to read that as "
            "'nothing is monitored'"
        )
    return frozenset(tagged)


class MonitoringFeed:
    """Reads the tagged symbols. The transport is injectable on purpose.

    ``transport`` exists so tests — and anyone who wants to see how this behaves
    when the feed misbehaves — can hand in a fake and never touch the network.
    """

    def __init__(
        self,
        *,
        url: str = MONITORING_FEED_URL,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = url
        self._timeout = timeout
        self._transport = transport

    async def fetch(self) -> frozenset[str]:
        """The tagged symbols, or :class:`MonitoringError`."""
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
                follow_redirects=True,
            ) as client:
                response = await client.get(
                    self._url, headers={"accept": "application/json"}
                )
                response.raise_for_status()
                payload = response.json()
        except MonitoringError:
            raise
        except Exception as exc:  # noqa: BLE001 - httpx raises several types
            raise MonitoringError(
                f"cannot read the data behind {MONITORING_PAGE_URL}: {exc}"
            ) from exc
        return parse_monitoring(payload)


def default_feed() -> MonitoringFeed:
    """The feed this agent reads.

    The one place to change the source: point it at a supported endpoint if one
    ever appears, or at a fixture while working on something else.
    """
    return MonitoringFeed()


@dataclass(frozen=True)
class MonitoringMarks:
    """What the agent knows about the tag, and when it last learned it."""

    symbols: frozenset[str] = frozenset()
    refreshed_at: datetime | None = None


async def load_monitoring() -> MonitoringMarks:
    """The stored marks, or nothing marked when they cannot be read.

    A missing or unreachable settings DB must not stop the agent from trading, so
    a failure is logged and reported as "nothing is tagged" — the same degradation
    the working symbols accept. Callers that make decisions should treat an empty
    set as "nothing known", not as a clean bill of health.

    ``refreshed_at`` is the newest stored row: :func:`save_monitoring` refuses an
    empty list, so any successful refresh leaves one behind and the timestamp
    cannot be lost while the marks exist.
    """
    try:
        async with settings_session() as session:
            rows = (
                await session.execute(
                    select(MonitoredSymbol.symbol, MonitoredSymbol.updated_at)
                )
            ).all()
    except (SQLAlchemyError, RuntimeError) as exc:
        logger.warning("cannot read the monitored symbols: %s", exc)
        return MonitoringMarks()
    if not rows:
        return MonitoringMarks()
    return MonitoringMarks(
        symbols=frozenset(symbol for symbol, _ in rows),
        refreshed_at=max(updated_at for _, updated_at in rows),
    )


async def save_monitoring(symbols: Iterable[str]) -> MonitoringMarks:
    """Replace the stored marks with ``symbols`` and return what was stored.

    Raises whatever the database raises, and :class:`MonitoringError` for an empty
    list (see the module docstring): a refresh that cannot be stored must be
    reported rather than quietly pretended.
    """
    wanted = sorted({symbol.upper() for symbol in symbols})
    if not wanted:
        raise MonitoringError("refusing to store an empty monitoring list")
    now = datetime.now(UTC)
    async with settings_session() as session:
        await session.execute(delete(MonitoredSymbol))
        session.add_all(
            MonitoredSymbol(symbol=symbol, updated_at=now) for symbol in wanted
        )
        await session.commit()
    return MonitoringMarks(symbols=frozenset(wanted), refreshed_at=now)


async def refresh_monitoring(
    *, feed: MonitoringFeed | None = None
) -> MonitoringMarks | None:
    """Read the tags and store them, or return ``None`` if either step failed.

    The caller decides what to do with success — the daily loop republishes the
    universe so the page shows the new labels immediately, and nothing else needs
    to happen, because every publish from then on reads the stored marks.
    """
    source = feed if feed is not None else default_feed()
    try:
        return await save_monitoring(await source.fetch())
    except MonitoringError as exc:
        logger.warning("cannot refresh the monitoring tag list: %s", exc)
        return None
    except SQLAlchemyError as exc:
        logger.warning("cannot store the monitoring tag list: %s", exc)
        return None


def apply_monitoring(
    document: dict[str, Any], marks: MonitoringMarks
) -> dict[str, Any]:
    """Mark the document with the tags, in place, and return it.

    Unlike the working symbols there is no stub for a tagged symbol that the
    universe does not list: the tag is secondary information about a symbol, and
    inventing an entry for it would put a row on the page that the exchange does
    not trade.
    """
    for entry in document.get("symbols", []):
        entry["monitoring"] = entry["symbol"] in marks.symbols
    document["monitoring_updated_at"] = (
        marks.refreshed_at.isoformat() if marks.refreshed_at is not None else None
    )
    return document


__all__ = [
    "MONITORING_FEED_URL",
    "MONITORING_PAGE_URL",
    "MONITORING_REFRESH_SECONDS",
    "MONITORING_TAG",
    "MonitoringError",
    "MonitoringFeed",
    "MonitoringMarks",
    "apply_monitoring",
    "default_feed",
    "load_monitoring",
    "parse_monitoring",
    "refresh_monitoring",
    "save_monitoring",
]
