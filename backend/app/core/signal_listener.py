"""Background subscriber turning agent ENTRY/EXIT signals into DB rows.

The strategy never places real orders (D-SIGNAL-ONLY) — it only emits
``Signal`` events, published as JSON on the Redis pub/sub channel
``agent:events`` (see ``Engine._publish_event`` in ``agent/agent/engine.py``).
Pub/sub has no history: a message is lost forever if nothing is subscribed
when it fires. This module is the subscriber, turning ENTRY signals into
durable ``SignalPosition`` rows the backend's own DB owns, so
``GET /trading/positions/`` has something to read regardless of when a signal
happened to fire relative to who was watching.

The backend runs several uvicorn workers, each with this module's lifespan
task, but only one of them listens at a time (``run_signal_listener``): a
Redis lock elects the leader, which renews it while alive and releases it on
shutdown, so another worker takes over within ``_LEADER_TTL_MS`` if it dies.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import Session, col, select

from app.core.db import engine
from app.models import SignalExitPending, SignalPosition

logger = logging.getLogger(__name__)

REDIS_EVENTS_CHANNEL = "agent:events"
# Same key the backend's own routes read (backend/app/api/routes/trading.py);
# duplicated here rather than imported to keep this module independent of the
# routes module's import graph, matching how the agent/backend already
# duplicate these key names across the two codebases.
REDIS_RUNTIME_KEY = "agent:runtime"
_RECONNECT_DELAY_SECONDS = 2.0
# Leader election across uvicorn workers: the lock outlives a renew interval
# comfortably, so a slow tick never hands leadership over by accident.
REDIS_LISTENER_LOCK_KEY = "backend:signal-listener:leader"
_LEADER_TTL_MS = 15_000
_LEADER_RENEW_SECONDS = 5.0
# A renewal that has not answered by then counts as lost: the lease may be
# expiring while we wait (the client has no socket timeout of its own).
_LEADER_RENEW_TIMEOUT_SECONDS = 2.0
# The lease is trusted until TTL minus this margin after the moment the last
# successful SET/renew was *sent* — never after the answer arrived.
_LEASE_MARGIN_SECONDS = 2.0
# Renew/release only if the lock is still ours: another worker may have taken
# it after ours expired, and that one must not be extended or deleted.
_RENEW_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('pexpire', KEYS[1], ARGV[2])
end
return 0
"""
_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""
_TP_RULE_RE = re.compile(r"fill x ([\d.]+)")


def _take_ref(entry_ref: str, tp_rule: str) -> str | None:
    """Parse ``"fill x 1.1"`` against ``entry_ref`` into an absolute price.

    Coupled to the exact template the strategy formats ``tp_rule`` with
    (``agent/agent/strategies/tripple_screen/strategy.py``); an unrecognized
    format means the take level can't be computed, not a crash.
    """
    match = _TP_RULE_RE.fullmatch(tp_rule)
    if match is None:
        return None
    try:
        return str(Decimal(entry_ref) * Decimal(match.group(1)))
    except InvalidOperation:
        return None


def _record_entry(payload: dict[str, Any], network: str) -> None:
    signal_id = payload.get("signal_id")
    entry_ref = payload.get("entry_ref")
    if not signal_id or not entry_ref:
        logger.warning(
            "ENTRY signal missing signal_id/entry_ref, skipping: %s", payload
        )
        return
    take_ref = _take_ref(entry_ref, payload.get("tp_rule") or "")
    if take_ref is None:
        logger.warning(
            "cannot parse tp_rule=%r for signal_id=%s, skipping",
            payload.get("tp_rule"),
            signal_id,
        )
        return
    position = SignalPosition(
        signal_id=signal_id,
        symbol=payload["symbol"],
        network=network,
        epoch_id=payload.get("epoch_id") or "",
        entry_ref=entry_ref,
        swing_low_ref=payload.get("swing_low_ref") or "0",
        tp_rule=payload.get("tp_rule") or "",
        take_ref=take_ref,
        emitted_at=datetime.fromisoformat(payload["emitted_at"]),
        valid_until=(
            datetime.fromisoformat(payload["valid_until"])
            if payload.get("valid_until")
            else None
        ),
    )
    # Only the elected worker listens, but a leadership handover can briefly
    # overlap and pub/sub can redeliver after a reconnect, so the same signal
    # may still be recorded twice. ON CONFLICT makes "first writer wins, the
    # rest are no-ops" atomic, where a select-then-insert would race.
    statement = (
        pg_insert(SignalPosition)
        .values(**position.model_dump())
        .on_conflict_do_nothing(index_elements=["signal_id"])
        .returning(col(SignalPosition.id))
    )
    with Session(engine) as session:
        _lock_symbol(session, network, position.symbol)
        inserted = session.execute(statement).first() is not None  # ty: ignore[deprecated]
        closed_by = _take_pending_exit(session, position) if inserted else None
        session.commit()
    if not inserted:
        return  # already recorded by another worker or a redelivered message
    logger.info(
        "recorded ENTRY position symbol=%s signal_id=%s%s",
        payload["symbol"],
        signal_id,
        f" closed by the EXIT of {closed_by.isoformat()}" if closed_by else "",
    )


def _lock_symbol(session: Session, network: str, symbol: str) -> None:
    """Serialize the ENTRY/EXIT writes of one symbol until the commit.

    Two workers overlap in a leadership handover, so an old leader's write
    may run alongside — or after — the new leader's. Under this lock each
    write sees every earlier commit of the symbol whole.
    """
    session.execute(  # ty: ignore[deprecated]
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": f"signal-exit:{network}:{symbol}"},
    )


def _closed_status(exit_reason: str | None) -> str:
    return "hit_take" if exit_reason == "tp" else "hit_stop"


def _take_pending_exit(session: Session, position: SignalPosition) -> datetime | None:
    """Close a just-recorded ENTRY by an EXIT that was handled before it.

    The earliest waiting EXIT of the symbol emitted at or after the ENTRY
    is its own: when it was handled, no open position was there to close.
    """
    pending = session.exec(
        select(SignalExitPending)
        .where(SignalExitPending.network == position.network)
        .where(SignalExitPending.symbol == position.symbol)
        .where(SignalExitPending.emitted_at >= position.emitted_at)
        .order_by(col(SignalExitPending.emitted_at))
    ).first()
    if pending is None:
        return None
    session.execute(  # ty: ignore[deprecated]
        update(SignalPosition)
        .where(col(SignalPosition.signal_id) == position.signal_id)
        .values(
            status=_closed_status(pending.exit_reason), closed_at=pending.emitted_at
        )
    )
    session.delete(pending)
    return pending.emitted_at


def _record_exit(payload: dict[str, Any], network: str) -> None:
    """Close the open position for this symbol on this network, if any.

    Future-proofing: the strategy does not emit EXIT today (position
    monitoring is a separate, unbuilt lane), so this path is currently dead
    code in production but keeps the listener correct if/when it starts.

    Idempotent: an EXIT is applied at most once. A position of the symbol
    already closed at this EXIT's ``emitted_at`` (or the EXIT already
    waiting) means a redelivery (pub/sub after a reconnect, or a leadership
    handover), which must not close the next open position of the same
    symbol.

    It closes the latest position emitted at or before it; a later one is
    not its own. With none, the EXIT waits in ``signal_exit_pending``: an old
    leader's ENTRY write may still land after it, and is then recorded
    closed (:func:`_take_pending_exit`).

    Everything runs in one transaction under the symbol's advisory lock
    (:func:`_lock_symbol`): two copies of the same EXIT handled at once run
    one after the other, and the second sees the first one's effect.
    """
    closed_at = datetime.fromisoformat(payload["emitted_at"])
    symbol = payload["symbol"]
    if payload.get("entry_signal_id"):
        _record_tracked_exit(payload, closed_at)
        return
    with Session(engine) as session:
        _lock_symbol(session, network, symbol)
        applied = session.exec(
            select(SignalPosition)
            .where(SignalPosition.symbol == symbol)
            .where(SignalPosition.network == network)
            .where(SignalPosition.status != "open")
            .where(SignalPosition.closed_at == closed_at)
        ).first()
        if applied is not None:
            return
        position = session.exec(
            select(SignalPosition)
            .where(SignalPosition.symbol == symbol)
            .where(SignalPosition.network == network)
            .where(SignalPosition.status == "open")
            .where(SignalPosition.emitted_at <= closed_at)
            .order_by(col(SignalPosition.emitted_at).desc())
            .with_for_update()
        ).first()
        if position is None:
            session.execute(  # ty: ignore[deprecated]
                pg_insert(SignalExitPending)
                .values(
                    **SignalExitPending(
                        network=network,
                        symbol=symbol,
                        emitted_at=closed_at,
                        exit_reason=payload.get("exit_reason"),
                    ).model_dump()
                )
                .on_conflict_do_nothing(
                    index_elements=["network", "symbol", "emitted_at"]
                )
            )
            session.commit()
            return
        position.status = _closed_status(payload.get("exit_reason"))
        position.closed_at = closed_at
        position.closed_price = _price(payload.get("exit_price"))
        position.closed_by = "exit"
        session.add(position)
        session.commit()


def _record_tracked_exit(payload: dict[str, Any], closed_at: datetime) -> None:
    """Close the one ENTRY this EXIT names, at the crossing it reports.

    The agent's signal tracker (``agent/agent/signal_tracker.py``) sends the
    1m bar where the price first crossed the stop or the take and that price.
    It corrects a closure made by a view (``closed_by = "view"``, the last
    known price at read time) and is otherwise applied once: a repeated EXIT
    finds the position already closed by an EXIT. An unknown ENTRY (its own
    message was lost) is not recorded: the EXIT does not name a symbol's next
    position, unlike a strategy EXIT without ``entry_signal_id``.
    """
    with Session(engine) as session:
        position = session.exec(
            select(SignalPosition)
            .where(SignalPosition.signal_id == payload["entry_signal_id"])
            .with_for_update()
        ).first()
        if position is None:
            logger.warning(
                "EXIT for unknown ENTRY signal_id=%s, skipping",
                payload["entry_signal_id"],
            )
            return
        if position.closed_by == "exit":
            return
        position.status = _closed_status(payload.get("exit_reason"))
        position.closed_at = closed_at
        position.closed_price = _price(payload.get("exit_price"))
        position.closed_by = "exit"
        session.add(position)
        session.commit()
    logger.info(
        "closed position signal_id=%s by %s at %s price=%s",
        payload["entry_signal_id"],
        payload.get("exit_reason"),
        closed_at.isoformat(),
        payload.get("exit_price"),
    )


def _price(value: Any) -> str | None:
    """A decimal string from the payload, or ``None`` for a missing/bad one."""
    if value is None:
        return None
    try:
        return str(Decimal(str(value)))
    except InvalidOperation:
        return None


async def _current_network(redis: Redis) -> str:
    """Best-effort network label for a signal — ``Signal`` itself carries none
    (the agent's ``Engine._publish_event`` doesn't add one either), so this
    falls back to whatever the backend last told the agent to run."""
    try:
        raw = await redis.get(REDIS_RUNTIME_KEY)
    except RedisError:
        return "unknown"
    if not raw:
        return "unknown"
    try:
        return json.loads(raw).get("network") or "unknown"
    except ValueError:
        return "unknown"


class Lease:
    """How long this worker may still act as the leader.

    Set from the moment the lock was taken or renewed (the request's send
    time), so a slow answer or a stalled renewal can only shorten it. The
    listener checks it before every message: past it, another worker may
    already be leading, and this one must not consume.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._valid_until = 0.0

    def now(self) -> float:
        return self._clock()

    def extend(self, sent_at: float) -> None:
        ttl = _LEADER_TTL_MS / 1000 - _LEASE_MARGIN_SECONDS
        self._valid_until = max(self._valid_until, sent_at + ttl)

    def valid(self) -> bool:
        return self._clock() < self._valid_until


async def _handle_message(redis: Redis, raw: str) -> None:
    try:
        payload = json.loads(raw)
    except ValueError:
        logger.warning("malformed agent:events payload, dropping")
        return
    if payload.get("kind") != "signal":
        return
    kind = payload.get("type")
    record = {"ENTRY": _record_entry, "EXIT": _record_exit}.get(kind or "")
    if record is None:
        return
    try:
        network = await _current_network(redis)
        await _in_thread(record, payload, network)
    except Exception:  # noqa: BLE001 - one bad message must not kill the listener
        logger.exception("failed to process signal payload: %s", payload)


async def _in_thread(
    record: Callable[[dict[str, Any], str], None], payload: dict[str, Any], network: str
) -> None:
    """Run blocking database work in a thread, to its end.

    In a thread it cannot hold up the event loop and with it the lease
    renewal. Cancelling the caller cannot stop the thread, so a cancelled
    handler waits for it before giving up: the listener task ends — and
    leadership is released — only once no write of this worker is in flight.
    """
    work = asyncio.ensure_future(asyncio.to_thread(record, payload, network))
    try:
        await asyncio.shield(work)
    except asyncio.CancelledError:
        while not work.done():
            with suppress(asyncio.CancelledError):
                await asyncio.shield(work)
        with suppress(Exception):
            work.result()
        raise


class LeaseLost(Exception):
    """The lease ran out while listening; another worker may be leading."""


async def listen_for_signals(redis: Redis, lease: Lease | None = None) -> None:
    """Run forever, turning ENTRY/EXIT signals into DB rows as they arrive.

    With a ``lease``, no message is handled once it is no longer known to be
    valid: the listener stops (:class:`LeaseLost`) instead.
    """
    while True:
        pubsub = redis.pubsub()
        try:
            await pubsub.subscribe(REDIS_EVENTS_CHANNEL)
            logger.info("subscribed to %s for strategy signals", REDIS_EVENTS_CHANNEL)
            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                if lease is not None and not lease.valid():
                    raise LeaseLost
                await _handle_message(redis, message["data"])
        except asyncio.CancelledError:
            raise
        except RedisError:
            logger.warning(
                "signal listener lost Redis connection; retrying in %ss",
                _RECONNECT_DELAY_SECONDS,
            )
            await asyncio.sleep(_RECONNECT_DELAY_SECONDS)
        finally:
            # A worker that stops listening (lost leadership, shutdown) must
            # unsubscribe, or Redis keeps buffering messages nobody reads.
            with suppress(RedisError):
                await pubsub.aclose()  # type: ignore[no-untyped-call]


async def _renew(redis: Redis, token: str, lease: Lease) -> bool:
    """Extend the lock if it is still ours; False if lost or unanswered."""
    sent_at = lease.now()
    renew: Awaitable[int] = redis.eval(  # type: ignore[assignment] # ty: ignore[invalid-assignment]
        _RENEW_SCRIPT, 1, REDIS_LISTENER_LOCK_KEY, token, str(_LEADER_TTL_MS)
    )
    try:
        renewed = await asyncio.wait_for(renew, timeout=_LEADER_RENEW_TIMEOUT_SECONDS)
    except RedisError, TimeoutError:
        return False
    if renewed:
        lease.extend(sent_at)
    return bool(renewed)


async def _hold_leadership(
    redis: Redis, token: str, listener: asyncio.Task[None], lease: Lease
) -> None:
    """Renew the lock until it is lost or the listener ends."""
    while not listener.done():
        await asyncio.wait({listener}, timeout=_LEADER_RENEW_SECONDS)
        if listener.done():
            return
        if not await _renew(redis, token, lease):
            logger.warning("signal listener lost leadership; stepping down")
            return


async def _drain(
    redis: Redis, token: str, listener: asyncio.Task[None], lease: Lease
) -> bool:
    """Wait for the cancelled ``listener`` to end; True if it crashed.

    A write in flight runs to its end (:func:`_in_thread`), however long it
    takes; the lock is renewed meanwhile, so no other worker takes over and
    handles the next message alongside it. A lock already lost cannot be
    renewed — the symbol's ordering in the database covers that case.
    """
    while not listener.done():
        await asyncio.wait({listener}, timeout=_LEADER_RENEW_SECONDS)
        if not listener.done():
            await _renew(redis, token, lease)
    try:
        listener.result()
    except asyncio.CancelledError, LeaseLost:
        pass
    except Exception:  # noqa: BLE001 - keep electing after a crash
        logger.exception("signal listener crashed; re-electing")
        return True
    return False


async def run_signal_listener(redis: Redis) -> None:
    """Run ``listen_for_signals`` in exactly one worker at a time.

    Every worker calls this; the one holding the Redis lock listens, the rest
    retry every renew interval so one of them takes over if the leader dies.
    """
    token = uuid.uuid4().hex
    while True:
        lease = Lease()
        sent_at = lease.now()
        try:
            acquired = await redis.set(
                REDIS_LISTENER_LOCK_KEY, token, nx=True, px=_LEADER_TTL_MS
            )
        except RedisError:
            acquired = False
        if not acquired:
            await asyncio.sleep(_LEADER_RENEW_SECONDS)
            continue
        lease.extend(sent_at)
        logger.info("signal listener leadership acquired")
        listener = asyncio.create_task(listen_for_signals(redis, lease))
        try:
            await _hold_leadership(redis, token, listener, lease)
        finally:
            listener.cancel()
            crashed = await _drain(redis, token, listener, lease)
            release: Awaitable[int] = redis.eval(  # type: ignore[assignment] # ty: ignore[invalid-assignment]
                _RELEASE_SCRIPT, 1, REDIS_LISTENER_LOCK_KEY, token
            )
            # A stalled release must not keep this worker from electing
            # again; the lock expires by its TTL anyway.
            with suppress(RedisError, TimeoutError):
                await asyncio.wait_for(release, timeout=_LEADER_RENEW_TIMEOUT_SECONDS)
        if crashed:
            await asyncio.sleep(_RECONNECT_DELAY_SECONDS)
