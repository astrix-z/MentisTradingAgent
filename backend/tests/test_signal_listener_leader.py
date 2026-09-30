"""Only one uvicorn worker listens to agent:events at a time.

Each worker's lifespan starts ``run_signal_listener``; a Redis lock picks the
one that actually subscribes. Redis is modelled in memory with just the calls
the election makes (SET NX, the renew/release scripts, pubsub).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from app.core import signal_listener as sl


class LockRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.pubsubs: list[FakePubSub] = []

    async def set(self, key: str, value: str, nx: bool, px: int) -> bool:
        assert nx and px == sl._LEADER_TTL_MS
        if key in self.values:
            return False
        self.values[key] = value
        return True

    async def eval(self, script: str, _numkeys: int, key: str, *args: str) -> int:
        if self.values.get(key) != args[0]:
            return 0
        if script == sl._RELEASE_SCRIPT:
            del self.values[key]
        return 1

    def pubsub(self) -> FakePubSub:
        pubsub = FakePubSub()
        self.pubsubs.append(pubsub)
        return pubsub


class FakePubSub:
    def __init__(self) -> None:
        self.subscribed: list[str] = []
        self.closed = False

    async def subscribe(self, channel: str) -> None:
        self.subscribed.append(channel)

    async def listen(self) -> Any:
        await asyncio.Event().wait()  # no messages; blocks until cancelled
        yield {}

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def fast_election(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sl, "_LEADER_RENEW_SECONDS", 0.01)


async def _settle() -> None:
    await asyncio.sleep(0.05)


def _listening(redis: LockRedis) -> list[FakePubSub]:
    return [p for p in redis.pubsubs if p.subscribed and not p.closed]


def test_one_of_four_workers_listens_and_another_takes_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Worker i draws token "w<i>" (tasks start in creation order), so the lock
    # value names the leader.
    tokens = iter(f"w{i}" for i in range(4))
    monkeypatch.setattr(
        sl.uuid, "uuid4", lambda: type("U", (), {"hex": next(tokens)})()
    )

    async def scenario() -> None:
        redis = LockRedis()
        workers = [
            asyncio.create_task(sl.run_signal_listener(redis))  # type: ignore[arg-type]
            for _ in range(4)
        ]
        await _settle()
        assert len(_listening(redis)) == 1
        leader_token = redis.values[sl.REDIS_LISTENER_LOCK_KEY]

        # Shutting the leader down unsubscribes it and releases the lock, so
        # one of the remaining workers takes over on its next retry.
        workers[int(leader_token[1:])].cancel()
        await _settle()
        assert len(_listening(redis)) == 1
        assert redis.values[sl.REDIS_LISTENER_LOCK_KEY] != leader_token

        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        assert _listening(redis) == []
        assert sl.REDIS_LISTENER_LOCK_KEY not in redis.values

    asyncio.run(scenario())


def test_leader_steps_down_when_its_lock_is_gone() -> None:
    async def scenario() -> None:
        redis = LockRedis()
        worker = asyncio.create_task(sl.run_signal_listener(redis))  # type: ignore[arg-type]
        await _settle()
        assert len(_listening(redis)) == 1
        first = _listening(redis)[0]

        # The lock expired and another process took it: renewing fails, so this
        # worker must stop listening rather than run alongside the new leader.
        redis.values[sl.REDIS_LISTENER_LOCK_KEY] = "someone-else"
        await _settle()
        assert first.closed
        assert _listening(redis) == []
        assert redis.values[sl.REDIS_LISTENER_LOCK_KEY] == "someone-else"

        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(scenario())


class ExpiringRedis:
    """A lock with a real TTL, renewals that can be made to hang, and
    pub/sub that delivers a published message to every subscriber."""

    def __init__(self) -> None:
        self.values: dict[str, tuple[str, float]] = {}
        self.hang: set[str] = set()  # tokens whose calls never answer (stalled)
        self.queues: list[tuple[asyncio.Queue[Any], BroadcastPubSub]] = []

    def _live(self, key: str) -> str | None:
        entry = self.values.get(key)
        if entry is None or entry[1] <= time.monotonic():
            self.values.pop(key, None)
            return None
        return entry[0]

    async def set(self, key: str, value: str, nx: bool, px: int) -> bool:
        if value in self.hang:
            await asyncio.Event().wait()
        if self._live(key) is not None:
            return False
        self.values[key] = (value, time.monotonic() + px / 1000)
        return True

    async def eval(self, script: str, _numkeys: int, key: str, *args: str) -> int:
        if args[0] in self.hang:
            await asyncio.Event().wait()  # the call never comes back
        if self._live(key) != args[0]:
            return 0
        if script == sl._RELEASE_SCRIPT:
            del self.values[key]
        else:
            self.values[key] = (args[0], time.monotonic() + int(args[1]) / 1000)
        return 1

    def pubsub(self) -> BroadcastPubSub:
        return BroadcastPubSub(self)

    def publish(self, data: str) -> None:
        for queue, pubsub in self.queues:
            if not pubsub.closed:
                queue.put_nowait({"type": "message", "data": data})


class BroadcastPubSub:
    def __init__(self, redis: ExpiringRedis) -> None:
        self.redis = redis
        self.closed = False
        self.queue: asyncio.Queue[Any] = asyncio.Queue()

    async def subscribe(self, channel: str) -> None:
        self.redis.queues.append((self.queue, self))

    async def listen(self) -> Any:
        while True:
            yield await self.queue.get()

    async def aclose(self) -> None:
        self.closed = True


def test_a_stalled_renewal_never_lets_two_workers_consume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Review 2026-09-30: A's renewal hung, its lease expired, B took over,
    # and a broadcast signal was handled by both.
    monkeypatch.setattr(sl, "_LEADER_TTL_MS", 300)
    monkeypatch.setattr(sl, "_LEASE_MARGIN_SECONDS", 0.05)
    monkeypatch.setattr(sl, "_LEADER_RENEW_SECONDS", 0.05)
    monkeypatch.setattr(sl, "_LEADER_RENEW_TIMEOUT_SECONDS", 0.1)
    tokens = iter(["A", "B"])
    monkeypatch.setattr(
        sl.uuid, "uuid4", lambda: type("U", (), {"hex": next(tokens)})()
    )
    handled: list[str] = []

    async def handle(_redis: Any, raw: str) -> None:
        handled.append(raw)

    monkeypatch.setattr(sl, "_handle_message", handle)

    async def scenario() -> None:
        redis = ExpiringRedis()
        a = asyncio.create_task(sl.run_signal_listener(redis))  # type: ignore[arg-type]
        await asyncio.sleep(0.02)
        assert redis._live(sl.REDIS_LISTENER_LOCK_KEY) == "A"
        redis.hang.add("A")  # A's connection stalls: no call of A answers
        b = asyncio.create_task(sl.run_signal_listener(redis))  # type: ignore[arg-type]
        await asyncio.sleep(0.5)  # A's lease runs out; B takes the lock
        assert redis._live(sl.REDIS_LISTENER_LOCK_KEY) == "B"
        redis.publish('{"kind": "signal"}')
        await asyncio.sleep(0.05)
        assert handled == ['{"kind": "signal"}']  # once, by B
        for worker in (a, b):
            worker.cancel()
        await asyncio.gather(a, b, return_exceptions=True)

    asyncio.run(scenario())


def test_the_lease_counts_from_when_the_renewal_was_sent() -> None:
    clock = [100.0]
    lease = sl.Lease(clock=lambda: clock[0])
    assert not lease.valid()
    sent_at = clock[0]
    clock[0] += 5  # the answer took 5 s
    lease.extend(sent_at)
    ttl = sl._LEADER_TTL_MS / 1000 - sl._LEASE_MARGIN_SECONDS
    clock[0] = sent_at + ttl - 0.001
    assert lease.valid()
    clock[0] = sent_at + ttl
    assert not lease.valid()
