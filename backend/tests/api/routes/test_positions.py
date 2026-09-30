"""GET /trading/positions/ and the signal listener that feeds it.

Positions are derived from ENTRY signals only (the strategy never places a
real order and never emits EXIT today) and closed lazily on read once the
last known price has crossed the stop/take level.
"""

from __future__ import annotations

import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, delete, select

from app.api.routes import trading
from app.core.config import settings
from app.core.signal_listener import _record_entry, _record_exit, _take_ref
from app.models import SignalPosition


@pytest.fixture(autouse=True)
def _clean_signal_positions(db: Session) -> Any:
    yield
    db.execute(delete(SignalPosition))
    db.commit()


def entry_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "signal_id": "sig-1",
        "symbol": "BTCUSDT",
        "epoch_id": "BTCUSDT:1",
        "entry_ref": "100",
        "swing_low_ref": "90",
        "tp_rule": "fill x 1.1",
        "emitted_at": datetime.now(UTC).isoformat(),
        "valid_until": None,
    }
    payload.update(overrides)
    return payload


def test_take_ref_parses_the_strategys_tp_rule_template() -> None:
    assert _take_ref("100", "fill x 1.1") == "110.0"
    assert _take_ref("100", "not a rule") is None


def test_record_entry_is_idempotent_on_signal_id(db: Session) -> None:
    _record_entry(entry_payload(), "live")
    _record_entry(entry_payload(), "live")  # redelivered pub/sub message
    rows = db.exec(
        select(SignalPosition).where(SignalPosition.signal_id == "sig-1")
    ).all()
    assert len(rows) == 1
    assert rows[0].symbol == "BTCUSDT"
    assert rows[0].take_ref == "110.0"
    assert rows[0].status == "open"


def test_record_entry_tolerates_concurrent_workers(db: Session) -> None:
    # Prod runs `fastapi run --workers 4`, each worker with its own listener,
    # so one signal is recorded by four callers at once; none may raise.
    payload = entry_payload(signal_id="sig-race")
    barrier = threading.Barrier(4)

    def record() -> None:
        barrier.wait()
        _record_entry(payload, "live")

    with ThreadPoolExecutor(max_workers=4) as pool:
        for future in [pool.submit(record) for _ in range(4)]:
            future.result()
    rows = db.exec(
        select(SignalPosition).where(SignalPosition.signal_id == "sig-race")
    ).all()
    assert len(rows) == 1


def test_record_entry_rejects_unparseable_tp_rule(db: Session) -> None:
    _record_entry(entry_payload(signal_id="sig-2", tp_rule="???"), "live")
    row = db.exec(
        select(SignalPosition).where(SignalPosition.signal_id == "sig-2")
    ).first()
    assert row is None


def test_record_exit_closes_the_open_position(db: Session) -> None:
    _record_entry(entry_payload(signal_id="sig-3"), "live")
    _record_exit(
        {
            "symbol": "BTCUSDT",
            "exit_reason": "tp",
            "emitted_at": datetime.now(UTC).isoformat(),
        },
        "live",
    )
    row = db.exec(
        select(SignalPosition).where(SignalPosition.signal_id == "sig-3")
    ).first()
    assert row is not None
    assert row.status == "hit_take"
    assert row.closed_at is not None


def test_a_duplicate_exit_closes_one_position_on_its_network(db: Session) -> None:
    # Review 2026-09-30: two open positions of one symbol plus a redelivered
    # EXIT closed both; the query also ignored the network.
    symbol = f"DUP{uuid.uuid4().hex[:8].upper()}USDT"
    for signal_id, network in (("a", "live"), ("b", "live"), ("c", "testnet")):
        _record_entry(
            entry_payload(signal_id=f"{symbol}-{signal_id}", symbol=symbol), network
        )
    exit_payload = {
        "symbol": symbol,
        "exit_reason": "stop",
        "emitted_at": datetime.now(UTC).isoformat(),
    }
    _record_exit(exit_payload, "live")
    _record_exit(exit_payload, "live")  # redelivered
    rows = {
        row.signal_id: row.status
        for row in db.exec(
            select(SignalPosition).where(SignalPosition.symbol == symbol)
        ).all()
    }
    assert sorted(rows.values()) == ["hit_stop", "open", "open"]
    assert rows[f"{symbol}-c"] == "open"  # the other network is untouched


def _universe_payload(last_price: str) -> str:
    return json.dumps(
        {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "quote_asset": "USDT",
                    "status": "TRADING",
                    "spot_allowed": True,
                    "last_price": last_price,
                    "classification": "crypto",
                }
            ]
        }
    )


@pytest.mark.parametrize(
    "last_price,expected_status",
    [("111", "hit_take"), ("85", "hit_stop"), ("101", "open")],
)
def test_positions_endpoint_closes_lazily_on_price_cross(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    last_price: str,
    expected_status: str,
) -> None:
    db.add(
        SignalPosition(
            signal_id="sig-endpoint",
            symbol="BTCUSDT",
            network="live",
            epoch_id="BTCUSDT:1",
            entry_ref="100",
            swing_low_ref="90",
            tp_rule="fill x 1.1",
            take_ref="110",
            emitted_at=datetime.now(UTC) - timedelta(minutes=5),
        )
    )
    db.commit()

    monkeypatch.setattr(
        trading,
        "_get_working_symbols",
        AsyncMock(
            return_value=trading.WorkingSymbols.model_validate_json(
                _universe_payload(last_price)
            )
        ),
    )

    response = client.get(
        f"{settings.API_V1_STR}/trading/positions/", headers=superuser_token_headers
    )
    assert response.status_code == 200
    positions = response.json()["positions"]
    assert len(positions) == 1
    assert positions[0]["status"] == expected_status
    if expected_status != "open":
        assert positions[0]["closed_price"] == last_price

    # The transition must have been persisted, not just reported once.
    row = db.exec(
        select(SignalPosition).where(SignalPosition.signal_id == "sig-endpoint")
    ).first()
    assert row is not None
    assert row.status == expected_status
