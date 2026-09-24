"""EXEC-04: deterministic ENTRY ids, the exit policy and ExecutionOutcome."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

# The autouse clock fixture and the bar fixtures of the runtime suite.
from test_strategy_runtime import PARAMS, SYMBOL, clock, fixture  # noqa: F401

from agent.execution.outcome import ExecutionOutcome, ExitMechanism, ExitReason
from agent.strategies.base import (
    EXIT_POLICY_VERSION,
    ExitPolicy,
    Signal,
    entry_signal_id,
)
from agent.strategies.tripple_screen.settings import TripleScreenSettings
from agent.strategies.tripple_screen.strategy import TripleScreenStrategy

IDENTITY = {
    "account_uid": "123456789",
    "network": "live",
    "strategy": "tripple_screen",
    "symbol": "BTCUSDT",
    "epoch_id": "BTCUSDT:1800000000000",
}


def test_entry_id_is_stable_and_has_a_control_value() -> None:
    first = entry_signal_id(**IDENTITY)
    assert first == entry_signal_id(**IDENTITY)
    assert UUID(first).version == 5
    # Control example: if this changes, every stored epoch would be re-admitted.
    assert first == "e993d961-7e23-54d5-9469-f65503823989"


@pytest.mark.parametrize(
    "field", ["account_uid", "network", "strategy", "symbol", "epoch_id"]
)
def test_any_identity_part_changes_the_id(field: str) -> None:
    other = {**IDENTITY, field: IDENTITY[field] + "x"}
    assert entry_signal_id(**other) != entry_signal_id(**IDENTITY)


@pytest.mark.parametrize("bad", ["", "a|b"])
def test_ambiguous_identity_parts_are_refused(bad: str) -> None:
    with pytest.raises(ValueError):
        entry_signal_id(**{**IDENTITY, "account_uid": bad})


def _signal(kind: str) -> Signal:
    return Signal(
        type=kind,  # type: ignore[arg-type]
        symbol="BTCUSDT",
        emitted_at=datetime(2026, 9, 24, tzinfo=UTC),
        epoch_id="BTCUSDT:1800000000000",
    )


def test_entry_takes_its_context_identity_and_hold_keeps_its_own() -> None:
    context = {
        "account_uid": "123456789",
        "network": "live",
        "strategy": "tripple_screen",
    }
    entry = _signal("ENTRY")
    identified = entry.identified(**context)
    assert identified.signal_id == entry_signal_id(**IDENTITY)
    # A new decision of the same epoch (e.g. after a restart) lands on it too.
    assert _signal("ENTRY").identified(**context).signal_id == identified.signal_id
    assert (
        _signal("ENTRY").identified(**{**context, "network": "testnet"}).signal_id
        != identified.signal_id
    )
    hold = _signal("HOLD")
    assert hold.identified(**context) is hold


def _policy(**changes: object) -> ExitPolicy:
    values: dict[str, object] = {
        "version": EXIT_POLICY_VERSION,
        "stop_ref": Decimal("98.9"),
        "swing_low_ref": Decimal("99.5"),
        "min_stop_distance_pct": Decimal(1),
        "stop_buffer_pct": Decimal("0.1"),
        "tp_pct": Decimal(10),
        "trail_interval": "15m",
        "trail_n": 2,
        "trail_cap_pct": Decimal(4),
        "max_hold_hours": Decimal(8),
        "cooldown_minutes": 60,
    }
    values.update(changes)
    return ExitPolicy(**values)  # type: ignore[arg-type]


def test_percentages_are_percentage_points() -> None:
    policy = _policy()
    fill = Decimal(100)
    assert policy.initial_stop(fill) == Decimal(99)  # 1% below, not zero
    assert policy.initial_stop(Decimal(101)) == Decimal("99.5")  # swing is lower
    assert policy.take_profit(fill) == Decimal(110)
    assert policy.trail_ceiling(fill) == Decimal(104)  # not 5 × fill
    assert policy.trail_candidate([Decimal(100), Decimal(102)]) == Decimal("99.9")
    custom = _policy(trail_cap_pct=Decimal("2.5"), tp_pct=Decimal("3"))
    assert custom.trail_ceiling(fill) == Decimal("102.5")
    assert custom.take_profit(fill) == Decimal(103)


def test_policy_survives_json_without_losing_digits() -> None:
    policy = _policy(stop_ref=Decimal("0.000012345678901234567"))
    stored = json.loads(json.dumps(policy.transport()))
    assert stored["stop_ref"] == "0.000012345678901234567"
    assert ExitPolicy.from_transport(stored) == policy
    with pytest.raises(ValueError):
        ExitPolicy.from_transport({**stored, "version": 99})


def test_strategy_entry_carries_the_policy_of_its_settings() -> None:
    params = {**PARAMS, "trail_cap_pct": "3", "max_hold_hours": "0.5"}
    strategy = TripleScreenStrategy(params)
    signal = strategy._decide(SYMBOL, fixture())
    assert signal is not None and signal.type == "ENTRY"
    assert signal.entry_ref is not None and signal.swing_low_ref is not None
    policy = signal.exit_policy
    assert policy is not None
    settings = TripleScreenSettings.from_params(params)
    expected_ref = min(
        signal.swing_low_ref,
        signal.entry_ref * (1 - settings.min_stop_distance_pct / 100),
    )
    assert policy.stop_ref == expected_ref > 0
    assert policy.swing_low_ref == signal.swing_low_ref
    assert policy.trail_cap_pct == Decimal(3)
    assert policy.max_hold_hours == Decimal("0.5")
    assert policy.trail_interval == settings.ripple_interval
    assert policy.trail_n == settings.trail_m15_n
    assert policy.cooldown_minutes == settings.cooldown_minutes
    transported = signal.transport()["exit_policy"]
    assert transported["max_hold_hours"] == "0.5"
    assert ExitPolicy.from_transport(json.loads(json.dumps(transported))) == policy


def test_hold_carries_no_policy() -> None:
    strategy = TripleScreenStrategy(PARAMS)
    strategy.mark_position_open(SYMBOL)
    signal = strategy._decide(SYMBOL, fixture())
    assert signal is not None and signal.type == "HOLD"
    assert signal.exit_policy is None


@pytest.mark.parametrize(
    "params",
    [
        {"trail_cap_pct": 10},
        {"trail_cap_pct": 12},
        {"trail_cap_pct": -1},
        {"tp_pct": 2},
        {"max_hold_hours": 0},
    ],
)
def test_settings_refuse_a_cap_at_or_above_take_profit(
    params: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        TripleScreenSettings.from_params(params)


def test_outcome_transport_and_losing_flag() -> None:
    outcome = ExecutionOutcome(
        position_id="p",
        signal_id="s",
        account="1",
        network="live",
        symbol="BTCUSDT",
        strategy="tripple_screen",
        reason=ExitReason.STOP,
        mechanism=ExitMechanism.MARKET_GAP,
        opened_at=datetime(2026, 9, 24, tzinfo=UTC),
        closed_at=datetime(2026, 9, 24, 1, tzinfo=UTC),
        quantity=Decimal("0.001"),
        entry_vwap=Decimal(100),
        exit_vwap=Decimal(99),
        realized_pnl=Decimal("-0.0012"),
        commission_quote=Decimal("0.0002"),
        order_ids=("mta-a-e1", "mta-a-x1"),
        exit_policy=_policy().transport(),
    )
    assert outcome.losing
    data = json.loads(json.dumps(outcome.transport()))
    assert data["reason"] == "stop" and data["mechanism"] == "market_gap"
    assert data["realized_pnl"] == "-0.0012"
    assert data["order_ids"] == ["mta-a-e1", "mta-a-x1"]
