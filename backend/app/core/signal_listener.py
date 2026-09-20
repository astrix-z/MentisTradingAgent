"""Background subscriber turning agent ENTRY/EXIT signals into DB rows.

The strategy never places real orders (D-SIGNAL-ONLY) — it only emits
``Signal`` events, published as JSON on the Redis pub/sub channel
``agent:events`` (see ``Engine._publish_event`` in ``agent/agent/engine.py``).
Pub/sub has no history: a message is lost forever if nothing is subscribed
when it fires. This module is the (only) subscriber, turning ENTRY signals
into durable ``SignalPosition`` rows the backend's own DB owns, so
``GET /trading/positions/`` has something to read regardless of when a signal
happened to fire relative to who was watching.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError
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
    with Session(engine) as session:
        existing = session.exec(
            select(SignalPosition).where(SignalPosition.signal_id == signal_id)
        ).first()
        if existing is not None:
            return  # already recorded (e.g. a redelivered pub/sub reconnect)
        session.add(
            SignalPosition(
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
        )
        session.commit()
        logger.info(
            "recorded ENTRY position symbol=%s signal_id=%s",
            payload["symbol"],
            signal_id,
        )


def _record_exit(payload: dict[str, Any]) -> None:
    """Close the open position for this symbol, if any.

    Future-proofing: the strategy does not emit EXIT today (position
    monitoring is a separate, unbuilt lane), so this path is currently dead
    code in production but keeps the listener correct if/when it starts.
    """
    with Session(engine) as session:
        position = session.exec(
            select(SignalPosition)
            .where(SignalPosition.symbol == payload["symbol"])
            .where(SignalPosition.status == "open")
            .order_by(col(SignalPosition.emitted_at).desc())
        ).first()
        if position is None:
            return
        position.status = (
            "hit_take" if payload.get("exit_reason") == "tp" else "hit_stop"
        )
        position.closed_at = datetime.fromisoformat(payload["emitted_at"])
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
        if kind == "ENTRY":
            _record_entry(payload, await _current_network(redis))
        elif kind == "EXIT":
            _record_exit(payload)
    except Exception:  # noqa: BLE001 - one bad message must not kill the listener
        logger.exception("failed to process signal payload: %s", payload)


async def listen_for_signals(redis: Redis) -> None:
    """Run forever, turning ENTRY/EXIT signals into DB rows as they arrive."""
    while True:
        try:
            pubsub = redis.pubsub()
            await pubsub.subscribe(REDIS_EVENTS_CHANNEL)
            logger.info("subscribed to %s for strategy signals", REDIS_EVENTS_CHANNEL)
            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                await _handle_message(redis, message["data"])
        except asyncio.CancelledError:
            raise
        except RedisError:
            logger.warning(
                "signal listener lost Redis connection; retrying in %ss",
                _RECONNECT_DELAY_SECONDS,
            )
            await asyncio.sleep(_RECONNECT_DELAY_SECONDS)
