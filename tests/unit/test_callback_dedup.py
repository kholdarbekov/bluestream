"""Unit tests for the callback-dedup middleware.

Locks in the root-cause fix for the production "Message to edit not found" /
"Message to delete not found" warning pair: that pair is what surfaces when
a duplicate callback (double-tap or Telegram redelivery) reaches a handler
whose first invocation already delete-and-replaced the source message. The
middleware short-circuits the duplicate at dispatch time so the second
handler invocation never runs.

These tests don't spin up the full PTB Application — they import
``callback_dedup`` directly with stubbed module dependencies so we can
assert: (1) the dedup predicates hold for the exact callback shape that
caused the production warnings; (2) ``ApplicationHandlerStop`` is raised on
the duplicate; (3) ``query.answer()`` is called on every tap including
duplicates so the spinner dismisses; (4) the in-memory fallback behaves
correctly when Redis is unavailable.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BOT_DIR = REPO_ROOT / "telegram_bot"


# ---------------------------------------------------------------------------
# Module loading. ``handlers.callback_dedup`` depends on
# ``shared.redis_keyspace`` and ``shared.redis_failure``, both of which exist
# in the repo and import cleanly without bot runtime — so we just need to
# ensure ``shared/`` is importable, then load the module by file path.
# ---------------------------------------------------------------------------


_REPO_ROOT_STR = str(REPO_ROOT)
if _REPO_ROOT_STR not in sys.path:
    sys.path.insert(0, _REPO_ROOT_STR)


def _load_dedup_module():
    cached = sys.modules.get("telegram_bot_callback_dedup_for_tests")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(
        "telegram_bot_callback_dedup_for_tests",
        BOT_DIR / "handlers" / "callback_dedup.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["telegram_bot_callback_dedup_for_tests"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def dedup_mod():
    return _load_dedup_module()


@pytest.fixture(autouse=True)
def _clear_in_memory(dedup_mod):
    """Each test starts with an empty in-memory lock dict so there's no
    cross-test contamination of dedup state."""
    dedup_mod._in_memory_locks.clear()
    yield
    dedup_mod._in_memory_locks.clear()


# ---------------------------------------------------------------------------
# Helpers to fabricate Update / Context fakes that exercise the middleware
# without pulling in PTB's full Application machinery.
# ---------------------------------------------------------------------------


def _make_update(*, user_id: int = 90801796, callback_data: str = "add_to_cart_1"):
    update = MagicMock()
    update.callback_query = MagicMock()
    update.callback_query.data = callback_data
    update.callback_query.answer = AsyncMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    return update


def _make_context(*, redis_client=None):
    """Build a minimal ContextTypes-shaped object. ``token_manager`` lives
    on ``bot_data`` exactly like the real bot wires it (see bot.py:166)."""
    context = MagicMock()
    if redis_client is None:
        context.bot_data = {}
    else:
        token_manager = MagicMock()
        token_manager.redis = redis_client
        context.bot_data = {"token_manager": token_manager}
    return context


# ---------------------------------------------------------------------------
# Predicate-level tests
# ---------------------------------------------------------------------------


def test_hash_is_stable_and_bounded(dedup_mod):
    digest = dedup_mod._hash_callback_data("add_to_cart_1")
    assert len(digest) == dedup_mod._DATA_DIGEST_LEN
    assert digest == dedup_mod._hash_callback_data("add_to_cart_1")
    assert digest != dedup_mod._hash_callback_data("add_to_cart_2")


def test_in_memory_lock_first_claim_wins(dedup_mod):
    assert dedup_mod._claim_in_memory("k") is True
    assert dedup_mod._claim_in_memory("k") is False, (
        "Second claim within the TTL must fail — this is the dedup"
    )
    assert dedup_mod._claim_in_memory("other") is True


def test_in_memory_lock_expires(dedup_mod, monkeypatch):
    """After the TTL elapses the lock must release so deliberate re-taps work."""
    fake_now = [1000.0]

    def fake_monotonic():
        return fake_now[0]

    monkeypatch.setattr(dedup_mod.time, "monotonic", fake_monotonic)
    assert dedup_mod._claim_in_memory("k") is True
    fake_now[0] += dedup_mod._DEDUP_TTL_SECONDS + 0.1  # past TTL
    assert dedup_mod._claim_in_memory("k") is True, (
        "Lock must release after TTL or deliberate re-taps would feel broken"
    )


# ---------------------------------------------------------------------------
# Middleware behaviour
# ---------------------------------------------------------------------------


def test_first_tap_is_acked_and_passes_through(dedup_mod):
    """First tap of a button: ack, claim lock, let the handler run (i.e.
    middleware returns normally without raising ApplicationHandlerStop)."""
    update = _make_update()
    context = _make_context()

    # No exception ⇒ dispatch continues to real handlers.
    asyncio.run(dedup_mod.callback_dedup_middleware(update, context))

    update.callback_query.answer.assert_awaited_once_with()
    assert any(
        "callback_dedup" in k for k in dedup_mod._in_memory_locks.keys()
    ), "First tap must have claimed an in-memory lock"


def test_duplicate_tap_is_dropped(dedup_mod):
    """Second tap of the same button within the window: ack again (so
    spinner dismisses), then raise ApplicationHandlerStop so no handler
    runs against the (now-stale) message_id."""
    from telegram.ext import ApplicationHandlerStop

    context = _make_context()

    # Tap 1 — passes through
    asyncio.run(dedup_mod.callback_dedup_middleware(_make_update(), context))

    # Tap 2 — same user + same callback_data within TTL ⇒ duplicate
    update2 = _make_update()
    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(dedup_mod.callback_dedup_middleware(update2, context))

    # Critical: even the duplicate gets acked. If we didn't ack, Telegram
    # would leave the loading spinner up on the user's button — the very
    # symptom that caused users to retap in the first place.
    update2.callback_query.answer.assert_awaited_once_with()


def test_different_buttons_dont_collide(dedup_mod):
    """Same user tapping two different buttons in quick succession must
    *not* be deduped — each button is a distinct intent."""
    context = _make_context()

    asyncio.run(dedup_mod.callback_dedup_middleware(
        _make_update(callback_data="add_to_cart_1"), context,
    ))
    # Different callback_data ⇒ different lock key ⇒ not a duplicate.
    asyncio.run(dedup_mod.callback_dedup_middleware(
        _make_update(callback_data="add_to_cart_2"), context,
    ))


def test_different_users_dont_collide(dedup_mod):
    """Two users tapping the same button at the same instant must each be
    let through — the lock is keyed on user_id."""
    context = _make_context()

    asyncio.run(dedup_mod.callback_dedup_middleware(
        _make_update(user_id=111), context,
    ))
    asyncio.run(dedup_mod.callback_dedup_middleware(
        _make_update(user_id=222), context,
    ))


def test_non_callback_updates_pass_through_untouched(dedup_mod):
    """Pre-checkout queries, plain messages, etc. must not be deduped or acked."""
    update = MagicMock()
    update.callback_query = None  # not a callback_query update
    update.effective_user = MagicMock(id=42)
    context = _make_context()

    # Should return without raising and without touching any answer/lock state.
    asyncio.run(dedup_mod.callback_dedup_middleware(update, context))
    assert dedup_mod._in_memory_locks == {}


def test_callback_without_data_is_ignored(dedup_mod):
    """A callback_query with empty data shouldn't be deduped (no stable key)."""
    update = _make_update(callback_data="")
    context = _make_context()
    asyncio.run(dedup_mod.callback_dedup_middleware(update, context))
    update.callback_query.answer.assert_not_awaited()
    assert dedup_mod._in_memory_locks == {}


def test_redis_path_used_when_available(dedup_mod):
    """When token_manager.redis is configured, the middleware should use
    SET NX EX rather than the in-memory dict — so the lock survives across
    bot replicas."""
    fake_redis = MagicMock()
    fake_redis.set = AsyncMock(return_value=True)  # claim succeeds
    context = _make_context(redis_client=fake_redis)
    update = _make_update()

    asyncio.run(dedup_mod.callback_dedup_middleware(update, context))

    fake_redis.set.assert_awaited_once()
    args, kwargs = fake_redis.set.call_args
    # Key is the keyspace-namespaced one (so ops dashboards see it).
    assert args[0].startswith("bot:callback_dedup:")
    # SET NX EX is the atomic claim primitive.
    assert kwargs.get("nx") is True
    assert kwargs.get("ex") == dedup_mod._DEDUP_TTL_SECONDS

    # In-memory dict should be untouched when Redis succeeds — otherwise we'd
    # be doing double the work (and double the dedup effect).
    assert dedup_mod._in_memory_locks == {}


def test_redis_duplicate_raises_handler_stop(dedup_mod):
    """SET NX returning falsy means another replica already claimed — drop."""
    from telegram.ext import ApplicationHandlerStop

    fake_redis = MagicMock()
    fake_redis.set = AsyncMock(return_value=None)  # NX failed ⇒ duplicate
    context = _make_context(redis_client=fake_redis)
    update = _make_update()

    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(dedup_mod.callback_dedup_middleware(update, context))

    update.callback_query.answer.assert_awaited_once_with()


def test_redis_failure_falls_back_to_in_memory(dedup_mod, caplog):
    """If Redis is unreachable, the middleware must keep working (degraded
    to single-replica) rather than failing the whole dispatch. The Sentry
    alert via report_redis_failure makes the degradation observable."""
    fake_redis = MagicMock()
    fake_redis.set = AsyncMock(side_effect=RuntimeError("redis down"))
    context = _make_context(redis_client=fake_redis)

    # Tap 1 falls through Redis failure to the in-memory path.
    asyncio.run(dedup_mod.callback_dedup_middleware(_make_update(), context))
    assert len(dedup_mod._in_memory_locks) == 1, (
        "Redis failure must cause the in-memory fallback to take ownership"
    )

    # Tap 2 against the in-memory path catches the duplicate.
    from telegram.ext import ApplicationHandlerStop
    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(dedup_mod.callback_dedup_middleware(_make_update(), context))


def test_query_answer_failure_does_not_break_dispatch(dedup_mod, caplog):
    """If query.answer() raises (e.g. 'Query is too old'), the middleware
    must continue with dedup rather than failing the dispatch — Telegram's
    error here is purely cosmetic."""
    update = _make_update()
    update.callback_query.answer = AsyncMock(side_effect=Exception("Query is too old"))
    context = _make_context()

    with caplog.at_level(logging.DEBUG, logger=dedup_mod.logger.name):
        asyncio.run(dedup_mod.callback_dedup_middleware(update, context))

    # Lock was still claimed despite the answer failure.
    assert len(dedup_mod._in_memory_locks) == 1
