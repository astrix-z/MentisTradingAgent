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
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import Session, col, select

from app.core.db import engine
from app.models import SignalPosition

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
        inserted = session.execute(statement).first() is not None
        session.commit()
    if not inserted:
        return  # already recorded by another worker or a redelivered message
    logger.info(
        "recorded ENTRY position symbol=%s signal_id=%s",
        payload["symbol"],
        signal_id,
    )


def _record_exit(payload: dict[str, Any], network: str) -> None:
    """Close the open position for this symbol on this network, if any.

    Future-proofing: the strategy does not emit EXIT today (position
    monitoring is a separate, unbuilt lane), so this path is currently dead
    code in production but keeps the listener correct if/when it starts.

    Idempotent: an EXIT is applied at most once. A position of the symbol
    already closed at this EXIT's ``emitted_at`` means a redelivery (pub/sub
    after a reconnect, or a leadership handover), which must not close the
    next open position of the same symbol.
    """
    closed_at = datetime.fromisoformat(payload["emitted_at"])
    with Session(engine) as session:
        applied = session.exec(
            select(SignalPosition)
            .where(SignalPosition.symbol == payload["symbol"])
            .where(SignalPosition.network == network)
            .where(SignalPosition.status != "open")
            .where(SignalPosition.closed_at == closed_at)
        ).first()
        if applied is not None:
            return
        position = session.exec(
            select(SignalPosition)
            .where(SignalPosition.symbol == payload["symbol"])
            .where(SignalPosition.network == network)
            .where(SignalPosition.status == "open")
            .order_by(col(SignalPosition.emitted_at).desc())
            .with_for_update()
        ).first()
        if position is None:
            return
        position.status = (
            "hit_take" if payload.get("exit_reason") == "tp" else "hit_stop"
        )
        position.closed_at = closed_at
        session.add(position)
        session.commit()


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
    try:
        # The database work is blocking: in a thread, it cannot hold up the
        # event loop and with it the lease renewal.
        if kind == "ENTRY":
            network = await _current_network(redis)
            await asyncio.to_thread(_record_entry, payload, network)
        elif kind == "EXIT":
            network = await _current_network(redis)
            await asyncio.to_thread(_record_exit, payload, network)
    except Exception:  # noqa: BLE001 - one bad message must not kill the listener
        logger.exception("failed to process signal payload: %s", payload)


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


async def _hold_leadership(
    redis: Redis, token: str, listener: asyncio.Task[None], lease: Lease
) -> None:
    """Renew the lock until it is lost or the listener ends."""
    while not listener.done():
        await asyncio.wait({listener}, timeout=_LEADER_RENEW_SECONDS)
        if listener.done():
            return
        sent_at = lease.now()
        renew: Awaitable[int] = redis.eval(  # type: ignore[assignment]
            _RENEW_SCRIPT, 1, REDIS_LISTENER_LOCK_KEY, token, str(_LEADER_TTL_MS)
        )
        renewed: int
        try:
            renewed = await asyncio.wait_for(
                renew, timeout=_LEADER_RENEW_TIMEOUT_SECONDS
            )
        except RedisError, TimeoutError:
            renewed = 0
        if renewed:
            lease.extend(sent_at)
        if not renewed:
            logger.warning("signal listener lost leadership; stepping down")
            return


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
            crashed = False
            try:
                await listener
            except asyncio.CancelledError, LeaseLost:
                pass
            except Exception:  # noqa: BLE001 - keep electing after a crash
                logger.exception("signal listener crashed; re-electing")
                crashed = True
            release: Awaitable[int] = redis.eval(  # type: ignore[assignment]
                _RELEASE_SCRIPT, 1, REDIS_LISTENER_LOCK_KEY, token
            )
            # A stalled release must not keep this worker from electing
            # again; the lock expires by its TTL anyway.
            with suppress(RedisError, TimeoutError):
                await asyncio.wait_for(release, timeout=_LEADER_RENEW_TIMEOUT_SECONDS)
        if crashed:
            await asyncio.sleep(_RECONNECT_DELAY_SECONDS)
