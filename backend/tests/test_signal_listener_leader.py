"""Only one uvicorn worker listens to agent:events at a time.

Each worker's lifespan starts ``run_signal_listener``; a Redis lock picks the
one that actually subscribes. Redis is modelled in memory with just the calls
the election makes (SET NX, the renew/release scripts, pubsub).
"""

from __future__ import annotations

import asyncio
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
