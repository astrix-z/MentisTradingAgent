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
from app.models import SignalExitPending, SignalPosition

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
        session.execute(
            delete(SignalExitPending).where(col(SignalExitPending.symbol) == SYMBOL)
        )
        session.commit()


def _pending() -> list[datetime]:
    with Session(engine) as session:
        rows = session.exec(
            select(SignalExitPending).where(SignalExitPending.symbol == SYMBOL)
        ).all()
        return [row.emitted_at for row in rows]


def _entry_payload(at: datetime) -> dict[str, str]:
    return {
        "signal_id": uuid.uuid4().hex,
        "symbol": SYMBOL,
        "epoch_id": "e",
        "entry_ref": "100",
        "swing_low_ref": "99",
        "tp_rule": "fill x 1.1",
        "emitted_at": at.isoformat(),
    }


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


# --- re-review 2026-10-03: an old leader's ENTRY lands after the new one's EXIT -----


def test_an_entry_recorded_after_its_exit_is_closed_by_it() -> None:
    # The old leader's ENTRY write is delayed past a handover; the new leader
    # handles the EXIT first. It used to find nothing, and the ENTRY then
    # stayed open forever.
    _clear()
    try:
        base = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
        exit_at = base + timedelta(minutes=10)
        sl._record_exit(_exit_payload(exit_at), NETWORK)
        assert _statuses() == [] and _pending() == [exit_at]
        sl._record_exit(_exit_payload(exit_at), NETWORK)  # redelivered
        assert _pending() == [exit_at]

        entry = _entry_payload(base)
        sl._record_entry(entry, NETWORK)
        assert _statuses() == ["hit_take"] and _pending() == []
        with Session(engine) as session:
            row = session.exec(
                select(SignalPosition).where(
                    SignalPosition.signal_id == entry["signal_id"]
                )
            ).one()
            assert row.closed_at == exit_at
        sl._record_entry(entry, NETWORK)  # redelivered ENTRY: no-op
        sl._record_exit(_exit_payload(exit_at), NETWORK)  # and EXIT: no-op
        assert _statuses() == ["hit_take"] and _pending() == []
    finally:
        _clear()


def test_an_exit_never_closes_a_position_emitted_after_it() -> None:
    _clear()
    try:
        base = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
        _open(base + timedelta(minutes=20))  # the next ENTRY, already recorded
        sl._record_exit(_exit_payload(base + timedelta(minutes=10)), NETWORK)
        assert _statuses() == ["open"]
        assert _pending() == [base + timedelta(minutes=10)]
        sl._record_entry(_entry_payload(base), NETWORK)  # the late, own ENTRY
        assert _statuses() == ["hit_take", "open"] and _pending() == []
        # An ENTRY emitted after a waiting EXIT is not closed by it.
        sl._record_exit(_exit_payload(base + timedelta(minutes=30)), NETWORK)
        assert _statuses() == ["hit_take", "hit_take"]
        sl._record_exit(_exit_payload(base + timedelta(minutes=40)), NETWORK)
        sl._record_entry(_entry_payload(base + timedelta(minutes=50)), NETWORK)
        assert _statuses() == ["hit_take", "hit_take", "open"]
    finally:
        _clear()
