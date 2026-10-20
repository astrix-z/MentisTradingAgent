"""Read-only access to the ``prcntl_<symbol>_<interval>`` tables of the Binance DB.

Each table is the distribution summary of one symbol/interval's EMA one-bar
changes: a text ``index`` column (``count``, ``mean``, ``std``, ``min``,
``5%`` … ``95%``, ``max``) and one ``ma_<N>_delta`` column per EMA period. A
delta is ``(EMA[t] − EMA[t−1]) / EMA[t−1] × 100`` — the same formula as the
``klines_*_ema`` tables the percentiles are computed from.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from decimal import Decimal
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

#: A table name goes into SQL, so its parts are checked rather than trusted.
_SAFE_PART = re.compile(r"[a-z0-9]+")


def table_name(symbol: str, interval: str) -> str:
    """``prcntl_<symbol>_<interval>``, lower-case as the Binance DB names it."""
    parts = (symbol.lower(), interval.lower())
    if not all(_SAFE_PART.fullmatch(part) for part in parts):
        raise ValueError(f"unsafe percentile table parts: {parts!r}")
    return f"prcntl_{parts[0]}_{parts[1]}"


class PercentileSource(Protocol):
    async def available(self, symbols: Iterable[str], interval: str) -> set[str]:
        """The subset of ``symbols`` that has a percentile table."""

    async def thresholds(
        self,
        symbols: Iterable[str],
        interval: str,
        periods: Sequence[int],
        percentile: str,
    ) -> dict[str, dict[int, Decimal]]:
        """Per symbol, the ``<percentile>%`` row of each ``ma_<N>_delta``.

        A symbol without a table, without that row or with a missing value is
        left out.
        """


class DbPercentileSource:
    """:class:`PercentileSource` over the read-only Binance DB engine."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def available(self, symbols: Iterable[str], interval: str) -> set[str]:
        by_table = {}
        for symbol in symbols:
            try:
                by_table[table_name(symbol, interval)] = symbol
            except ValueError:
                continue
        if not by_table:
            return set()
        async with self._engine.connect() as connection:
            rows = await connection.execute(
                text(
                    "select table_name from information_schema.tables "
                    "where table_schema = 'public' and table_name = any(:names)"
                ),
                {"names": list(by_table)},
            )
            return {by_table[name] for (name,) in rows}

    async def thresholds(
        self,
        symbols: Iterable[str],
        interval: str,
        periods: Sequence[int],
        percentile: str,
    ) -> dict[str, dict[int, Decimal]]:
        symbols = list(symbols)
        present = await self.available(symbols, interval)
        columns = ", ".join(f"ma_{int(period)}_delta" for period in periods)
        result: dict[str, dict[int, Decimal]] = {}
        async with self._engine.connect() as connection:
            for symbol in symbols:
                if symbol not in present:
                    continue
                row = (
                    await connection.execute(
                        text(
                            f'select {columns} from "{table_name(symbol, interval)}" '
                            "where index = :row"
                        ),
                        {"row": f"{percentile}%"},
                    )
                ).first()
                if row is None or any(value is None for value in row):
                    continue
                result[symbol] = {
                    int(period): Decimal(str(value))
                    for period, value in zip(periods, row, strict=True)
                }
        return result


__all__ = ["DbPercentileSource", "PercentileSource", "table_name"]
