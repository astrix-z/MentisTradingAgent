"""Triple Screen v2: settings contract, wave/ripple screens, percentile source."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal, Inexact, localcontext
from typing import Any

import pytest
from pg import scratch_database
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from test_strategy_catalog import _COMPARE, RuleFailed, _eval_expr
from test_strategy_runtime import (
    TRIGGER,
    MultiRest,
    MultiStreams,
    RedisSink,
    bar,
    multi_services,
)

from agent.common.intervals import interval_ms
from agent.protocol import describe_catalog, describe_strategy
from agent.strategies import STRATEGIES
from agent.strategies.tripple_screen.indicators import Candle
from agent.strategies.tripple_screen_v2.percentiles import (
    DbPercentileSource,
    table_name,
)
from agent.strategies.tripple_screen_v2.presets import BUILTIN_PRESETS
from agent.strategies.tripple_screen_v2.settings import TripleScreenV2Settings
from agent.strategies.tripple_screen_v2.strategy import TripleScreenV2Strategy

SYMBOL = "BTCUSDT"
PARAMS: dict[str, Any] = {
    "kline_window": 12,
    "ema_tide_periods": [2, 3, 4],
    "ema_wave_periods": [2, 3, 4],
    "ema_ripple_periods": [5, 8],
}
# The trigger bar has just closed.
DECIDE_AT = TRIGGER + 900_000


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> None:
    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Clock:
            return cls.fromtimestamp(DECIDE_AT / 1000, UTC)

    for module in ("tripple_screen", "tripple_screen_v2"):
        monkeypatch.setattr(f"agent.strategies.{module}.strategy.datetime", Clock)


def windows(
    *, wave_step: int = 1, jump: str = "12", count: int = 12
) -> dict[str, list[Candle]]:
    """Rising tide; wave closes moving by ``wave_step`` per bar; a flat ripple
    at 10 whose trigger bar (the last) closes at ``jump``.

    With ripple EMA 5/8 seeded flat at 10, a jump to 12 moves EMA 5 by
    +6.67 % and EMA 8 by +4.44 %.
    """
    result: dict[str, list[Candle]] = {}
    for interval in ("4h", "1h", "15m"):
        step = interval_ms(interval)
        # The wave bar closes before the trigger opens; tide and ripple at its close.
        cutoff = TRIGGER if interval == "1h" else TRIGGER + 899_999
        end = (cutoff + 1) // step * step
        result[interval] = [
            bar(end - (count - i) * step, interval, "10") for i in range(count)
        ]
    result["4h"] = [
        replace(c, close=Decimal(100 + i)) for i, c in enumerate(result["4h"])
    ]
    result["1h"] = [
        replace(c, close=Decimal(100 + wave_step * i))
        for i, c in enumerate(result["1h"])
    ]
    result["15m"][-1] = replace(result["15m"][-1], close=Decimal(jump))
    return result


def decide(
    thresholds: dict[int, Decimal] | None, **shape: Any
) -> tuple[TripleScreenV2Strategy, Any]:
    strategy = TripleScreenV2Strategy(PARAMS)
    if thresholds is not None:
        strategy._thresholds = {SYMBOL: thresholds}
    return strategy, strategy._decide(SYMBOL, windows(**shape))


# --- Settings contract --------------------------------------------------------


def test_default_preset_equals_the_model_defaults() -> None:
    assert TripleScreenV2Settings.model_validate(BUILTIN_PRESETS["Default"]) == (
        TripleScreenV2Settings()
    )
    settings = TripleScreenV2Settings()
    assert settings.ema_wave_periods == (5, 13, 34)
    assert settings.ema_ripple_periods == (5, 13)
    assert settings.ripple_percentile == "95"
    assert settings.ripple_interval == "15m"


def test_stochastic_parameters_are_not_part_of_v2() -> None:
    fields = set(TripleScreenV2Settings.model_fields)
    assert not {name for name in fields if "stochastic" in name or "fi_" in name}
    # A v1 payload's extra keys are ignored like any shared key.
    TripleScreenV2Settings.from_params({"stochastic_k_period": 5})


ENTRY_V2 = describe_strategy("tripple_screen_v2", STRATEGIES["tripple_screen_v2"])


def published_failures(params: dict[str, Any]) -> list[str]:
    values = {
        name: prop.get("default")
        for name, prop in ENTRY_V2["settings_schema"]["properties"].items()
    }
    values.update(params)
    lookups = describe_catalog()["lookups"]
    failures = []
    for rule in ENTRY_V2["constraints"]:
        try:
            with localcontext() as context:
                context.prec = 1000
                context.traps[Inexact] = True
                lhs = _eval_expr(rule["lhs"], values, lookups)
                rhs = _eval_expr(rule["rhs"], values, lookups)
        except RuleFailed:
            failures.append(rule["message"])
            continue
        if not _COMPARE[rule["cmp"]](lhs, rhs):
            failures.append(rule["message"].format(lhs=int(lhs), rhs=int(rhs)))
    return failures


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"ema_wave_periods": [13, 5, 34]},
        {"ema_wave_periods": [5, 34, 34]},
        {"ema_ripple_periods": [13, 5]},
        {"ema_ripple_periods": [8, 8]},
        {"ema_tide_periods": [5, 3, 34]},
        {"kline_window": 34},
        {"kline_window": 35},
        {"ema_wave_periods": [5, 13, 50], "kline_window": 49},
        {"ema_wave_periods": [5, 13, 50], "kline_window": 50},
        {"ema_ripple_periods": [89, 144], "kline_window": 144},
        {"ema_ripple_periods": [89, 144], "kline_window": 145},
        {"wave_interval": "4h"},
        {"ripple_interval": "1h"},
        {"trail_cap_pct": "10"},
    ],
)
def test_published_constraints_match_the_model(params: dict[str, Any]) -> None:
    try:
        TripleScreenV2Settings.from_params(params)
    except ValueError:
        rejected = True
    else:
        rejected = False
    assert bool(published_failures(params)) == rejected


@pytest.mark.parametrize(
    "params",
    [
        {"ema_ripple_periods": [5, 7]},
        {"ema_ripple_periods": [5, 13, 21]},
        {"ripple_percentile": "99"},
        {"ripple_percentile": 95},
    ],
)
def test_schema_rejects_values_without_a_percentile_column(
    params: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        TripleScreenV2Settings.from_params(params)


# --- Decision -----------------------------------------------------------------


def test_entry_when_both_ema_changes_exceed_their_percentile() -> None:
    _, signal = decide({5: Decimal("6.6"), 8: Decimal("4.4")})
    assert signal is not None and signal.type == "ENTRY", signal
    assert signal.entry_ref == Decimal(12)
    assert signal.exit_policy is not None
    ripple = signal.provenance["ripple"]
    assert ripple["percentile"] == "95"
    assert ripple["conditions"] == {"ema_5_above_p95": True, "ema_8_above_p95": True}
    assert Decimal(ripple["ema"]["5"]["delta_pct"]).quantize(Decimal("0.01")) == (
        Decimal("6.67")
    )
    assert Decimal(ripple["ema"]["8"]["delta_pct"]).quantize(Decimal("0.01")) == (
        Decimal("4.44")
    )
    assert ripple["ema"]["8"]["threshold_pct"] == "4.4"
    assert signal.provenance["wave"]["conditions"] == {
        "fast_above_mid": True,
        "mid_above_slow": True,
    }
    assert "entry_trigger" not in ripple


@pytest.mark.parametrize(
    "thresholds",
    [
        {5: Decimal("6.6"), 8: Decimal("4.5")},
        {5: Decimal("6.7"), 8: Decimal("4.4")},
    ],
)
def test_hold_when_an_ema_change_stays_at_or_below_its_percentile(
    thresholds: dict[int, Decimal],
) -> None:
    _, signal = decide(thresholds)
    assert signal is not None and signal.type == "HOLD"
    assert signal.reason == "no_momentum"
    assert signal.exit_policy is None


def test_a_change_equal_to_its_percentile_does_not_pass() -> None:
    _, probe = decide({5: Decimal(0), 8: Decimal(0)})
    delta = Decimal(probe.provenance["ripple"]["ema"]["5"]["delta_pct"])
    _, signal = decide({5: delta, 8: Decimal(0)})
    assert signal is not None and signal.reason == "no_momentum"
    _, signal = decide({5: delta - Decimal("1e-20"), 8: Decimal(0)})
    assert signal is not None and signal.type == "ENTRY"


def test_hold_when_the_ripple_ema_falls() -> None:
    _, signal = decide({5: Decimal("-1"), 8: Decimal("-1")}, jump="9")
    assert signal is not None and signal.reason == "no_momentum"


def test_hold_when_wave_emas_are_not_aligned() -> None:
    _, signal = decide({5: Decimal(0), 8: Decimal(0)}, wave_step=-1)
    assert signal is not None and signal.reason == "no_wave_alignment"
    assert signal.provenance["wave"]["ema_aligned"] is False


def test_hold_without_percentiles_for_the_symbol() -> None:
    _, signal = decide(None)
    assert signal is not None and signal.reason == "no_percentiles"
    assert signal.provenance["ripple"]["thresholds_loaded"] is False


def test_breakout_is_not_required() -> None:
    # v1 needed close > previous high; here the previous high (19) stays above.
    _, signal = decide({5: Decimal(1), 8: Decimal(1)})
    assert signal is not None and signal.type == "ENTRY"
    assert signal.entry_ref is not None and signal.entry_ref < Decimal(19)


def test_no_signal_when_the_tide_is_not_bullish() -> None:
    strategy = TripleScreenV2Strategy(PARAMS)
    strategy._thresholds = {SYMBOL: {5: Decimal(0), 8: Decimal(0)}}
    shaped = windows()
    shaped["4h"] = [
        replace(c, close=Decimal(200 - i)) for i, c in enumerate(shaped["4h"])
    ]
    assert strategy._decide(SYMBOL, shaped) is None


def test_position_and_cooldown_still_block_an_entry() -> None:
    strategy = TripleScreenV2Strategy(PARAMS)
    strategy._thresholds = {SYMBOL: {5: Decimal(1), 8: Decimal(1)}}
    strategy.mark_position_open(SYMBOL)
    signal = strategy._decide(SYMBOL, windows())
    assert signal is not None and signal.reason == "position_open"


# --- Screening and lifecycle --------------------------------------------------


class FakePercentiles:
    def __init__(self, tables: dict[str, dict[int, Decimal]]) -> None:
        self.tables = tables
        self.reads: list[tuple[list[str], str, tuple[int, ...], str]] = []

    async def available(self, symbols: Iterable[str], interval: str) -> set[str]:
        return {symbol for symbol in symbols if symbol in self.tables}

    async def thresholds(
        self,
        symbols: Iterable[str],
        interval: str,
        periods: Sequence[int],
        percentile: str,
    ) -> dict[str, dict[int, Decimal]]:
        symbols = list(symbols)
        self.reads.append((symbols, interval, tuple(periods), percentile))
        return {s: self.tables[s] for s in symbols if s in self.tables}


def test_screening_skips_symbols_without_a_percentile_table() -> None:
    async def run() -> None:
        rest = MultiRest({"AAAUSDT": windows(), "BBBUSDT": windows()})
        streams, sink = MultiStreams(), RedisSink()
        strategy = TripleScreenV2Strategy({**PARAMS, "ws_top_n": 30})
        source = FakePercentiles({"AAAUSDT": {5: Decimal(1), 8: Decimal(2)}})
        strategy._percentile_source = source
        await strategy.on_start(
            multi_services(rest, streams, sink, ["AAAUSDT", "BBBUSDT"])
        )
        try:
            assert strategy._active_symbols == {"AAAUSDT"}
            assert strategy._thresholds == {"AAAUSDT": {5: Decimal(1), 8: Decimal(2)}}
            assert source.reads == [(["AAAUSDT"], "15m", (5, 8), "95")]
            # The excluded symbol never cost a REST window.
            assert {call["symbol"] for call in rest.calls} == {"AAAUSDT"}
            snapshot = strategy.indicator_snapshot()
            assert snapshot is not None
            assert snapshot["screening"]["skipped"] == [
                {
                    "symbol": "BBBUSDT",
                    "reason": "no 15m percentile table (prcntl_bbbusdt_15m)",
                }
            ]
            row = snapshot["rows"][0]["groups"]
            assert row["ripple"]["values"]["threshold_fast"]["current"] == "1"
            assert row["ripple"]["values"]["threshold_mid"]["current"] == "2"
            assert set(row["wave"]["values"]) == {"ema_fast", "ema_mid", "ema_slow"}
            labels = [i["label"] for i in snapshot["groups"][2]["indicators"]]
            assert labels == [
                "ΔEMA 5 %",
                "p95 ΔEMA 5 %",
                "ΔEMA 8 %",
                "p95 ΔEMA 8 %",
            ]
        finally:
            await strategy.on_stop()
        assert not strategy._thresholds

    asyncio.run(run())


def test_start_without_the_market_db_is_refused() -> None:
    async def run() -> None:
        strategy = TripleScreenV2Strategy(PARAMS)
        services = multi_services(MultiRest({}), MultiStreams(), RedisSink(), [SYMBOL])
        with pytest.raises(RuntimeError, match="BINANCE_DB_URL"):
            await strategy.on_start(services)

    asyncio.run(run())


def test_v2_is_registered_next_to_v1() -> None:
    assert {"tripple_screen", "tripple_screen_v2"} <= set(STRATEGIES)
    assert ENTRY_V2["label"] == "Triple Screen v2"
    assert [p["name"] for p in ENTRY_V2["presets"]] == ["Default"]


# --- Percentile source over PostgreSQL ----------------------------------------


def test_table_name_is_lower_case_and_checked() -> None:
    assert table_name("BTCUSDT", "15m") == "prcntl_btcusdt_15m"
    with pytest.raises(ValueError):
        table_name('X"; drop table y; --', "15m")


def test_db_source_reads_the_percentile_row() -> None:
    async def run() -> None:
        async with scratch_database() as url:
            engine = create_async_engine(
                url.replace("postgresql://", "postgresql+psycopg://", 1)
            )
            try:
                async with engine.begin() as connection:
                    await connection.execute(
                        text(
                            "create table prcntl_btcusdt_15m (index text, "
                            "ma_5_delta double precision, "
                            "ma_13_delta double precision)"
                        )
                    )
                    await connection.execute(
                        text(
                            "insert into prcntl_btcusdt_15m values "
                            "('90%', 0.12, 0.08), ('95%', 0.1934, 0.1241)"
                        )
                    )
                    await connection.execute(
                        text(
                            "create table prcntl_ethusdt_15m (index text, "
                            "ma_5_delta double precision, "
                            "ma_13_delta double precision)"
                        )
                    )
                source = DbPercentileSource(engine)
                symbols = ["BTCUSDT", "ETHUSDT", "XRPUSDT"]
                assert await source.available(symbols, "15m") == {
                    "BTCUSDT",
                    "ETHUSDT",
                }
                # ETHUSDT has a table but no 95% row: left out like a missing one.
                assert await source.thresholds(symbols, "15m", (5, 13), "95") == {
                    "BTCUSDT": {5: Decimal("0.1934"), 13: Decimal("0.1241")}
                }
            finally:
                await engine.dispose()

    asyncio.run(run())
