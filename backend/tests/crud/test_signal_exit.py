"""EXIT recording on PostgreSQL: dedup and close are one serialized step.

Review 2026-10-03: the dedup check ran before the lock and select, so two
copies of one EXIT handled at once (a leadership handover overlap) could
close two different open positions of the symbol.
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, col, delete, select

from app.core import signal_listener as sl
from app.core.db import engine
from app.models import SignalPosition

SYMBOL = "EXITTESTUSDT"
NETWORK = "testnet"


def _open(emitted_at: datetime) -> None:
    with Session(engine) as session:
        session.add(
            SignalPosition(
                signal_id=uuid.uuid4().hex,
                symbol=SYMBOL,
                network=NETWORK,
                epoch_id="e",
                entry_ref="100",
                swing_low_ref="99",
                tp_rule="fill x 1.1",
                take_ref="110",
                emitted_at=emitted_at,
            )
        )
        session.commit()


def _statuses() -> list[str]:
    with Session(engine) as session:
        rows = session.exec(
            select(SignalPosition)
            .where(SignalPosition.symbol == SYMBOL)
            .order_by(col(SignalPosition.emitted_at))
        ).all()
        return [row.status for row in rows]


def _clear() -> None:
    with Session(engine) as session:
        session.execute(
            delete(SignalPosition).where(col(SignalPosition.symbol) == SYMBOL)
        )
        session.commit()


def _exit_payload(at: datetime) -> dict[str, str]:
    return {"symbol": SYMBOL, "emitted_at": at.isoformat(), "exit_reason": "tp"}


def _handle(
    barrier: threading.Barrier, payload: dict[str, str], errors: list[BaseException]
) -> None:
    try:
        barrier.wait(5)
        sl._record_exit(payload, NETWORK)
    except BaseException as exc:  # noqa: BLE001 - reported by the test
        errors.append(exc)


def test_parallel_duplicate_exit_closes_one_position() -> None:
    _clear()
    try:
        base = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
        for round_ in range(5):  # the race needs several tries to show
            _clear()
            _open(base)
            _open(base + timedelta(minutes=1))
            _open(base + timedelta(minutes=2))
            payload = _exit_payload(base + timedelta(minutes=10, seconds=round_))
            barrier = threading.Barrier(2)
            errors: list[BaseException] = []
            threads = [
                threading.Thread(target=_handle, args=(barrier, payload, errors))
                for _ in range(2)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
            assert errors == []
            # The latest open position is closed, once; the others stay open.
            assert _statuses() == ["open", "open", "hit_take"]
    finally:
        _clear()


def test_sequential_duplicate_exit_is_a_no_op() -> None:
    _clear()
    try:
        base = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
        _open(base)
        _open(base + timedelta(minutes=1))
        payload = _exit_payload(base + timedelta(minutes=10))
        sl._record_exit(payload, NETWORK)
        sl._record_exit(payload, NETWORK)
        assert _statuses() == ["open", "hit_take"]
    finally:
        _clear()
