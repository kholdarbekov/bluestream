"""Card mechanics (spec §6.3): pinned silent creation, edit-in-place, no-op
skip, repost-when-buried, shift rollover, and the per-driver lock that keeps
a webhook push and a user tap from double-sending.

Uses a `_FakeRedis` (same shape as tests/staff_bot/test_route_card_state.py)
rather than the in-memory fallback the plan originally sketched for
`route_card_state` -- that fallback was removed (see that module's
docstring: it created a split-brain), so `route_card_state.configure(None)`
makes every load/save a no-op and there is nothing for these mechanics tests
to persist against. Each test gets its own fresh fake store, so nothing here
depends on real Redis or on another pytest-xdist worker's flush."""

import asyncio
import datetime as datetime_module
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import BadRequest, Forbidden, TimedOut

from staff_bot.handlers.delivery import route_card
from staff_bot.i18n import i18n
from staff_bot.utils import route_card_state


class _FakeRedis:
    """Just enough of redis.asyncio for set/get/delete with ex=."""

    def __init__(self):
        self.store = {}

    async def set(self, key, value, ex=None):
        self.store[key] = value

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        self.store.pop(key, None)


def _item(delivery_id, order_no):
    return {
        "delivery_id": delivery_id, "order_number": order_no, "status": "assigned",
        "customer_name": "U", "customer_phone": "+998900000001",
        "district": "Chilanzar", "address": "Street 1",
        "items": [], "total_amount": 10000, "payment_method": "cash",
        "amount_collected": 0, "outstanding_amount": 10000,
        "expected_cash_to_collect": 10000, "cod_reserved_prepayment_amount": 0,
        "destination_latitude": 41.31, "destination_longitude": 69.27,
        "route_position": 0, "is_next": True,
        "eta_minutes_from_current_location": None, "distance_km_to_next": None,
    }


def _payload(n=1):
    return {
        "items": [_item(10 + i, f"10{i}") for i in range(n)],
        "total": n,
        "location_status": "fresh",
        "route_summary": {
            "remaining": n, "stops_completed_today": 0, "stops_total_today": n,
            "committed_delivery_id": None, "finish_eta": None, "updated_at": None,
        },
    }


def _bot(next_message_id=100):
    bot = MagicMock()
    sent = MagicMock()
    sent.chat_id = 777
    sent.message_id = next_message_id
    bot.send_message = AsyncMock(return_value=sent)
    bot.edit_message_text = AsyncMock()
    bot.delete_message = AsyncMock()
    bot.pin_chat_message = AsyncMock()
    return bot


def _today():
    return route_card.local_date_str()


@pytest.fixture(autouse=True)
def _reset_state():
    route_card_state.configure(_FakeRedis())
    route_card_state._locks.clear()
    yield
    route_card_state.configure(None)
    route_card_state._locks.clear()


def _render(bot, *, view=None, ref=None, payload=None, session_hint=None, force=False):
    return asyncio.run(route_card.render_route_card(
        bot, telegram_id=777, chat_id=777, language="en",
        payload=payload or _payload(), view=view, reference_message_id=ref,
        session_hint=session_hint, force=force,
    ))


@pytest.mark.unit
class TestCreate:
    def test_first_render_sends_silent_and_pins_silent(self):
        bot = _bot()
        _render(bot)
        bot.send_message.assert_awaited_once()
        assert bot.send_message.await_args.kwargs["disable_notification"] is True
        bot.pin_chat_message.assert_awaited_once()
        pin_kwargs = bot.pin_chat_message.await_args.kwargs
        assert pin_kwargs["message_id"] == 100
        assert pin_kwargs["disable_notification"] is True
        # "never both" (fix round 1, M2): create must not also edit.
        bot.edit_message_text.assert_not_called()
        state = asyncio.run(route_card_state.load(777))
        assert state["message_id"] == 100
        assert state["card_date"] == _today()
        assert state["view"] == "next"

    def test_pin_failure_is_swallowed(self):
        bot = _bot()
        bot.pin_chat_message.side_effect = RuntimeError("no rights")
        _render(bot)
        bot.send_message.assert_awaited_once()  # card still created


@pytest.mark.unit
class TestRedisUnavailable:
    """route_card_state has NO in-memory fallback (removed -- see its module
    docstring for the split-brain that created). With Redis unconfigured,
    load() always returns None and save() no-ops silently. The mechanics
    must still behave sanely: never raise, and always fall down the
    'no card for today' path since there is nothing to remember -- every
    render degrades to a fresh, still-silent send rather than crashing or
    somehow producing a noisy one."""

    def test_renders_without_state_store_never_raise_and_stay_silent(self):
        route_card_state.configure(None)  # simulate Redis outage / not wired
        bot1 = _bot(next_message_id=501)
        _render(bot1)  # must not raise despite load()/save() being no-ops
        bot1.send_message.assert_awaited_once()
        assert bot1.send_message.await_args.kwargs["disable_notification"] is True

        # A second render for the same driver has no memory of the first
        # (nothing persisted) -- it also degrades to a fresh silent send,
        # never an edit against state that was never stored, and never a
        # crash from treating None as a dict.
        bot2 = _bot(next_message_id=502)
        _render(bot2, payload=_payload(2))
        bot2.send_message.assert_awaited_once()
        assert bot2.send_message.await_args.kwargs["disable_notification"] is True
        bot2.edit_message_text.assert_not_called()

        assert asyncio.run(route_card_state.load(777)) is None  # confirms no store


@pytest.mark.unit
class TestRedisOutageBoundedDegradation:
    """Review round 1, I2: with no `session_hint`, TestRedisUnavailable
    above is the DOCUMENTED behaviour when a caller can't offer one (the
    webhook path) -- every render creates fresh. This class covers the
    NEW opt-in path a PTB handler DOES have available: a caller-owned,
    process-lifetime-scoped dict (in production, `context.user_data`) that
    bounds a Redis outage to "one send per bot-process restart" instead of
    "one new pinned card per tap", and skips pinning entirely while it's
    in use (a durably-pinned card is a promise this mode can't keep)."""

    def test_first_render_with_hint_creates_unpinned(self):
        route_card_state.configure(None)  # Redis outage
        bot = _bot(next_message_id=601)
        hint = {}
        _render(bot, session_hint=hint)
        bot.send_message.assert_awaited_once()
        bot.pin_chat_message.assert_not_called()
        # The hint is populated in place so the SAME dict, handed back on
        # the next call, lets that call find it.
        assert hint["message_id"] == 601
        assert hint["chat_id"] == 777

    def test_second_tap_with_same_hint_edits_instead_of_creating(self):
        route_card_state.configure(None)  # Redis outage, whole session
        hint = {}
        bot1 = _bot(next_message_id=601)
        _render(bot1, session_hint=hint)  # tap 1: creates
        bot1.send_message.assert_awaited_once()

        bot2 = _bot(next_message_id=602)
        _render(bot2, payload=_payload(2), session_hint=hint)  # tap 2: same hint

        bot2.send_message.assert_not_called()
        bot2.edit_message_text.assert_awaited_once()
        assert bot2.edit_message_text.await_args.kwargs["message_id"] == 601
        bot2.pin_chat_message.assert_not_called()

        bot3 = _bot(next_message_id=603)
        _render(bot3, payload=_payload(3), session_hint=hint)  # tap 3: still bounded
        bot3.send_message.assert_not_called()
        bot3.edit_message_text.assert_awaited_once()

    def test_message_gone_during_outage_recreates_unpinned_and_refreshes_hint(self):
        route_card_state.configure(None)
        hint = {}
        bot1 = _bot(next_message_id=601)
        _render(bot1, session_hint=hint)

        bot2 = _bot(next_message_id=700)
        bot2.edit_message_text.side_effect = BadRequest("Message to edit not found")
        _render(bot2, payload=_payload(2), session_hint=hint)

        bot2.send_message.assert_awaited_once()  # fresh create, the documented recovery
        bot2.pin_chat_message.assert_not_called()  # still outage-mode: no pin
        assert hint["message_id"] == 700  # hint follows the new message

    def test_process_restart_loses_the_hint_but_still_never_pins(self):
        """A restart during an outage means a fresh, empty `user_data` --
        modelled here by simply not carrying the old hint forward. This is
        the one remaining "extra send" case the design accepts (see
        render_route_card's session_hint docstring): bounded by restart
        count, never by tap count, and still never pinned."""
        route_card_state.configure(None)
        bot1 = _bot(next_message_id=601)
        _render(bot1, session_hint={})  # "session" 1, discarded after
        bot1.send_message.assert_awaited_once()
        bot1.pin_chat_message.assert_not_called()

        bot2 = _bot(next_message_id=602)
        _render(bot2, session_hint={})  # "session" 2: fresh hint, no memory of session 1
        bot2.send_message.assert_awaited_once()  # a new create -- expected, not a repost
        bot2.pin_chat_message.assert_not_called()

    def test_hint_is_irrelevant_when_redis_is_healthy(self):
        """Passing a (stale, wrong) session_hint must never override real
        Redis-backed state -- it is consulted ONLY when `state is None`."""
        # Autouse fixture already configured a working _FakeRedis.
        bot1 = _bot(next_message_id=100)
        _render(bot1)  # real create, real state persisted
        bot1.pin_chat_message.assert_awaited_once()  # healthy Redis: pins as normal

        stale_hint = {"chat_id": 777, "message_id": 999999, "card_date": _today()}
        bot2 = _bot(next_message_id=200)
        _render(bot2, payload=_payload(2), session_hint=stale_hint)

        # Edited the REAL card (100), never touched the bogus hinted id.
        bot2.edit_message_text.assert_awaited_once()
        assert bot2.edit_message_text.await_args.kwargs["message_id"] == 100
        bot2.send_message.assert_not_called()


@pytest.mark.unit
class TestEdit:
    def test_second_render_edits_in_place_no_send(self):
        bot = _bot()
        _render(bot)
        bot2 = _bot()
        _render(bot2, payload=_payload(2))  # content changed
        bot2.send_message.assert_not_called()
        bot2.edit_message_text.assert_awaited_once()
        assert bot2.edit_message_text.await_args.kwargs["message_id"] == 100

    def test_identical_content_same_view_skips_edit(self, monkeypatch):
        """Unforced (webhook) renders keep their signature idempotence --
        two identical pushes must still collapse to one edit. The DRIVER
        tap path deliberately does NOT take this branch; see
        TestForcedRender."""
        monkeypatch.setattr(route_card, "format_local_time",
                            lambda dt=None, with_seconds=False: "11:42")
        import staff_bot.utils.formatters as fmt
        monkeypatch.setattr(fmt, "format_local_time",
                            lambda dt=None, with_seconds=False: "11:42")
        bot = _bot()
        _render(bot)
        bot2 = _bot()
        _render(bot2)
        bot2.edit_message_text.assert_not_called()
        bot2.send_message.assert_not_called()

    def test_edit_failure_message_gone_falls_back_to_fresh_send(self):
        """The ONLY case that should trigger delete+resend: Telegram says
        the message itself no longer exists. Uses the real exception type
        PTB raises (`telegram.error.BadRequest`), not a generic exception
        (fix round 1, I2: the old bare `except Exception` didn't distinguish
        this from "message is not modified" or a transient blip)."""
        bot = _bot()
        _render(bot)
        bot2 = _bot(next_message_id=200)
        bot2.edit_message_text.side_effect = BadRequest("Message to edit not found")
        _render(bot2, payload=_payload(2))
        bot2.send_message.assert_awaited_once()
        assert asyncio.run(route_card_state.load(777))["message_id"] == 200

    def test_edit_not_modified_is_treated_as_success_not_a_repost(self):
        """Reachable with ZERO Telegram flakiness (review I2): a Redis blip
        after a prior successful edit leaves a stale content_sig, so the
        next render computes a genuinely-identical signature check gap and
        attempts an edit Telegram already reflects. Telegram's own
        'Message is not modified' reports NOOP -- the card is correct, but
        nothing changed -- NOT "message gone", so the pinned card is never
        deleted+resent over it."""
        bot = _bot()
        _render(bot)
        bot2 = _bot()
        bot2.edit_message_text.side_effect = BadRequest(
            "Message is not modified: specified new message content and "
            "reply markup are exactly the same as a current content and "
            "reply markup of the message"
        )
        _render(bot2, payload=_payload(2))  # content actually differs
        bot2.edit_message_text.assert_awaited_once()
        bot2.send_message.assert_not_called()
        bot2.delete_message.assert_not_called()
        bot2.pin_chat_message.assert_not_called()
        # Signature persisted so a future render with the SAME content
        # skips the edit entirely via the no-op check.
        state = asyncio.run(route_card_state.load(777))
        assert state["message_id"] == 100

    def test_transient_edit_error_leaves_card_alone(self):
        """RetryAfter/TimedOut/NetworkError must never trigger a repost —
        deleting a perfectly good pinned card over a network blip would
        make flood control worse, not better (review I2)."""
        bot = _bot()
        _render(bot)
        bot2 = _bot()
        bot2.edit_message_text.side_effect = TimedOut()
        _render(bot2, payload=_payload(2))
        bot2.edit_message_text.assert_awaited_once()
        bot2.send_message.assert_not_called()
        bot2.delete_message.assert_not_called()
        # State untouched -- next render retries the SAME edit (against the
        # same message_id), not a repost against a freshly created one.
        state = asyncio.run(route_card_state.load(777))
        assert state["message_id"] == 100

    def test_unrecognized_bad_request_leaves_card_alone(self):
        """A BadRequest reason we don't recognize is NOT assumed to mean
        "message gone" -- only the specific message-gone family reposts
        (review I2: 'restrict the fallback to the message-gone BadRequest
        family')."""
        bot = _bot()
        _render(bot)
        bot2 = _bot()
        bot2.edit_message_text.side_effect = BadRequest("Chat not found")
        _render(bot2, payload=_payload(2))
        bot2.edit_message_text.assert_awaited_once()
        bot2.send_message.assert_not_called()
        bot2.delete_message.assert_not_called()

    def test_send_failure_on_create_does_not_raise(self):
        """A driver blocking the bot is routine, not exceptional (review
        I4). The create-path send is unguarded no longer -- it must not
        propagate out of render_route_card."""
        bot = _bot()
        bot.send_message.side_effect = Forbidden("bot was blocked by the user")
        _render(bot)  # must not raise
        bot.pin_chat_message.assert_not_called()  # never reached
        assert asyncio.run(route_card_state.load(777)) is None  # nothing persisted


@pytest.mark.unit
class TestForcedRender:
    """The 2026-08-14 reported bug: a driver taps 'Active deliveries' four
    times inside one minute and the bot makes ZERO Telegram calls, because
    the only time-varying token in the card was a minute-granular stamp."""

    @pytest.fixture(autouse=True)
    def _seed_updated_at_translation(self, monkeypatch):
        """This file's i18n singleton has no translations loaded, so an
        unseeded `staff.route.updated_at` falls back to the humanized key
        tail ("Updated at"), which has no `{time}` placeholder -- str.format
        silently drops the `time` kwarg on a string with no slot for it.
        Without this seed, `test_forced_render_stamps_seconds_unforced_does_not`
        below would never see a seconds-stamp in EITHER render and pass
        vacuously regardless of whether `with_seconds` actually worked. Seed
        the real production copy (scripts/seed_staff_translations.py) so the
        assertion exercises genuine product text. `monkeypatch.setitem`
        reverts the "en" entry after this class's tests, so nothing here
        leaks into other test files."""
        merged = {**i18n.translations.get("en", {}), "staff.route.updated_at": "updated {time}"}
        monkeypatch.setitem(i18n.translations, "en", merged)

    def test_repeat_tap_same_minute_still_edits(self, monkeypatch):
        # Freeze the MINUTE stamp exactly as the old no-op test does, so the
        # only thing that can break the tie is the seconds resolution.
        monkeypatch.setattr(
            route_card, "format_local_time",
            lambda dt=None, with_seconds=False: "11:42:07" if with_seconds else "11:42",
        )
        bot = _bot()
        _render(bot, force=True)
        bot.send_message.assert_awaited_once()

        # Second tap, same frozen minute: must still reach Telegram.
        bot2 = _bot()
        _render(bot2, force=True)
        bot2.edit_message_text.assert_awaited_once()
        assert bot2.edit_message_text.await_args.kwargs["message_id"] == 100
        bot2.send_message.assert_not_called()

    def test_forced_render_stamps_seconds_unforced_does_not(self):
        bot = _bot()
        _render(bot, force=True)
        forced_text = bot.send_message.await_args.kwargs["text"]

        route_card_state.configure(_FakeRedis())  # fresh store, force a create
        bot2 = _bot()
        _render(bot2, force=False)
        plain_text = bot2.send_message.await_args.kwargs["text"]

        import re
        assert re.search(r"\d{2}:\d{2}:\d{2}", forced_text)
        assert not re.search(r"\d{2}:\d{2}:\d{2}", plain_text)

    def test_forced_render_actually_changes_the_text(self, monkeypatch):
        """force=True only guarantees an edit is ATTEMPTED. If the text were
        byte-identical Telegram answers 'message is not modified', which
        render_route_card reports as NOOP (route_card.py, the 'not
        modified' branch) -- the card is correct, but the driver still sees
        no visible change. The seconds stamp is what makes the edit real, so
        assert the text, not the call."""
        times = iter(["11:42:07", "11:42:09"])
        monkeypatch.setattr(
            route_card, "format_local_time",
            lambda dt=None, with_seconds=False: next(times) if with_seconds else "11:42",
        )
        bot = _bot()
        _render(bot, force=True)
        first = bot.send_message.await_args.kwargs["text"]
        bot2 = _bot()
        _render(bot2, force=True)
        second = bot2.edit_message_text.await_args.kwargs["text"]
        assert first != second


@pytest.mark.unit
class TestDeletionAwareRepost:
    """Repost when the card is not provably the last visible message.

    Reposting on every tap costs 1 send + 1 delete + 1 pin against a ~1
    msg/sec per-chat budget, and PTB's AIORateLimiter halts ALL requests on
    RetryAfter -- so an unconditional repost would trip flood control on a
    frustrated driver and reproduce the very bug this fixes."""

    def _set_echoes(self, n):
        state = asyncio.run(route_card_state.load(777))
        state["echoes_deleted"] = n
        asyncio.run(route_card_state.save(777, state))

    def test_repeat_taps_whose_echoes_we_deleted_never_repost(self):
        bot = _bot()
        _render(bot, force=True)  # card at 100

        # Three taps at ids 101, 102, 103; we deleted each echo.
        for tap_id, deleted in ((101, 0), (102, 1), (103, 2)):
            self._set_echoes(deleted)
            bot_n = _bot(next_message_id=999)
            _render(bot_n, ref=tap_id, force=True)
            bot_n.send_message.assert_not_called()
            bot_n.edit_message_text.assert_awaited_once()

    def test_undeleted_traffic_below_the_card_triggers_a_repost(self):
        bot = _bot()
        _render(bot, force=True)  # card at 100
        # 3 echoes deleted, then a head-change alert (id 104) we did NOT
        # delete, then a tap at 105. Gap 5 > 3 + 1 -> repost.
        self._set_echoes(3)
        bot2 = _bot(next_message_id=300)
        _render(bot2, ref=105, force=True)
        bot2.send_message.assert_awaited_once()
        assert bot2.send_message.await_args.kwargs["disable_notification"] is True
        bot2.delete_message.assert_awaited_once()
        assert bot2.delete_message.await_args.kwargs["message_id"] == 100
        bot2.edit_message_text.assert_not_called()

    def test_repost_resets_the_echo_counter(self):
        bot = _bot()
        _render(bot, force=True)
        self._set_echoes(3)
        _render(_bot(next_message_id=300), ref=105, force=True)
        assert asyncio.run(route_card_state.load(777))["echoes_deleted"] == 0

    def test_edit_in_place_preserves_the_echo_counter(self):
        bot = _bot()
        _render(bot, force=True)
        self._set_echoes(2)
        _render(_bot(), ref=103, force=True)
        assert asyncio.run(route_card_state.load(777))["echoes_deleted"] == 2


@pytest.mark.unit
class TestRepostAndRollover:
    def test_buried_card_is_reposted_below(self):
        bot = _bot()
        _render(bot)  # card at message_id 100, echoes_deleted 0
        bot2 = _bot(next_message_id=300)
        _render(bot2, ref=103)  # gap 3 > 0 + 1
        bot2.delete_message.assert_awaited_once()
        assert bot2.delete_message.await_args.kwargs["message_id"] == 100
        bot2.send_message.assert_awaited_once()
        assert bot2.send_message.await_args.kwargs["disable_notification"] is True
        # "never both" (fix round 1, M2): repost must not also edit.
        bot2.edit_message_text.assert_not_called()
        assert asyncio.run(route_card_state.load(777))["message_id"] == 300

    def test_adjacent_tap_edits_instead_of_reposting(self):
        """Gap of exactly 1 = the tap is the very next id after the card, so
        the card is still the last visible message. Nothing to move."""
        bot = _bot()
        _render(bot)
        bot2 = _bot()
        _render(bot2, ref=101, payload=_payload(2))
        bot2.send_message.assert_not_called()
        bot2.edit_message_text.assert_awaited_once()

    def test_gap_exactly_at_threshold_edits_not_reposts(self):
        """The condition is a strict `>`, so a gap exactly equal to
        `echoes_deleted + 1` must NOT repost (review M3: the boundary
        itself was uncovered)."""
        bot = _bot()
        _render(bot)
        state = asyncio.run(route_card_state.load(777))
        state["echoes_deleted"] = 2
        asyncio.run(route_card_state.save(777, state))
        bot2 = _bot()
        _render(bot2, ref=103, payload=_payload(2))  # gap 3, threshold 3
        bot2.send_message.assert_not_called()
        bot2.delete_message.assert_not_called()
        bot2.edit_message_text.assert_awaited_once()

    def test_yesterdays_card_gets_a_fresh_message(self):
        bot = _bot()
        _render(bot)
        state = asyncio.run(route_card_state.load(777))
        state["card_date"] = "2000-01-01"  # simulate shift rollover
        asyncio.run(route_card_state.save(777, state))
        bot2 = _bot(next_message_id=400)
        _render(bot2)
        bot2.delete_message.assert_awaited_once()  # best-effort cleanup
        bot2.send_message.assert_awaited_once()
        # "never both" (fix round 1, M2): rollover must not also edit.
        bot2.edit_message_text.assert_not_called()
        assert asyncio.run(route_card_state.load(777))["card_date"] == _today()


@pytest.mark.unit
class TestShiftRollover:
    def test_local_date_str_uses_tashkent_not_utc(self, monkeypatch):
        """Review I3: the existing rollover tests compute their expected
        `card_date` via `route_card.local_date_str()` itself, so a UTC
        implementation would pass every one of them unchanged. This test
        freezes the clock to an instant that falls on DIFFERENT calendar
        dates in UTC vs Asia/Tashkent (UTC+5, no DST) and asserts against a
        LITERAL expected string -- not by calling local_date_str() on both
        sides -- so a regression to datetime.utcnow()/datetime.now() (which
        would roll the shift over at 05:00 local, mid-shift) fails this
        test."""
        fixed_utc = datetime_module.datetime(2026, 8, 11, 20, 0, tzinfo=datetime_module.timezone.utc)

        class _FixedDatetime(datetime_module.datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_utc.astimezone(tz) if tz is not None else fixed_utc

        monkeypatch.setattr(route_card, "datetime", _FixedDatetime)

        # 20:00 UTC on 2026-08-11 is already 01:00 the NEXT day in Tashkent.
        assert route_card.local_date_str() == "2026-08-12"


@pytest.mark.unit
class TestConcurrency:
    def test_concurrent_renders_serialize_one_card(self):
        """A webhook push racing a user tap must not create two cards — the
        per-driver lock serializes the read-modify-write (the successor of
        the user_data render lock at active_delivery.py:32-39)."""
        bot = MagicMock()
        counter = {"n": 100}

        async def _send(*a, **k):
            await asyncio.sleep(0.01)  # widen the race window
            counter["n"] += 1
            sent = MagicMock()
            sent.chat_id = 777
            sent.message_id = counter["n"]
            return sent

        bot.send_message = AsyncMock(side_effect=_send)
        bot.edit_message_text = AsyncMock()
        bot.delete_message = AsyncMock()
        bot.pin_chat_message = AsyncMock()

        async def run():
            await asyncio.gather(
                route_card.render_route_card(
                    bot, telegram_id=777, chat_id=777, language="en", payload=_payload()
                ),
                route_card.render_route_card(
                    bot, telegram_id=777, chat_id=777, language="en", payload=_payload(2)
                ),
            )

        asyncio.run(run())
        # Exactly one CREATE; the loser of the race sees state and edits.
        assert bot.send_message.await_count == 1
        assert bot.edit_message_text.await_count == 1


@pytest.mark.unit
class TestRenderOutcome:
    def test_outcomes_are_distinguishable(self):
        bot = _bot()
        assert _render(bot) == route_card.RenderOutcome.RENDERED

        bot2 = _bot()
        assert _render(bot2) == route_card.RenderOutcome.NOOP  # identical, unforced

        bot3 = _bot()
        bot3.edit_message_text.side_effect = TimedOut()
        assert _render(bot3, payload=_payload(2)) == route_card.RenderOutcome.FAILED

    def test_borrowed_card_reports_blocked_not_failed(self):
        bot = _bot()
        _render(bot)
        asyncio.run(route_card_state.mark_borrowed(777))
        outcome = asyncio.run(route_card.render_route_card(
            bot, telegram_id=777, chat_id=777, language="en",
            payload=_payload(), respect_borrowed=True,
        ))
        assert outcome == route_card.RenderOutcome.BLOCKED

    def test_not_modified_reports_noop_not_rendered(self):
        """Telegram says the content is identical, so nothing on the
        driver's screen changed -- reporting RENDERED would hide exactly
        the regression this branch exists to surface."""
        bot = _bot()
        _render(bot)
        bot2 = _bot()
        bot2.edit_message_text.side_effect = BadRequest(
            "Message is not modified: specified new message content and "
            "reply markup are exactly the same as a current content and "
            "reply markup of the message"
        )
        outcome = _render(bot2, payload=_payload(2))
        assert outcome == route_card.RenderOutcome.NOOP


@pytest.mark.unit
class TestWebhookEntry:
    def _bot_app(self, bot, token="tok"):
        app = MagicMock()
        app.bot = bot
        tm = MagicMock()
        tm.get_valid_token = AsyncMock(return_value=token)
        app.bot_data = {"token_manager": tm}
        return app

    def _api(self, payload, success=True):
        class _Client:
            def __init__(self):
                self.client = MagicMock()
                self.client.get_active_deliveries = AsyncMock(
                    return_value=MagicMock(success=success, data=payload)
                )

            async def __aenter__(self):
                return self.client

            async def __aexit__(self, *a):
                return False

        return _Client()

    def test_updates_card_silently(self, monkeypatch):
        bot = _bot()
        monkeypatch.setattr(route_card, "api_client", self._api(_payload()))
        monkeypatch.setattr(route_card, "_get_user_language", AsyncMock(return_value="en"))
        ok = asyncio.run(route_card.update_card_for_driver(self._bot_app(bot), 777))
        assert ok is True
        bot.send_message.assert_awaited_once()  # first-ever card, silent create
        assert bot.send_message.await_args.kwargs["disable_notification"] is True

    def test_skips_when_borrowed(self, monkeypatch):
        asyncio.run(route_card_state.save(777, {
            "chat_id": 777, "message_id": 100, "card_date": _today(),
            "view": route_card_state.VIEW_BORROWED, "content_sig": "x",
        }))
        bot = _bot()
        monkeypatch.setattr(route_card, "api_client", self._api(_payload()))
        ok = asyncio.run(route_card.update_card_for_driver(self._bot_app(bot), 777))
        assert ok is False
        bot.send_message.assert_not_called()
        bot.edit_message_text.assert_not_called()

    def test_skips_without_token(self, monkeypatch):
        bot = _bot()
        monkeypatch.setattr(route_card, "api_client", self._api(_payload()))
        ok = asyncio.run(route_card.update_card_for_driver(self._bot_app(bot, token=None), 777))
        assert ok is False
        bot.send_message.assert_not_called()

    def test_skips_on_api_failure(self, monkeypatch):
        """M4: the fourth documented skip reason (API failure) had no test."""
        bot = _bot()
        monkeypatch.setattr(route_card, "api_client", self._api(None, success=False))
        ok = asyncio.run(route_card.update_card_for_driver(self._bot_app(bot), 777))
        assert ok is False
        bot.send_message.assert_not_called()
        bot.edit_message_text.assert_not_called()

    def test_does_not_raise_when_driver_blocked_the_bot(self, monkeypatch):
        """Review I4: a blocked bot is routine, and update_card_for_driver's
        contract is "returns False", not "may raise". Task 6 fans this out
        over several drivers in one loop -- one blocked driver must not
        abort the rest."""
        bot = _bot()
        bot.send_message.side_effect = Forbidden("bot was blocked by the user")
        monkeypatch.setattr(route_card, "api_client", self._api(_payload()))
        monkeypatch.setattr(route_card, "_get_user_language", AsyncMock(return_value="en"))
        ok = asyncio.run(route_card.update_card_for_driver(self._bot_app(bot), 777))  # must not raise
        assert ok is False

    def test_stale_shift_borrow_does_not_block_the_webhook_pre_check(self, monkeypatch):
        """FINAL review, I3: `update_card_for_driver`'s own cheap pre-check
        (route_card.py:573-578) used to short-circuit on `view` alone,
        ignoring `card_date` -- the ONE thing that makes a borrow from a
        PREVIOUS shift meaningless. Every production webhook push reaches
        `render_route_card` ONLY through this function, so that blind
        pre-check alone was enough to strand a card across a shift boundary
        even after `render_route_card`'s own guard was made date-aware:
        this earlier, cruder check would already have returned False before
        the deeper fix ever got a chance to run."""
        asyncio.run(route_card_state.save(777, {
            "chat_id": 777, "message_id": 100, "card_date": "2000-01-01",
            "view": route_card_state.VIEW_BORROWED, "content_sig": "x",
        }))
        bot = _bot(next_message_id=400)
        monkeypatch.setattr(route_card, "api_client", self._api(_payload()))
        monkeypatch.setattr(route_card, "_get_user_language", AsyncMock(return_value="en"))

        ok = asyncio.run(route_card.update_card_for_driver(self._bot_app(bot), 777))

        assert ok is True, "a stale-shift borrow must not block the webhook's own pre-check"
        bot.delete_message.assert_awaited_once()  # best-effort cleanup of yesterday's message
        bot.send_message.assert_awaited_once()
        bot.edit_message_text.assert_not_called()
        state = asyncio.run(route_card_state.load(777))
        assert state["card_date"] == _today()
        assert state["view"] == route_card_state.VIEW_NEXT

    def test_borrow_landing_during_the_api_call_wins_the_race(self, monkeypatch):
        """Review I1 (TOCTOU): update_card_for_driver's own borrowed
        pre-check is a cheap, non-authoritative short-circuit that runs
        BEFORE the API call. This test seeds a card that is NOT borrowed at
        that pre-check, then has the mocked API call itself mark the card
        borrowed (simulating a concurrent driver tap winning the race
        during the awaited HTTP round trip, before render_route_card
        acquires its lock). The AUTHORITATIVE re-check inside the lock must
        still catch it: no edit, no send, ok is False. Drives the actual
        race window, not just a call to a function that happens to exist."""
        asyncio.run(route_card_state.save(777, {
            "chat_id": 777, "message_id": 100, "card_date": _today(),
            "view": route_card_state.VIEW_NEXT, "content_sig": "stale",
        }))
        bot = _bot()

        class _RacyClient:
            async def __aenter__(self):
                client = MagicMock()

                async def _get_active_deliveries(token):
                    # The race window: the pre-check already passed (state
                    # was VIEW_NEXT). A concurrent driver tap wins right
                    # here, before render_route_card acquires the lock.
                    await route_card_state.mark_borrowed(777)
                    return MagicMock(success=True, data=_payload(2))

                client.get_active_deliveries = AsyncMock(side_effect=_get_active_deliveries)
                return client

            async def __aexit__(self, *a):
                return False

        monkeypatch.setattr(route_card, "api_client", _RacyClient())
        monkeypatch.setattr(route_card, "_get_user_language", AsyncMock(return_value="en"))

        ok = asyncio.run(route_card.update_card_for_driver(self._bot_app(bot), 777))

        assert ok is False  # authoritative in-lock check caught the borrow
        bot.edit_message_text.assert_not_called()
        bot.send_message.assert_not_called()
        # The borrow itself is untouched by the aborted render.
        assert asyncio.run(route_card_state.load(777))["view"] == route_card_state.VIEW_BORROWED


@pytest.mark.unit
class TestMarkBorrowedRace:
    """FINAL review, C1: `route_card_state.mark_borrowed` used to do its own
    UNLOCKED load-modify-save. If a driver tapped a stop while a webhook
    push's `render_route_card` was mid-`editMessageText` (holding the
    per-driver lock for its whole read-modify-write), the borrow landed in
    Redis, then the render resumed with its OWN stale, already-loaded
    `state` object and clobbered it straight back to the pre-borrow view.
    Reviewer-probed against the pre-fix code: `after mark_borrowed ->
    borrowed`, `after render completes -> next`, `follow-up webhook edited
    the borrowed message? True`.

    This test drives the exact same race with the real
    `route_card_state.mark_borrowed` and the real `render_route_card`, using
    an `edit_message_text` double that blocks on an `asyncio.Event` to
    reproduce "the edit's round trip is in flight" deterministically instead
    of via timing."""

    def test_borrow_landing_during_an_in_flight_edit_survives_and_blocks_the_next_push(self):
        # Seed an existing card so the webhook push takes the EDIT branch
        # (not create), which is the branch that holds the lock across a
        # real Telegram round trip.
        asyncio.run(route_card_state.save(777, {
            "chat_id": 777, "message_id": 100, "card_date": _today(),
            "view": route_card_state.VIEW_NEXT, "content_sig": "stale",
        }))

        edit_started = asyncio.Event()
        edit_may_finish = asyncio.Event()

        async def _slow_edit(*args, **kwargs):
            edit_started.set()
            await edit_may_finish.wait()

        bot = _bot()
        bot.edit_message_text = AsyncMock(side_effect=_slow_edit)

        async def scenario():
            # Step 1: a silent webhook push starts rendering -- content
            # differs from the stale signature, so it takes the edit branch
            # and parks on the (slow) edit call while holding the lock.
            render_task = asyncio.create_task(route_card.render_route_card(
                bot, telegram_id=777, chat_id=777, language="en",
                payload=_payload(2), respect_borrowed=True,
            ))
            await edit_started.wait()

            # Step 2: the driver taps a stop -- exactly
            # active_delivery.py's view_active_delivery calling
            # route_card_state.mark_borrowed while the edit above is
            # in flight.
            borrow_task = asyncio.create_task(route_card_state.mark_borrowed(777))
            await asyncio.sleep(0.02)  # let mark_borrowed reach (and, unfixed, clear) the lock

            # Step 3: Telegram's edit "returns".
            edit_may_finish.set()
            await render_task
            await borrow_task

        asyncio.run(scenario())

        state = asyncio.run(route_card_state.load(777))
        assert state["view"] == route_card_state.VIEW_BORROWED, (
            f"a borrow landing mid-render must survive the render's own save, "
            f"got view={state.get('view')!r}"
        )

        # A follow-up silent webhook push must now skip the card entirely --
        # it is borrowed. Before the fix this edited the message the driver
        # is mid at-door-flow on.
        bot2 = _bot()
        result = asyncio.run(route_card.render_route_card(
            bot2, telegram_id=777, chat_id=777, language="en",
            payload=_payload(2), respect_borrowed=True,
        ))
        assert result == route_card.RenderOutcome.BLOCKED
        bot2.edit_message_text.assert_not_called()


@pytest.mark.unit
class TestBorrowedRolloverPrecedence:
    """FINAL review, I3: must ship together with C1. Once C1 makes a borrow
    reliably survive a concurrent render, a borrow that nothing ever clears
    (there is no production caller of `route_card_state.clear`, and only a
    successful FULL render un-borrows) can strand a card across a shift
    boundary: `respect_borrowed`'s short-circuit used to fire before the
    `card_date != today` rollover check, so every webhook push the day
    after a borrow was set returned False right there, forever, until the
    48h TTL evicted the state."""

    def test_borrowed_card_from_a_previous_shift_still_rolls_over(self):
        asyncio.run(route_card_state.save(777, {
            "chat_id": 777, "message_id": 100, "card_date": "2000-01-01",
            "view": route_card_state.VIEW_BORROWED, "content_sig": "x",
        }))
        bot = _bot(next_message_id=400)

        result = asyncio.run(route_card.render_route_card(
            bot, telegram_id=777, chat_id=777, language="en",
            payload=_payload(), respect_borrowed=True,
        ))

        assert result == route_card.RenderOutcome.RENDERED, \
            "the rollover-create must win over a stale-shift borrow"
        bot.delete_message.assert_awaited_once()  # best-effort cleanup of yesterday's message
        bot.send_message.assert_awaited_once()
        bot.edit_message_text.assert_not_called()
        state = asyncio.run(route_card_state.load(777))
        assert state["card_date"] == _today()
        assert state["view"] == route_card_state.VIEW_NEXT

    def test_borrowed_card_from_today_still_blocks_the_webhook(self):
        """Guard against an over-correction: a same-day borrow must still
        protect the card from a silent webhook edit."""
        asyncio.run(route_card_state.save(777, {
            "chat_id": 777, "message_id": 100, "card_date": _today(),
            "view": route_card_state.VIEW_BORROWED, "content_sig": "x",
        }))
        bot = _bot()

        result = asyncio.run(route_card.render_route_card(
            bot, telegram_id=777, chat_id=777, language="en",
            payload=_payload(), respect_borrowed=True,
        ))

        assert result == route_card.RenderOutcome.BLOCKED
        bot.edit_message_text.assert_not_called()
        bot.send_message.assert_not_called()
        bot.delete_message.assert_not_called()
