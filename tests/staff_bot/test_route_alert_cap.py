"""Task 9: the head-change alert -- restyled, capped, newest-supersedes.

This is the ONLY sounded message the route-card plan sends (Task 8 owns the
silent branch, measured elsewhere at 0 send / 1 edit). Outside
ROUTE_ALERT_MIN_INTERVAL_SECONDS it pings; inside the window it must still be
visible but silent, and the previous alert is deleted so the chat never
accumulates a stack of superseded "next stop changed" notices. The card
refresh itself is delegated to Task 8's `update_card_for_driver` -- NOT
re-implemented here (CLAUDE.md SSOT) -- so most tests here mock that
delegation boundary except where a test's whole point is to prove the
delegate is actually reached correctly (fix round 1: `TestAlertCardRefreshDelegation`;
fix round 2: `TestAlertStateRaceSafety`).

Naming note: the brief (written ahead of Task 8 landing) called the
card-refresh dependency `refresh_card_for_driver`. Task 8's own report
records deviating to `update_card_for_driver` as a deliberate improvement
(reuses existing token/fetch/render machinery). These tests target the name
that actually exists.

Fix round 1: `send_head_change_alert` takes `bot_app` (an Application-shaped
object exposing `.bot` and `.bot_data`), not a raw Bot -- `update_card_for_driver`
needs `.bot_data` for its token-manager lookup and `.bot` for its own
Telegram calls, neither of which a raw `telegram.Bot` has. `_bot_app()`
below wraps a mock Bot the same way the real webhook server's `server.bot_app`
does.

Fix round 2: three changes to this file worth flagging up front.
  1. The `_state` fixture's fake `load`/`save` now hand back/store COPIES,
     not the same aliased dict object, matching the real
     `route_card_state.load`/`save`'s JSON-serialize-on-write-and-read
     semantics (`json.loads`/`json.dumps` always produce a fresh object).
     Without this, a "concurrent" mutation injected by one coroutine's
     side effect would alias directly into another coroutine's already-loaded
     local `state` variable in this test double, which cannot happen against
     real Redis -- and would make the race-safety test below meaningless
     (it would pass even against the OLD buggy code, since the aliasing
     would silently "fix" the clobber for the wrong reason).
  2. Message ids across every test that exercises the REAL `update_card_for_driver`
     are now realistic and monotonic: an existing card's id is always LOWER
     than anything freshly sent afterward (real Telegram message ids are
     monotonic per chat). The original fixture values (card `message_id=900`,
     alert send returning `100`) are impossible on Telegram and hid exactly
     the class of bug fix round 2 found: `render_route_card`'s repost
     heuristic (`reference_message_id - state["message_id"] > gap`) is
     negative and never fires when the "new" id is numerically smaller than
     the "old" one, so a regression reintroducing `reference_message_id`
     threading would have kept passing.
  3. `_frozen_clock` / `_redis_unconfigured` fixtures added so
     `_FrozenDateTime._frozen` and `route_card_state`'s global `_redis`
     always get explicit teardown instead of being left set/cleared with no
     restore, which is an isolation smell under `pytest -n auto`.
"""
import asyncio
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from staff_bot.handlers.delivery import route_card
from staff_bot.i18n import i18n
from staff_bot.utils import route_card_state

# Captured at import time, before any per-test monkeypatching -- lets the
# borrowed-card, race-safety and Redis-down tests drive the REAL
# update_card_for_driver / route_card_state.load / route_card_state.save
# instead of the autouse fixture's fakes, without reaching into another test
# file's fixtures (per-file fixtures is the project convention).
_REAL_UPDATE_CARD_FOR_DRIVER = route_card.update_card_for_driver
_REAL_STATE_LOAD = route_card_state.load
_REAL_STATE_SAVE = route_card_state.save

# Route-card copy is DB-backed (Task 2 seeded scripts/seed_staff_translations.py
# under category='staff_bot'). Loading the seed script by path and resolving
# through `_curated_value` -- the SAME function seed_translations() calls to
# decide what actually gets written to Postgres -- so a copy edit there is
# reflected here automatically instead of a hand-pasted string quietly going
# stale (CLAUDE.md: never let a test re-implement production logic). Same
# technique as tests/staff_bot/test_route_card_views.py.
_SEED_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "seed_staff_translations.py"


def _load_seed_module():
    spec = importlib.util.spec_from_file_location("seed_staff_translations", _SEED_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SEED_MODULE = _load_seed_module()
_ALERT_KEYS = ["staff.route.head_changed_alert", "staff.route.open_route_card"]


def _bot_app(bot, bot_data=None):
    """Minimal Application-shaped stand-in: `.bot` for direct Telegram calls
    (what `send_head_change_alert` itself makes), `.bot_data` for
    `update_card_for_driver`'s token-manager lookup. Mirrors how
    `staff_bot/webhook_server.py`'s `server.bot_app` is actually shaped."""
    app = MagicMock()
    app.bot = bot
    app.bot_data = bot_data if bot_data is not None else {}
    return app


def _delivery_payload():
    return {
        "items": [{
            "delivery_id": 11, "order_number": "101", "status": "assigned",
            "customer_name": "U", "customer_phone": "+998900000001",
            "district": "Chilanzar", "address": "Street 1",
            "items": [], "total_amount": 10000, "payment_method": "cash",
            "amount_collected": 0, "outstanding_amount": 10000,
            "expected_cash_to_collect": 10000, "cod_reserved_prepayment_amount": 0,
            "destination_latitude": 41.31, "destination_longitude": 69.27,
            "route_position": 0, "is_next": True,
            "eta_minutes_from_current_location": None, "distance_km_to_next": None,
        }],
        "total": 1, "location_status": "fresh",
        "route_summary": {
            "remaining": 1, "stops_completed_today": 0, "stops_total_today": 1,
            "committed_delivery_id": None, "finish_eta": None, "updated_at": None,
        },
    }


class _ApiClientStub:
    """Just enough of `staff_bot.api_client.api_client`'s async-context-manager
    shape for `update_card_for_driver`'s `async with api_client as client:`."""

    def __init__(self, payload, success=True):
        self.client = MagicMock()
        self.client.get_active_deliveries = AsyncMock(
            return_value=MagicMock(success=success, data=payload)
        )

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, *a):
        return False


class _FrozenDateTime(datetime):
    """A `datetime` subclass whose `.now()` returns a fixed instant.

    Only `now` is overridden; `fromisoformat`, arithmetic, `.replace`, etc.
    are all inherited from the real `datetime` and behave normally (verified
    empirically: `fromisoformat` on a subclass returns an instance of that
    subclass via `cls(...)`, so the module's `last.tzinfo is None` /
    `.replace(tzinfo=...)` branch and the subtraction below all still work).
    Used to pin the exactly-at-the-cap-boundary test to a real instant
    instead of a wall-clock race against `datetime.now()`.
    """
    _frozen = None

    @classmethod
    def now(cls, tz=None):
        return cls._frozen


@pytest.fixture(autouse=True)
def _state(monkeypatch):
    """Fix round 2: `load`/`save` now hand back/store COPIES of the state
    dict, not the same aliased object -- see the module docstring's fix
    round 2 note 1. This matters for any test that injects a "concurrent"
    mutation via a side effect: without copying, that mutation would alias
    directly into a coroutine's already-loaded local `state`, silently
    "fixing" a clobber that would be real against actual Redis.

    FINAL review, C1/I2: also clears `route_card_state._locks` before and
    after every test, matching the established pattern in
    tests/staff_bot/test_route_card_render.py's `_reset_state` and
    tests/staff_bot/test_route_card_borrowed.py's `_reset_state`. This file
    didn't need it before -- every prior test's `get_lock(555).acquire()`
    happened to be uncontended (the fast, non-`_get_loop()`-touching path).
    `asyncio.Lock` only binds to a running event loop the first time it
    actually contends (the slow path); reusing that SAME Lock object from a
    later test's own `asyncio.run()` (a different loop) then raises
    "bound to a different event loop". The C1 and I2 regression tests in
    this file are the first here to genuinely contend telegram_id 555's
    lock across separate tests, so leaving stale entries in `_locks` between
    tests is no longer safe."""
    route_card_state._locks.clear()
    store = {}

    async def load(telegram_id):
        state = store.get(telegram_id)
        return dict(state) if state is not None else None

    async def save(telegram_id, state):
        store[telegram_id] = dict(state)

    monkeypatch.setattr(route_card_state, "load", load)
    monkeypatch.setattr(route_card_state, "save", save)
    monkeypatch.setattr(route_card, "_get_user_language", AsyncMock(return_value="en"))
    monkeypatch.setattr(route_card, "update_card_for_driver", AsyncMock(return_value=True))
    yield store
    route_card_state._locks.clear()


@pytest.fixture
def _frozen_clock():
    """Fix round 2 minor: `_FrozenDateTime._frozen` is a class attribute, so
    leaving it set survives the test that set it (`route_card.datetime`
    itself IS restored by `monkeypatch`, but the frozen value on the class
    is not) -- an isolation smell under `pytest -n auto`. Reset explicitly,
    every time, pass or fail."""
    yield _FrozenDateTime
    _FrozenDateTime._frozen = None


@pytest.fixture
def _redis_unconfigured():
    """Fix round 2 minor: the Redis-down test used to call
    `route_card_state.configure(None)` with no teardown -- harmless only
    because None already happens to be the module's own default. Save and
    restore whatever was configured before this test explicitly, so this
    test never leaves a DIFFERENT global state than it found, regardless of
    what ran before it in the same `pytest -n auto` worker."""
    previous = route_card_state._redis
    route_card_state.configure(None)
    yield
    route_card_state.configure(previous)


@pytest.fixture(autouse=True)
def _seed_alert_translations(monkeypatch):
    """Feed the real English alert copy -- resolved live from the seed
    script -- into the i18n singleton for this file only. `monkeypatch.setitem`
    reverts the 'en' entry after every test, so other test files that rely
    on the empty-dict fallback are unaffected even when the whole suite runs
    in one process (same pattern as test_route_card_views.py)."""
    resolved = {}
    for key in _ALERT_KEYS:
        value = _SEED_MODULE._curated_value(key, "en")
        assert value, f"{key} has no curated English value in seed_staff_translations.py"
        resolved[key] = value
    merged = {**i18n.translations.get("en", {}), **resolved}
    monkeypatch.setitem(i18n.translations, "en", merged)


class TestAlertCap:
    def test_first_alert_pings(self, _state):
        _state[555] = {"chat_id": 555, "message_id": 10, "view": "next"}
        bot = AsyncMock()
        bot.send_message.return_value = AsyncMock(message_id=77)

        asyncio.run(route_card.send_head_change_alert(_bot_app(bot), telegram_id=555))

        kwargs = bot.send_message.await_args.kwargs
        assert kwargs["disable_notification"] is False
        assert _state[555]["last_alert_message_id"] == 77
        assert _state[555]["last_alert_at"]
        bot.delete_message.assert_not_called()

    def test_second_alert_within_window_is_silent_and_supersedes(self, _state):
        recent = datetime.now(timezone.utc) - timedelta(seconds=30)
        _state[555] = {
            "chat_id": 555, "message_id": 10, "view": "next",
            "last_alert_at": recent.isoformat(), "last_alert_message_id": 77,
        }
        bot = AsyncMock()
        bot.send_message.return_value = AsyncMock(message_id=78)

        asyncio.run(route_card.send_head_change_alert(_bot_app(bot), telegram_id=555))

        bot.delete_message.assert_awaited_once()
        assert bot.delete_message.await_args.kwargs["message_id"] == 77
        assert bot.send_message.await_args.kwargs["disable_notification"] is True
        assert _state[555]["last_alert_message_id"] == 78

    def test_alert_outside_window_pings_again(self, _state):
        old = datetime.now(timezone.utc) - timedelta(seconds=3600)
        _state[555] = {
            "chat_id": 555, "message_id": 10, "view": "next",
            "last_alert_at": old.isoformat(), "last_alert_message_id": 77,
        }
        bot = AsyncMock()
        bot.send_message.return_value = AsyncMock(message_id=79)

        asyncio.run(route_card.send_head_change_alert(_bot_app(bot), telegram_id=555))

        assert bot.send_message.await_args.kwargs["disable_notification"] is False

    def test_alert_refreshes_the_card_with_the_right_args(self, _state):
        """Fix round 2 minor: previously only asserted `assert_awaited()` --
        true even if the wrong bot_app, telegram_id, language or
        reference_message_id were passed. Assert the exact call. Also pins
        item 2's fix: reference_message_id is always None on this path."""
        _state[555] = {"chat_id": 555, "message_id": 10, "view": "next"}
        bot = AsyncMock()
        bot.send_message.return_value = AsyncMock(message_id=80)
        bot_app = _bot_app(bot)

        asyncio.run(route_card.send_head_change_alert(bot_app, telegram_id=555))

        route_card.update_card_for_driver.assert_awaited_once_with(
            bot_app, 555, language="en", reference_message_id=None,
        )


class TestAlertCopy:
    """Fix round 2 minor: nothing previously inspected the alert's actual
    text or keyboard, so a hardcoded/mistranslated English string would have
    passed every other test in this file. Assert against Task 2's curated
    seed values, not a hand-copied local string."""

    def test_alert_text_and_button_use_the_seeded_copy(self, _state):
        _state[555] = {"chat_id": 555, "message_id": 10, "view": "next"}
        bot = AsyncMock()
        bot.send_message.return_value = AsyncMock(message_id=95)

        asyncio.run(route_card.send_head_change_alert(_bot_app(bot), telegram_id=555))

        kwargs = bot.send_message.await_args.kwargs
        expected_text = _SEED_MODULE._curated_value("staff.route.head_changed_alert", "en")
        expected_button = _SEED_MODULE._curated_value("staff.route.open_route_card", "en")
        assert expected_text in kwargs["text"]
        button = kwargs["reply_markup"].inline_keyboard[0][0]
        assert expected_button in button.text
        assert button.callback_data == "staff_route_view_next"


class TestAlertCapBoundary:
    """The off-by-one that would mis-sound (or mis-silence) alerts forever."""

    def test_exactly_at_the_interval_pings_again(self, _state, monkeypatch, _frozen_clock):
        """age == ROUTE_ALERT_MIN_INTERVAL_SECONDS must be treated as OUTSIDE
        the window (`age < interval`, not `<=`) -- the cap has fully elapsed."""
        frozen_now = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)
        last = frozen_now - timedelta(seconds=route_card.ROUTE_ALERT_MIN_INTERVAL_SECONDS)
        _frozen_clock._frozen = frozen_now
        monkeypatch.setattr(route_card, "datetime", _frozen_clock)
        _state[555] = {
            "chat_id": 555, "message_id": 10, "view": "next",
            "last_alert_at": last.isoformat(), "last_alert_message_id": 77,
        }
        bot = AsyncMock()
        bot.send_message.return_value = AsyncMock(message_id=83)

        asyncio.run(route_card.send_head_change_alert(_bot_app(bot), telegram_id=555))

        assert bot.send_message.await_args.kwargs["disable_notification"] is False

    def test_one_second_inside_the_interval_is_still_capped(self, _state, monkeypatch, _frozen_clock):
        """age == interval - 1 must still be capped (silent + supersede)."""
        frozen_now = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)
        last = frozen_now - timedelta(seconds=route_card.ROUTE_ALERT_MIN_INTERVAL_SECONDS - 1)
        _frozen_clock._frozen = frozen_now
        monkeypatch.setattr(route_card, "datetime", _frozen_clock)
        _state[555] = {
            "chat_id": 555, "message_id": 10, "view": "next",
            "last_alert_at": last.isoformat(), "last_alert_message_id": 77,
        }
        bot = AsyncMock()
        bot.send_message.return_value = AsyncMock(message_id=84)

        asyncio.run(route_card.send_head_change_alert(_bot_app(bot), telegram_id=555))

        bot.delete_message.assert_awaited_once()
        assert bot.send_message.await_args.kwargs["disable_notification"] is True


class TestAlertCapConcurrency:
    """FINAL review, I2: `route_card.py:688,697` used to read `state` (and
    therefore decide `capped`) OUTSIDE any lock -- only the later merge/save
    was locked. Two sounded events handled concurrently for the same driver
    (distinct `event_id`s, so the webhook's dedup does not collapse them)
    could both observe the same `last_alert_at`, both compute
    `capped=False`, and both ping.

    Reproduced by pausing the FIRST call immediately after its own state
    read (the exact point the old code's lock did not yet cover) and
    letting a SECOND call attempt to run in that window. Against the
    unfixed code, the second call's own unlocked read lands before the
    first ever writes, so both compute capped=False and both ping. Against
    the fix, the whole decide-then-send-then-save is one critical section,
    so the second call cannot even begin its read until the first has
    fully recorded its own alert -- it then correctly sees itself as
    capped."""

    def test_two_concurrent_alerts_do_not_both_ping(self, _state, monkeypatch):
        _state[555] = {"chat_id": 555, "message_id": 10, "view": "next"}

        first_read_done = asyncio.Event()
        release_first = asyncio.Event()
        real_load = route_card_state.load
        calls = {"n": 0}

        async def _paced_load(telegram_id):
            result = await real_load(telegram_id)
            calls["n"] += 1
            if calls["n"] == 1:
                first_read_done.set()
                await release_first.wait()
            return result

        monkeypatch.setattr(route_card_state, "load", _paced_load)

        message_ids = iter([901, 902])
        bot = AsyncMock()
        bot.send_message = AsyncMock(side_effect=lambda *a, **k: MagicMock(message_id=next(message_ids)))

        async def scenario():
            task_a = asyncio.create_task(
                route_card.send_head_change_alert(_bot_app(bot), telegram_id=555)
            )
            await first_read_done.wait()
            task_b = asyncio.create_task(
                route_card.send_head_change_alert(_bot_app(bot), telegram_id=555)
            )
            # Give task_b every chance to run as far as it can while task_a
            # is paused: unfixed, it runs its own unlocked read straight
            # through to a send; fixed, it blocks acquiring the lock.
            await asyncio.sleep(0.02)
            release_first.set()
            await task_a
            await task_b

        asyncio.run(scenario())

        pings = [
            call.kwargs["disable_notification"] is False
            for call in bot.send_message.await_args_list
        ]
        assert sum(pings) == 1, f"expected exactly one uncapped ping among concurrent alerts, got {pings}"
        # The second (capped) alert must supersede the first.
        bot.delete_message.assert_awaited_once()
        assert bot.delete_message.await_args.kwargs["message_id"] == 901
        assert _state[555]["last_alert_message_id"] == 902


class TestAlertResilience:
    def test_alert_pings_when_redis_is_down(self, monkeypatch, _redis_unconfigured):
        """route_card_state.load returning None (Redis unreachable) must not
        crash and must not wrongly cap -- with no memory of a prior alert,
        the only safe default is to ping."""
        monkeypatch.setattr(route_card_state, "load", _REAL_STATE_LOAD)
        monkeypatch.setattr(route_card_state, "save", _REAL_STATE_SAVE)
        bot = AsyncMock()
        bot.send_message.return_value = AsyncMock(message_id=201)

        asyncio.run(route_card.send_head_change_alert(_bot_app(bot), telegram_id=999))

        bot.send_message.assert_awaited_once()
        assert bot.send_message.await_args.kwargs["disable_notification"] is False
        bot.delete_message.assert_not_called()

    def test_previous_alert_delete_failure_does_not_abort_the_new_send(self, _state):
        """48h limit / already-gone message: deleteMessage can fail (BadRequest
        or anything else) and the new alert must still go out."""
        recent = datetime.now(timezone.utc) - timedelta(seconds=10)
        _state[555] = {
            "chat_id": 555, "message_id": 10, "view": "next",
            "last_alert_at": recent.isoformat(), "last_alert_message_id": 77,
        }
        bot = AsyncMock()
        bot.delete_message.side_effect = Exception("message to delete not found")
        bot.send_message.return_value = AsyncMock(message_id=85)

        asyncio.run(route_card.send_head_change_alert(_bot_app(bot), telegram_id=555))

        bot.send_message.assert_awaited_once()
        assert bot.send_message.await_args.kwargs["disable_notification"] is True
        assert _state[555]["last_alert_message_id"] == 85

    def test_language_lookup_failure_still_sends_the_alert(self, _state, monkeypatch):
        """Fix round 2, item 4: a DB blip on the language lookup must not
        silently drop the one message class this plan allows to ping."""
        monkeypatch.setattr(
            route_card, "_get_user_language",
            AsyncMock(side_effect=RuntimeError("Database not connected")),
        )
        _state[555] = {"chat_id": 555, "message_id": 10, "view": "next"}
        bot = AsyncMock()
        bot.send_message.return_value = AsyncMock(message_id=86)

        asyncio.run(route_card.send_head_change_alert(_bot_app(bot), telegram_id=555))

        bot.send_message.assert_awaited_once()
        assert bot.send_message.await_args.kwargs["disable_notification"] is False
        route_card.update_card_for_driver.assert_awaited_once()
        assert route_card.update_card_for_driver.await_args.kwargs["language"] == i18n.fallback_language


class TestAlertCardRefreshDelegation:
    """Fix round 1, item 2: proves `send_head_change_alert` reaches the REAL
    `update_card_for_driver` correctly through `bot_app`, not just that it
    "was awaited" (the autouse mock already proved that). Message ids below
    are realistic and monotonic (fix round 2, note 2): the existing card's
    id is always LOWER than anything freshly sent afterward, so a regression
    that re-enables the repost heuristic on this path would show up as a
    SECOND `send_message` call, not hide behind an impossible id ordering."""

    def test_alert_fires_but_does_not_stomp_a_borrowed_card(self, _state, monkeypatch):
        """The alert is a distinct message from the card. When the card is
        BORROWED (driver looking at a stop's detail), the alert must still
        fire, but the card edit must NOT happen -- that guarantee belongs to
        Task 8's `update_card_for_driver` (proven exhaustively in
        tests/staff_bot/test_route_updated_sound_gate.py); this test proves
        THIS caller reaches that guarantee rather than bypassing it, by
        driving the real delegate instead of the autouse mock."""
        monkeypatch.setattr(route_card, "update_card_for_driver", _REAL_UPDATE_CARD_FOR_DRIVER)
        _state[555] = {
            "chat_id": 555, "message_id": 50, "view": route_card_state.VIEW_BORROWED,
        }
        bot = AsyncMock()
        bot.send_message.return_value = AsyncMock(message_id=950)

        asyncio.run(route_card.send_head_change_alert(_bot_app(bot), telegram_id=555))

        bot.send_message.assert_awaited_once()
        assert bot.send_message.await_args.kwargs["disable_notification"] is False
        bot.edit_message_text.assert_not_called()

    def test_alert_reaches_the_real_card_refresh_through_bot_app(self, _state, monkeypatch):
        """Regression proof: an earlier revision passed the raw `bot` straight
        into `update_card_for_driver`'s `bot_app` slot. That function reads
        `bot_app.bot_data` for its token manager and `bot_app.bot` for its
        own Telegram calls -- a raw Bot has neither, so the card would have
        silently never refreshed in production, on every push that is NOT
        borrowed (the only path exercised by the test above). Here the card
        actually exists, is NOT borrowed, and the token manager / API fetch
        are both wired for real, so a wrong object in the `bot_app` slot
        shows up here as "the card was never refreshed" -- token manager
        never awaited, `edit_message_text` never called -- instead of hiding
        behind a short-circuit or a mock. Also proves exactly ONE
        `send_message` call: card id (50) < alert id (950), so a regression
        that re-enables the repost heuristic here WOULD trigger a second
        send and fail this."""
        monkeypatch.setattr(route_card, "update_card_for_driver", _REAL_UPDATE_CARD_FOR_DRIVER)
        monkeypatch.setattr(route_card, "api_client", _ApiClientStub(_delivery_payload()))
        _state[555] = {
            "chat_id": 555, "message_id": 50, "card_date": route_card.local_date_str(),
            "view": route_card_state.VIEW_NEXT, "content_sig": "stale-sig",
        }
        bot = AsyncMock()
        bot.send_message.return_value = AsyncMock(message_id=950, chat_id=555)
        token_manager = MagicMock()
        token_manager.get_valid_token = AsyncMock(return_value="tok")
        bot_app = _bot_app(bot, bot_data={"token_manager": token_manager})

        asyncio.run(route_card.send_head_change_alert(bot_app, telegram_id=555))

        token_manager.get_valid_token.assert_awaited_once()
        bot.send_message.assert_awaited_once()  # the alert -- exactly one
        bot.edit_message_text.assert_awaited_once()  # the card, actually refreshed


class TestAlertStateRaceSafety:
    """Fix round 2, item 1 (CRITICAL). The alert's state save used to write
    back the WHOLE blob this function read at the top, across several awaits,
    with no lock. A driver's tap landing in that window
    (`route_card_state.mark_borrowed`) got silently overwritten the instant
    this function saved -- reviewer-probed against the old code: `final
    stored view: next`, `card edited despite borrow? True`.

    FINAL review, C1 + I2: two things changed since this test was written,
    both of which touch how it must simulate the race:

    1. `mark_borrowed` (C1) now takes `get_lock(telegram_id)` itself.
    2. `send_head_change_alert` (I2) now holds that SAME lock across its
       whole decide-send-save critical section, not just the final merge.

    The original technique injected the borrow via a DIRECT nested
    `await route_card_state.mark_borrowed(555)` inside the mocked
    `bot.send_message`'s side effect -- i.e. synchronously inside the SAME
    task that already holds the lock (since I2's fix wraps the send in the
    lock too). That is not how two independent Telegram updates actually
    race in production (a driver's tap is a separate task, scheduled by
    python-telegram-bot's own dispatcher); it is a same-task reentrant
    acquire, which is a real, verified deadlock now (`asyncio.Lock` is not
    reentrant) -- confirmed by hand: the old technique hangs forever
    against the current code.

    Fixed by spawning the borrow as its own `asyncio.Task`, mirroring how a
    genuinely concurrent driver tap is scheduled, and awaiting that task
    from the test's own top-level scenario (not from inside the mocked
    call) once `send_head_change_alert` has returned. The assertion this
    test exists for is unchanged: the borrow must still land and must not
    be clobbered -- it simply now lands (correctly) AFTER the alert's own
    save, once the lock is free, instead of racing inside it."""

    def test_borrow_landing_during_the_send_survives_the_alert_save(self, _state, monkeypatch):
        monkeypatch.setattr(route_card, "update_card_for_driver", _REAL_UPDATE_CARD_FOR_DRIVER)
        monkeypatch.setattr(route_card, "api_client", _ApiClientStub(_delivery_payload()))
        _state[555] = {
            "chat_id": 555, "message_id": 50, "card_date": route_card.local_date_str(),
            "view": route_card_state.VIEW_NEXT, "content_sig": "stale-sig",
        }
        bot = AsyncMock()
        borrow_tasks = []

        async def _send_message_racing_a_borrow(*args, **kwargs):
            # A driver's tap lands as an INDEPENDENT task -- not a nested
            # await -- while this alert's own send is in flight. It will
            # queue on the per-driver lock (held by send_head_change_alert
            # for its whole decide-send-save critical section, I2) and
            # proceed once that lock is released.
            task = asyncio.create_task(route_card_state.mark_borrowed(555))
            borrow_tasks.append(task)
            # `create_task` only SCHEDULES the coroutine; it hasn't run a
            # single line yet, so it hasn't joined the lock's waiter queue.
            # Yield once, while this task still holds the lock, so
            # `mark_borrowed` actually reaches its own (blocking) acquire
            # and registers as a waiter BEFORE we return and release --
            # asyncio.Lock's fairness then guarantees it is served ahead of
            # any acquire attempted afterward (e.g. update_card_for_driver's
            # own render), which is what makes this a real regression proof
            # rather than a coincidence of scheduling order.
            await asyncio.sleep(0)
            return AsyncMock(message_id=950, chat_id=555)

        bot.send_message.side_effect = _send_message_racing_a_borrow
        token_manager = MagicMock()
        token_manager.get_valid_token = AsyncMock(return_value="tok")
        bot_app = _bot_app(bot, bot_data={"token_manager": token_manager})

        async def scenario():
            await route_card.send_head_change_alert(bot_app, telegram_id=555)
            await asyncio.gather(*borrow_tasks)

        asyncio.run(scenario())

        assert _state[555]["view"] == route_card_state.VIEW_BORROWED, (
            f"a concurrent borrow must survive the alert's own state save, "
            f"got view={_state[555].get('view')!r}"
        )
        bot.edit_message_text.assert_not_called()
        # The alert's own fields still landed -- the fix merges them in,
        # it does not just skip its own write.
        assert _state[555]["last_alert_message_id"] == 950
