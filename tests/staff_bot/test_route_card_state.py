"""Card state must be shared between PTB handlers and the webhook server:
module-level locks (one per driver, stable identity) and Redis-backed
persistence with a borrowed flag. Redis is the single source of truth (no
in-memory fallback -- see route_card_state module docstring for why a prior
revision's `_memory` layer was a split-brain hazard). asyncio.run + fakes,
no real Redis."""

import asyncio
import json

import pytest

from shared.redis_keyspace import KEYSPACE_TIERS, RedisKeyspace
from staff_bot.utils import route_card_state


class _FakeRedis:
    """Just enough of redis.asyncio for set/get/delete with ex=."""

    def __init__(self):
        self.store = {}
        self.ttls = {}

    async def set(self, key, value, ex=None):
        self.store[key] = value
        self.ttls[key] = ex

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        self.store.pop(key, None)


@pytest.fixture(autouse=True)
def _reset_module():
    route_card_state.configure(None)
    route_card_state._locks.clear()
    yield
    route_card_state.configure(None)
    route_card_state._locks.clear()


@pytest.mark.unit
class TestKeyspace:
    def test_key_format_and_tier(self):
        assert RedisKeyspace.staff_bot_route_card(777) == "staff_bot:route_card:777"
        assert KEYSPACE_TIERS["staff_bot_route_card"].name == "TIER_CACHE"


@pytest.mark.unit
class TestLocks:
    def test_same_driver_same_lock_instance(self):
        assert route_card_state.get_lock(1) is route_card_state.get_lock(1)

    def test_different_drivers_different_locks(self):
        assert route_card_state.get_lock(1) is not route_card_state.get_lock(2)


@pytest.mark.unit
class TestRedisUnconfigured:
    """Redis is the single source of truth. With no client installed
    (configure(None), e.g. TokenManager failed to connect at startup),
    every operation must degrade to a safe no-op/None -- never raise, and
    never fall back to any process-local store."""

    def test_load_returns_none_without_redis(self):
        async def run():
            assert await route_card_state.load(7) is None

        asyncio.run(run())

    def test_save_is_noop_without_redis(self):
        async def run():
            # Must not raise. Nothing to assert it wrote anywhere -- there
            # is no fallback store to inspect, which is the point.
            await route_card_state.save(7, {"chat_id": 7, "message_id": 10, "view": "next"})
            assert await route_card_state.load(7) is None

        asyncio.run(run())

    def test_clear_is_noop_without_redis(self):
        async def run():
            await route_card_state.clear(7)  # must not raise

        asyncio.run(run())

    def test_mark_borrowed_is_noop_without_redis(self):
        async def run():
            await route_card_state.mark_borrowed(7)  # must not raise
            assert await route_card_state.load(7) is None

        asyncio.run(run())


@pytest.mark.unit
class TestLockSerializesConcurrentWriters:
    """The whole point of `get_lock`: the PTB-handler path (driver tap) and
    the webhook-server path (backend push) run in the SAME process/event
    loop and can fire for the same driver at the same instant. Without one
    shared lock per driver, their read-modify-write critical sections
    interleave -> lost message id / duplicate card. A test that only checks
    `get_lock(x) is get_lock(x)` would not catch a regression where the lock
    is acquired but not actually awaited around the critical section, so we
    drive real overlapping coroutines and inspect the interleaving of their
    start/end markers.

    Each worker calls `route_card_state.get_lock(42)` ITSELF (not hoisted
    into `run()`) so the test exercises the real call path both entry points
    use: fetch-the-shared-lock-then-acquire-it, not acquire-a-lock-handed-
    to-you. Hoisting the call let a `get_lock` that returns a fresh Lock
    every time still pass (caught only by the identity test, per review).
    """

    @staticmethod
    def _no_interleaving(events):
        """True iff every `<name>-start` is immediately followed by that
        SAME name's `-end` before any other name's `-start` appears --
        i.e. critical sections never overlapped."""
        i = 0
        while i < len(events):
            if i + 1 >= len(events):
                return False
            name = events[i].split("-", 1)[0]
            if events[i] != f"{name}-start" or events[i + 1] != f"{name}-end":
                return False
            i += 2
        return True

    def test_shared_lock_serializes_two_writers_on_same_driver(self):
        events = []

        async def worker(name, hold_seconds):
            lock = route_card_state.get_lock(42)
            # A grabs the lock first and holds it across an await; B fires
            # "at the same moment" (webhook landing mid-tap) and must queue
            # behind A instead of running its critical section concurrently.
            async with lock:
                events.append(f"{name}-start")
                await asyncio.sleep(hold_seconds)
                events.append(f"{name}-end")

        async def run():
            await asyncio.gather(
                worker("A", 0.05),
                worker("B", 0.0),
            )

        asyncio.run(run())

        assert len(events) == 4
        assert self._no_interleaving(events), events

    def test_control_without_shared_lock_the_same_scenario_interleaves(self):
        """Negative control: proves the assertion above is a real test of
        serialization, not a tautology that always passes regardless of
        locking. Using two INDEPENDENT locks (i.e. no shared per-driver
        lock, the bug this task prevents) the same timing reliably produces
        an interleaved trace."""
        events = []

        async def worker(name, lock, hold_seconds):
            async with lock:
                events.append(f"{name}-start")
                await asyncio.sleep(hold_seconds)
                events.append(f"{name}-end")

        async def run():
            await asyncio.gather(
                worker("A", asyncio.Lock(), 0.05),
                worker("B", asyncio.Lock(), 0.0),
            )

        asyncio.run(run())

        assert events == ["A-start", "B-start", "B-end", "A-end"]
        assert not self._no_interleaving(events)


@pytest.mark.unit
class TestRedisBacked:
    def test_round_trip_uses_keyspace_and_ttl(self):
        fake = _FakeRedis()
        route_card_state.configure(fake)

        async def run():
            await route_card_state.save(777, {"chat_id": 777, "message_id": 5, "view": "all"})
            assert RedisKeyspace.staff_bot_route_card(777) in fake.store
            assert fake.ttls[RedisKeyspace.staff_bot_route_card(777)] == route_card_state.STATE_TTL_SECONDS
            loaded = await route_card_state.load(777)
            assert loaded == {"chat_id": 777, "message_id": 5, "view": "all"}

        asyncio.run(run())

    def test_mark_borrowed_sets_view(self):
        fake = _FakeRedis()
        route_card_state.configure(fake)

        async def run():
            # No state yet -> no crash, still nothing stored.
            await route_card_state.mark_borrowed(7)
            assert await route_card_state.load(7) is None
            await route_card_state.save(7, {"chat_id": 7, "message_id": 10, "view": "next"})
            await route_card_state.mark_borrowed(7)
            assert (await route_card_state.load(7))["view"] == route_card_state.VIEW_BORROWED

        asyncio.run(run())

    def test_clear_removes_the_key(self):
        fake = _FakeRedis()
        route_card_state.configure(fake)

        async def run():
            await route_card_state.save(7, {"chat_id": 7, "message_id": 10, "view": "next"})
            await route_card_state.clear(7)
            assert await route_card_state.load(7) is None
            assert RedisKeyspace.staff_bot_route_card(7) not in fake.store

        asyncio.run(run())

    def test_redis_failure_returns_none_and_does_not_raise(self):
        """No in-memory fallback: if Redis errors on save, the state is
        simply not persisted (never a stale/split value silently reappearing
        on a later successful load -- see module docstring for the
        split-brain this replaces)."""

        class _Boom:
            async def set(self, *a, **k):
                raise RuntimeError("redis down")

            async def get(self, *a, **k):
                raise RuntimeError("redis down")

            async def delete(self, *a, **k):
                raise RuntimeError("redis down")

        route_card_state.configure(_Boom())

        async def run():
            await route_card_state.save(9, {"chat_id": 9, "message_id": 1, "view": "next"})  # must not raise
            assert await route_card_state.load(9) is None  # nothing to fall back to
            await route_card_state.clear(9)  # must not raise
            await route_card_state.mark_borrowed(9)  # must not raise (load inside returns None)

        asyncio.run(run())


@pytest.mark.unit
class TestRestartSurvival:
    def test_load_reads_from_redis_with_no_process_local_state(self):
        """Simulates a bot restart mid-shift: a previous process wrote the
        card state to Redis, then died. The new process starts with zero
        process-local state and must still recover the SAME card via
        `load` alone -- this is the entire reason state lives in Redis
        instead of `context.user_data`. Seeds Redis directly (bypassing
        `route_card_state.save`) so nothing in this process ever held the
        value first; must fail if `load` stops reading Redis (e.g. a future
        cache layer reintroduces the split-brain flagged in review)."""
        fake = _FakeRedis()
        seeded = {
            "chat_id": 555,
            "message_id": 42,
            "view": "next",
            "card_date": "2026-08-11",
        }
        fake.store[RedisKeyspace.staff_bot_route_card(555)] = json.dumps(seeded)

        route_card_state.configure(fake)

        async def run():
            return await route_card_state.load(555)

        loaded = asyncio.run(run())
        assert loaded == seeded
