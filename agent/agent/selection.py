"""Which symbols the user marked, and whether the exchange still agrees.

The mark lives in the agent's own database (:mod:`agent.models`), because this
is where the symbols are actually used and where the trades that follow will be
recorded. The backend serves the UI from the document this module merges into
(:func:`agent.symbols.publish_universe`) and asks for changes through the
command lane; it never reads or writes the table.

Validation is deliberately the agent's: it is the only service that talks to the
exchange, so it is the only one that can say whether a marked symbol is still
listed, still spot-tradable, and still something other than a stablecoin or a
tokenized equity. A marked symbol that stopped qualifying is not silently
dropped — it stays marked, and the published document says why it cannot be used.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError

from agent.core.db import settings_session
from agent.models import WorkingSymbol

logger = logging.getLogger(__name__)

#: Reported when a marked symbol is not usable right now.
NO_LONGER_LISTED = "no longer listed"


async def load_selected() -> set[str]:
    """The marked symbols, or an empty set when nothing can be read.

    A missing or unreachable settings DB must not stop the agent from trading,
    so a failure is logged and treated as "nothing is marked" — the document
    then shows an empty selection rather than a stale one.
    """
    try:
        async with settings_session() as session:
            rows = await session.scalars(select(WorkingSymbol.symbol))
            return set(rows)
    except (SQLAlchemyError, RuntimeError) as exc:
        logger.warning("cannot read the working symbols: %s", exc)
        return set()


async def save_selected(symbols: Iterable[str]) -> list[str]:
    """Replace the marked set with ``symbols``, and return it sorted.

    Raises whatever the database raises: a caller that cannot persist must
    report failure rather than claim the mark was stored.
    """
    wanted = sorted({symbol.upper() for symbol in symbols})
    async with settings_session() as session:
        await session.execute(delete(WorkingSymbol))
        session.add_all(WorkingSymbol(symbol=symbol) for symbol in wanted)
        await session.commit()
    return wanted


def validate_selection(symbols: Sequence[str], document: dict[str, Any]) -> list[str]:
    """The subset of ``symbols`` the exchange does not currently allow.

    Only *usable* symbols qualify: a delisted, non-spot, stable, gold, wrapped,
    equity or non-Latin symbol is rejected even if it is still listed, because
    the working list is what the strategy is allowed to trade.
    """
    usable = {
        entry["symbol"]
        for entry in document.get("symbols", [])
        if entry.get("excluded_reason") is None
    }
    return sorted(
        {symbol.strip().upper() for symbol in symbols if symbol.strip()} - usable
    )


def apply_selection(document: dict[str, Any], selected: set[str]) -> dict[str, Any]:
    """Mark the document with the selection, in place, and return it.

    A marked symbol that the universe no longer lists at all gets a stub entry
    so the UI can show it as unusable instead of dropping it without a word.
    """
    known = set()
    for entry in document.get("symbols", []):
        entry["selected"] = entry["symbol"] in selected
        known.add(entry["symbol"])
    for missing in sorted(selected - known):
        document["symbols"].append(
            {
                "symbol": missing,
                "base_asset": None,
                "quote_asset": document.get("quote_asset"),
                "status": "MISSING",
                "spot_allowed": False,
                "quote_volume_24h": "0",
                "last_price": None,
                "classification": "crypto",
                "classification_label": None,
                "excluded_reason": NO_LONGER_LISTED,
                "monitoring": False,
                "selected": True,
            }
        )
    document["selected"] = len(selected)
    document["selected_unusable"] = sum(
        1
        for entry in document["symbols"]
        if entry.get("selected") and entry.get("excluded_reason") is not None
    )
    return document


__all__ = [
    "NO_LONGER_LISTED",
    "apply_selection",
    "load_selected",
    "save_selected",
    "validate_selection",
]
