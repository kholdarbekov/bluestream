"""The bot READS the backend-published `sound` field — it never re-derives
materiality (design principle 3). sound=False -> 200 with no message and no
dedup consumption; sound=True/missing -> today's sounded behaviour."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from staff_bot.handlers.delivery import route_card
from staff_bot.utils import route_card_state
from staff_bot.webhook_server import StaffWebhookServer, handle_route_updated_payload


def _server_with_bot():
    server = StaffWebhookServer()
    server.bot_app = MagicMock()
    server.bot_app.bot.send_message = AsyncMock()
    # A bare MagicMock() for bot_data would make `.get("token_manager")`
    # return another (truthy, non-awaitable) MagicMock instead of the real
    # "no token manager wired up" signal `update_card_for_driver` checks for
    # -- give it a real empty dict so the silent branch's no-token path
    # exercises the actual code path instead of an incidental TypeError.
    server.bot_app.bot_data = {}
    return server


def _request_with(payload):
    req = MagicMock()
    req.path = '/internal/route-updated'
    req.json = AsyncMock(return_value=payload)
    return req


def _i18n_stub():
    stub = MagicMock()
    stub.get_user_language = AsyncMock(return_value='en')
    stub.get = MagicMock(return_value='Route updated')
    return stub


@pytest.mark.unit
def test_silent_update_sends_no_message_and_skips_dedup():
    server = _server_with_bot()
    payload = {'telegram_id': 777, 'driver_id': 5, 'sound': False,
               'head_changed': False, 'driver_initiated': True}
    with patch('staff_bot.webhook_server.verify_webhook_signature',
               AsyncMock(return_value=True)), \
         patch('staff_bot.webhook_server.i18n', _i18n_stub()):
        resp = asyncio.run(server.route_updated_handler(_request_with(payload)))
    assert resp.status == 200
    server.bot_app.bot.send_message.assert_not_called()
    # The silent event must not have recorded a dedup key.
    assert server._processed_events == {}


@pytest.mark.unit
def test_sounded_update_still_sends(monkeypatch):
    """Fix round 1: sound=True now routes through `route_card.send_head_change_alert`
    (Task 9), which calls the real `route_card._get_user_language` (a DB
    lookup) unless mocked -- `_server_with_bot()`'s empty `bot_data` still
    short-circuits the card-refresh half before any of that matters, but the
    alert half runs for real, so this mock is required here regardless."""
    monkeypatch.setattr(route_card, "_get_user_language", AsyncMock(return_value="en"))
    server = _server_with_bot()
    payload = {'telegram_id': 777, 'driver_id': 5, 'sound': True,
               'head_changed': True, 'driver_initiated': False}
    with patch('staff_bot.webhook_server.verify_webhook_signature',
               AsyncMock(return_value=True)), \
         patch('staff_bot.webhook_server.i18n', _i18n_stub()):
        resp = asyncio.run(server.route_updated_handler(_request_with(payload)))
    assert resp.status == 200
    server.bot_app.bot.send_message.assert_called_once()


@pytest.mark.unit
def test_missing_sound_field_defaults_to_sounded(monkeypatch):
    """Rolling-restart safety: an older backend that sends no `sound` keeps
    today's behaviour."""
    monkeypatch.setattr(route_card, "_get_user_language", AsyncMock(return_value="en"))
    server = _server_with_bot()
    payload = {'telegram_id': 777, 'driver_id': 5}
    with patch('staff_bot.webhook_server.verify_webhook_signature',
               AsyncMock(return_value=True)), \
         patch('staff_bot.webhook_server.i18n', _i18n_stub()):
        resp = asyncio.run(server.route_updated_handler(_request_with(payload)))
    assert resp.status == 200
    server.bot_app.bot.send_message.assert_called_once()


@pytest.mark.unit
def test_silent_then_sounded_is_not_deduped_away(monkeypatch):
    """Regression for the §2.3 bug shape: a silent event must never occupy the
    constant-key fallback slot and swallow the next genuine sounded push."""
    monkeypatch.setattr(route_card, "_get_user_language", AsyncMock(return_value="en"))
    server = _server_with_bot()
    silent = {'telegram_id': 777, 'driver_id': 5, 'sound': False}
    sounded = {'telegram_id': 777, 'driver_id': 5, 'sound': True}
    with patch('staff_bot.webhook_server.verify_webhook_signature',
               AsyncMock(return_value=True)), \
         patch('staff_bot.webhook_server.i18n', _i18n_stub()):
        asyncio.run(server.route_updated_handler(_request_with(silent)))
        asyncio.run(server.route_updated_handler(_request_with(sounded)))
    server.bot_app.bot.send_message.assert_called_once()


# --- Task 8: the silent branch edits the card instead of doing nothing -----
#
# Plan 3 turns the silent branch above from "acknowledge and do nothing"
# into "silently refresh the driver's route card". `handle_route_updated_payload`
# is the free function `route_updated_handler` delegates to; it is what
# makes the branch unit-testable without an aiohttp request.
#
# Fix round 1 corrections baked into this section:
#   - assertions on the delegation boundary now check propagated, falsifiable
#     return values (not an always-True tautology);
#   - the borrowed-card coverage is split into the cheap pre-check (which
#     THIS test class's original version actually exercised) and a genuine
#     race that drives the authoritative in-lock recheck;
#   - the Redis-outage test is rewritten to the behaviour that can actually
#     occur (route_card_state shares the token manager's own Redis client --
#     staff_bot/bot.py -- so a real outage kills the token cache too, and
#     `update_card_for_driver` short-circuits at the token check before any
#     fetch or render: 0 send / 0 edit / 0 pin, NOT an unpinned create loop);
#   - a new, narrower and genuinely reachable variant is added alongside it:
#     the card-state KEY evicted while the token cache (a different key, same
#     live connection) stays warm -- that one DOES reach render and DOES pin
#     every push;
#   - the first-contact test now asserts the pin call too (send + pin = 2
#     Telegram calls, not 1).


def _wired_bot(next_message_id=100, chat_id=555):
    """A bot mock with every route-card-relevant method properly async --
    mirrors tests/staff_bot/test_route_card_render.py's `_bot()`. Required:
    a bare MagicMock()'s auto-created attributes are NOT awaitable, and
    `_create_card`'s try/except around `pin_chat_message` silently swallows
    that mismatch -- using an under-wired bot would hide a real pin-count
    bug instead of catching it (fix round 1, M6)."""
    bot = MagicMock()
    sent = MagicMock(chat_id=chat_id, message_id=next_message_id)
    bot.send_message = AsyncMock(return_value=sent)
    bot.edit_message_text = AsyncMock()
    bot.delete_message = AsyncMock()
    bot.pin_chat_message = AsyncMock()
    return bot


def _driver_bot_app(bot, token="tok"):
    app = MagicMock()
    app.bot = bot
    tm = MagicMock()
    tm.get_valid_token = AsyncMock(return_value=token)
    app.bot_data = {"token_manager": tm}
    return app


def _delivery_payload(n=1):
    return {
        "items": [
            {
                "delivery_id": 10 + i, "order_number": f"10{i}", "status": "assigned",
                "customer_name": "U", "customer_phone": "+998900000001",
                "district": "Chilanzar", "address": "Street 1",
                "items": [], "total_amount": 10000, "payment_method": "cash",
                "amount_collected": 0, "outstanding_amount": 10000,
                "expected_cash_to_collect": 10000, "cod_reserved_prepayment_amount": 0,
                "destination_latitude": 41.31, "destination_longitude": 69.27,
                "route_position": i, "is_next": i == 0,
                "eta_minutes_from_current_location": None, "distance_km_to_next": None,
            }
            for i in range(n)
        ],
        "total": n,
        "location_status": "fresh",
        "route_summary": {
            "remaining": n, "stops_completed_today": 0, "stops_total_today": n,
            "committed_delivery_id": None, "finish_eta": None, "updated_at": None,
        },
    }


def _api_client_stub(payload, success=True):
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


@pytest.mark.unit
class TestHandleRouteUpdatedPayloadDelegatesSilentBranch:
    """Fast, boundary-mocked proof that the silent branch delegates to
    `route_card.update_card_for_driver` (Task 5's mechanics -- never
    reimplemented here), never touches send_message, and PROPAGATES that
    function's return value rather than hardcoding True (fix round 1, M2 +
    M4: `handle_route_updated_payload` no longer has an unconditional-True
    branch here, so these assertions are genuinely falsifiable). The real
    Telegram call-count proof, driving the actual function, lives in
    TestSilentBranchEditsCard below."""

    def test_silent_payload_refreshes_card_and_propagates_true(self, monkeypatch):
        """sound=False must NOT send a chat message, but MUST delegate to
        update_card_for_driver with the right args, and return what it
        returns."""
        fake_update = AsyncMock(return_value=True)
        monkeypatch.setattr(route_card, "update_card_for_driver", fake_update)

        server = StaffWebhookServer.__new__(StaffWebhookServer)
        server.bot_app = MagicMock()
        server.bot_app.bot = AsyncMock()

        payload = {
            "driver_id": 303, "telegram_id": 555, "event_id": "route_updated:abc123",
            "sound": False, "trigger": "delivery", "head_changed": False,
            "set_changed": False, "sequence_changed": True, "driver_initiated": True,
        }
        result = asyncio.run(handle_route_updated_payload(server, payload))

        server.bot_app.bot.send_message.assert_not_awaited()
        fake_update.assert_awaited_once_with(server.bot_app, 555)
        assert result is True

    def test_silent_branch_survives_a_refresh_that_could_not_happen(self, monkeypatch):
        """No cached token / borrowed card / API failure -> update_card_for_driver
        returns False. No crash, no message -- and the handler propagates
        that False (fix round 1, M4: previously this asserted `result is
        True`, which could never fail even if the code ignored the
        delegate's return entirely)."""
        fake_update = AsyncMock(return_value=False)
        monkeypatch.setattr(route_card, "update_card_for_driver", fake_update)

        server = StaffWebhookServer.__new__(StaffWebhookServer)
        server.bot_app = MagicMock()
        server.bot_app.bot = AsyncMock()

        payload = {
            "driver_id": 303, "telegram_id": 555, "event_id": "route_updated:def456",
            "sound": False, "trigger": "arrival", "head_changed": False,
            "set_changed": False, "sequence_changed": False, "driver_initiated": True,
        }
        result = asyncio.run(handle_route_updated_payload(server, payload))

        server.bot_app.bot.send_message.assert_not_awaited()
        fake_update.assert_awaited_once_with(server.bot_app, 555)
        assert result is False

    def test_dedup_hit_returns_false_meaning_no_new_action(self):
        """fix round 1, M4: an already-processed SOUNDED event is a genuine
        no-op -- no message, and the return value says so."""
        server = StaffWebhookServer.__new__(StaffWebhookServer)
        server.bot_app = MagicMock()
        server.bot_app.bot = AsyncMock()
        server._is_duplicate_event = AsyncMock(return_value=True)

        payload = {'telegram_id': 555, 'driver_id': 303,
                   'event_id': 'route_updated:dup1', 'sound': True}
        result = asyncio.run(handle_route_updated_payload(server, payload))

        assert result is False
        server.bot_app.bot.send_message.assert_not_awaited()


@pytest.mark.unit
class TestSilentBranchEditsCard:
    """End-to-end through `route_updated_handler` -> `handle_route_updated_payload`
    -> the REAL `route_card.update_card_for_driver` (nothing mocked at that
    boundary). This is the headline proof: a non-sound-worthy update must
    produce zero send_message calls and exactly one edit_message_text when
    a card already exists, and send+pin (both disable_notification=True) on
    the one permitted exception -- first-contact creation."""

    @pytest.fixture(autouse=True)
    def _reset_card_state(self):
        route_card_state.configure(_FakeRedis())
        route_card_state._locks.clear()
        yield
        route_card_state.configure(None)
        route_card_state._locks.clear()

    def test_silent_update_with_existing_card_edits_and_sends_nothing(self, monkeypatch):
        """Headline assertion: an already-carded driver's silent update
        produces ZERO send_message calls and exactly ONE edit_message_text,
        and never pins (pinning only ever happens on create)."""
        asyncio.run(route_card_state.save(555, {
            "chat_id": 555, "message_id": 900, "card_date": route_card.local_date_str(),
            "view": route_card_state.VIEW_NEXT, "content_sig": "stale-sig",
        }))
        bot = _wired_bot()
        server = _server_with_bot()
        server.bot_app = _driver_bot_app(bot)
        monkeypatch.setattr(route_card, "api_client", _api_client_stub(_delivery_payload(2)))
        monkeypatch.setattr(route_card, "_get_user_language", AsyncMock(return_value="en"))

        payload = {
            "driver_id": 303, "telegram_id": 555, "event_id": "route_updated:abc123",
            "sound": False, "trigger": "delivery", "head_changed": False,
            "set_changed": False, "sequence_changed": True, "driver_initiated": True,
        }
        with patch('staff_bot.webhook_server.verify_webhook_signature',
                   AsyncMock(return_value=True)):
            resp = asyncio.run(server.route_updated_handler(_request_with(payload)))

        assert resp.status == 200
        bot.send_message.assert_not_awaited()
        bot.edit_message_text.assert_awaited_once()
        bot.pin_chat_message.assert_not_called()

    def test_silent_update_first_contact_creates_card_and_pins_it(self, monkeypatch):
        """A driver with no card yet: TWO Telegram calls happen -- a silent
        send AND a silent pin (fix round 1, M6: the report previously
        under-counted this as one call). Both carry
        disable_notification=True; edit_message_text is never touched --
        create and edit are mutually exclusive."""
        bot = _wired_bot(next_message_id=42)
        server = _server_with_bot()
        server.bot_app = _driver_bot_app(bot)
        monkeypatch.setattr(route_card, "api_client", _api_client_stub(_delivery_payload(1)))
        monkeypatch.setattr(route_card, "_get_user_language", AsyncMock(return_value="en"))

        payload = {
            "driver_id": 303, "telegram_id": 555, "event_id": "route_updated:xyz999",
            "sound": False, "trigger": "accept", "head_changed": False,
            "set_changed": True, "sequence_changed": False, "driver_initiated": True,
        }
        with patch('staff_bot.webhook_server.verify_webhook_signature',
                   AsyncMock(return_value=True)):
            resp = asyncio.run(server.route_updated_handler(_request_with(payload)))

        assert resp.status == 200
        bot.send_message.assert_awaited_once()
        assert bot.send_message.await_args.kwargs["disable_notification"] is True
        bot.pin_chat_message.assert_awaited_once()
        pin_kwargs = bot.pin_chat_message.await_args.kwargs
        assert pin_kwargs["message_id"] == 42
        assert pin_kwargs["disable_notification"] is True
        bot.edit_message_text.assert_not_called()

    def test_silent_update_borrowed_from_the_start_short_circuits_at_the_cheap_precheck(
        self, monkeypatch,
    ):
        """fix round 1, item 3: a card ALREADY borrowed before this push
        starts exits at `update_card_for_driver`'s cheap pre-check, before
        the token lookup or the API fetch -- this is only the fast-path
        optimization, NOT the authoritative guarantee (that's the race test
        below). Proven by asserting the API was never even touched, not
        just that no message was sent."""
        asyncio.run(route_card_state.save(555, {
            "chat_id": 555, "message_id": 900, "card_date": route_card.local_date_str(),
            "view": route_card_state.VIEW_BORROWED, "content_sig": "x",
        }))
        bot = _wired_bot()
        server = _server_with_bot()
        server.bot_app = _driver_bot_app(bot)
        api_stub = _api_client_stub(_delivery_payload(1))
        monkeypatch.setattr(route_card, "api_client", api_stub)

        payload = {'telegram_id': 555, 'driver_id': 303, 'sound': False}
        with patch('staff_bot.webhook_server.verify_webhook_signature',
                   AsyncMock(return_value=True)):
            resp = asyncio.run(server.route_updated_handler(_request_with(payload)))

        assert resp.status == 200
        bot.send_message.assert_not_called()
        bot.edit_message_text.assert_not_called()
        bot.pin_chat_message.assert_not_called()
        api_stub.client.get_active_deliveries.assert_not_called()

    def test_silent_update_borrow_landing_during_the_fetch_wins_the_race(self, monkeypatch):
        """fix round 1, item 3: the AUTHORITATIVE check. Seed state NOT
        borrowed (so the cheap pre-check above passes through), then have
        the mocked API call itself mark the card borrowed -- simulating a
        driver's tap landing during the awaited HTTP round trip, before
        `render_route_card` acquires its per-driver lock (mirrors
        test_route_card_render.py::TestWebhookEntry::test_borrow_landing_during_the_api_call_wins_the_race).
        The in-lock recheck must still catch it: no edit, no send, no pin."""
        asyncio.run(route_card_state.save(555, {
            "chat_id": 555, "message_id": 900, "card_date": route_card.local_date_str(),
            "view": route_card_state.VIEW_NEXT, "content_sig": "stale-sig",
        }))
        bot = _wired_bot()
        server = _server_with_bot()
        server.bot_app = _driver_bot_app(bot)

        class _RacyClient:
            async def __aenter__(self):
                client = MagicMock()

                async def _get_active_deliveries(token):
                    # The race window: the pre-check already passed. A
                    # concurrent driver tap wins right here, before
                    # render_route_card acquires the lock.
                    await route_card_state.mark_borrowed(555)
                    return MagicMock(success=True, data=_delivery_payload(2))

                client.get_active_deliveries = AsyncMock(side_effect=_get_active_deliveries)
                return client

            async def __aexit__(self, *a):
                return False

        monkeypatch.setattr(route_card, "api_client", _RacyClient())
        monkeypatch.setattr(route_card, "_get_user_language", AsyncMock(return_value="en"))

        payload = {'telegram_id': 555, 'driver_id': 303, 'sound': False}
        with patch('staff_bot.webhook_server.verify_webhook_signature',
                   AsyncMock(return_value=True)):
            resp = asyncio.run(server.route_updated_handler(_request_with(payload)))

        assert resp.status == 200
        bot.send_message.assert_not_called()
        bot.edit_message_text.assert_not_called()
        bot.pin_chat_message.assert_not_called()

    def test_silent_update_no_cached_token_is_a_safe_no_op(self):
        """No cached token (driver never logged in, or refresh token expired):
        the webhook still reports success, and nothing is sent -- a card
        that cannot be refreshed is strictly better than a wrong one."""
        bot = _wired_bot()
        server = _server_with_bot()
        server.bot_app = _driver_bot_app(bot, token=None)

        payload = {'telegram_id': 555, 'driver_id': 303, 'sound': False}
        with patch('staff_bot.webhook_server.verify_webhook_signature',
                   AsyncMock(return_value=True)):
            resp = asyncio.run(server.route_updated_handler(_request_with(payload)))

        assert resp.status == 200
        bot.send_message.assert_not_called()
        bot.edit_message_text.assert_not_called()
        bot.pin_chat_message.assert_not_called()

    def test_silent_update_during_redis_outage_short_circuits_at_token_check(self, monkeypatch):
        """fix round 1 correction: the original version of this test pinned
        an IMPOSSIBLE state -- Redis down for `route_card_state` but somehow
        still serving a valid cached token. `staff_bot/bot.py` wires
        `route_card_state` to the TOKEN MANAGER'S OWN Redis client, so a
        real outage kills both on the same connection.
        `TokenManager.get_cached_tokens` catches the connection failure and
        returns None -> `get_valid_token` returns None -- and
        `update_card_for_driver` checks that BEFORE any fetch or render.
        Modeled here by making BOTH the card-state store unavailable AND
        the cached token None, matching what one dead shared client
        actually produces. Result across 5 pushes: 0 send / 0 edit / 0 pin
        -- NOT the "unpinned create every push" this test previously
        (incorrectly) asserted -- and the API is never even reached."""
        route_card_state.configure(None)  # the shared connection is down for both
        bot = _wired_bot()
        server = _server_with_bot()
        server.bot_app = _driver_bot_app(bot, token=None)  # dead Redis => no cached token
        api_stub = _api_client_stub(_delivery_payload(1))
        monkeypatch.setattr(route_card, "api_client", api_stub)

        payload = {'telegram_id': 555, 'driver_id': 303, 'sound': False}
        with patch('staff_bot.webhook_server.verify_webhook_signature',
                   AsyncMock(return_value=True)):
            for _ in range(5):
                resp = asyncio.run(server.route_updated_handler(_request_with(payload)))
                assert resp.status == 200

        bot.send_message.assert_not_called()
        bot.edit_message_text.assert_not_called()
        bot.pin_chat_message.assert_not_called()
        api_stub.client.get_active_deliveries.assert_not_called()

    def test_silent_update_state_key_eviction_creates_and_pins_every_push(self, monkeypatch):
        """fix round 1: the narrower, genuinely reachable variant the
        reviewer measured alongside the full outage above. The shared
        connection stays up and the token cache (a different key, its own
        TTL) stays warm, but the route-card state KEY itself never survives
        to the next read -- e.g. an eviction or TTL misconfiguration landing
        on exactly that key pattern. Unlike the full outage (0/0/0 -- the
        token check short-circuits first), THIS one DOES reach render, and
        DOES pin, on every single push: with no persisted state ever found,
        every push takes the create branch. 5 pushes -> 5 sends + 5 pins,
        0 edits -- contradicting "unpinned" as a blanket claim about any
        Redis degradation."""
        route_card_state.configure(_EvictingCardStateRedis())
        bot = _wired_bot()
        server = _server_with_bot()
        server.bot_app = _driver_bot_app(bot)  # token cache unaffected -- still "tok"
        monkeypatch.setattr(route_card, "api_client", _api_client_stub(_delivery_payload(1)))
        monkeypatch.setattr(route_card, "_get_user_language", AsyncMock(return_value="en"))

        payload = {'telegram_id': 555, 'driver_id': 303, 'sound': False}
        with patch('staff_bot.webhook_server.verify_webhook_signature',
                   AsyncMock(return_value=True)):
            for _ in range(5):
                resp = asyncio.run(server.route_updated_handler(_request_with(payload)))
                assert resp.status == 200

        assert bot.send_message.await_count == 5
        assert bot.pin_chat_message.await_count == 5
        bot.edit_message_text.assert_not_called()
        for call in bot.send_message.await_args_list:
            assert call.kwargs["disable_notification"] is True
        for call in bot.pin_chat_message.await_args_list:
            assert call.kwargs["disable_notification"] is True


class _FakeRedis:
    """Just enough of redis.asyncio for set/get/delete with ex= -- mirrors
    tests/staff_bot/test_route_card_render.py's fake, kept local so this
    file stays self-contained."""

    def __init__(self):
        self.store = {}

    async def set(self, key, value, ex=None):
        self.store[key] = value

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        self.store.pop(key, None)


class _EvictingCardStateRedis(_FakeRedis):
    """Redis is alive (it is the SAME connection token_manager uses -- see
    staff_bot/bot.py), but this specific route-card-state key never sticks
    across a read: `set` "succeeds" but `get` always reports it missing.
    Models an eviction/TTL landing on exactly this key pattern while the
    token-cache keys, on their own TTLs, survive untouched on the same
    client."""

    async def set(self, key, value, ex=None):
        pass  # the write reaches Redis but the key never survives to the next read

    async def get(self, key):
        return None


@pytest.mark.unit
class TestUpdateCardForDriverReferenceMessageIdPassthrough:
    """fix round 1, M2: Task 9's sounded-alert flow is expected to call
    `update_card_for_driver` too, and unlike the silent webhook branch (which
    never reposts) it DOES have a reference message to anchor a
    repost-when-buried decision against. Added as a pure passthrough --
    default None, so every existing caller (including Task 8's own silent
    branch) is unaffected -- rather than left as an undocumented gap for
    Task 9 to rediscover."""

    @pytest.fixture(autouse=True)
    def _reset_card_state(self):
        route_card_state.configure(_FakeRedis())
        route_card_state._locks.clear()
        yield
        route_card_state.configure(None)
        route_card_state._locks.clear()

    def _patch_render_capture(self, monkeypatch, captured):
        async def fake_render(bot, *, telegram_id, chat_id, language, payload,
                               view=None, reference_message_id=None, **kwargs):
            captured["reference_message_id"] = reference_message_id
            # Must return a RenderOutcome, not a bare bool: update_card_for_driver
            # narrows via `outcome in (RenderOutcome.RENDERED, RenderOutcome.NOOP)`,
            # and `True in (...)` is False (str-Enum equality against a plain
            # bool falls back to identity) -- a bare `True` here would silently
            # invert the assertions below.
            return route_card.RenderOutcome.RENDERED

        monkeypatch.setattr(route_card, "render_route_card", fake_render)
        monkeypatch.setattr(route_card, "api_client", _api_client_stub(_delivery_payload(1)))
        monkeypatch.setattr(route_card, "_get_user_language", AsyncMock(return_value="en"))

    def test_reference_message_id_flows_through_to_render_route_card(self, monkeypatch):
        captured = {}
        self._patch_render_capture(monkeypatch, captured)
        bot_app = _driver_bot_app(_wired_bot())

        ok = asyncio.run(route_card.update_card_for_driver(
            bot_app, 555, reference_message_id=4321,
        ))

        assert ok is True
        assert captured["reference_message_id"] == 4321

    def test_reference_message_id_defaults_to_none(self, monkeypatch):
        captured = {}
        self._patch_render_capture(monkeypatch, captured)
        bot_app = _driver_bot_app(_wired_bot())

        ok = asyncio.run(route_card.update_card_for_driver(bot_app, 555))

        assert ok is True
        assert captured["reference_message_id"] is None


@pytest.mark.unit
def test_sounded_branch_now_sends_the_capped_head_change_alert(monkeypatch):
    """Fix round 1, item 3: this test used to pin the OLD toast+button as
    "Task 9's territory, unaffected" -- correct while Task 8 owned this file
    and Task 9 hadn't wired anything in yet. Task 9 now owns this branch for
    real (`handle_route_updated_payload` calls `route_card.send_head_change_alert`),
    so this pins the NEW behaviour instead: still exactly one send at this
    shallow mocking level (no token manager wired -> the card-refresh half
    short-circuits, same as the other shallow sound=True tests above). The
    genuine end-to-end proof, with a real card and a real `update_card_for_driver`,
    is `TestSoundedBranchSendsCappedAlert` below."""
    monkeypatch.setattr(route_card, "_get_user_language", AsyncMock(return_value="en"))
    server = _server_with_bot()
    payload = {'telegram_id': 777, 'driver_id': 5, 'sound': True,
               'head_changed': True, 'driver_initiated': False}
    with patch('staff_bot.webhook_server.verify_webhook_signature',
               AsyncMock(return_value=True)), \
         patch('staff_bot.webhook_server.i18n', _i18n_stub()):
        resp = asyncio.run(server.route_updated_handler(_request_with(payload)))
    assert resp.status == 200
    server.bot_app.bot.send_message.assert_called_once()


@pytest.mark.unit
class TestSoundedBranchSendsCappedAlert:
    """Fix round 1, items 1 + 3: end-to-end through `route_updated_handler` ->
    `handle_route_updated_payload` -> the REAL `route_card.send_head_change_alert`
    -> the REAL `route_card.update_card_for_driver` (nothing mocked at either
    boundary) -- proves the wiring itself (Task 9 was previously dead code:
    no production caller) and measures genuine Telegram call counts for the
    four scenarios the coordinator asked for. An existing (non-borrowed,
    unless noted) card is seeded so the refresh takes the EDIT branch, which
    keeps `edit_message_text` unambiguous from the alert's own `send_message`.

    Dedup is bypassed here (`_is_duplicate_event` forced False) -- that gate
    is already proven independently by `test_dedup_hit_returns_false_meaning_no_new_action`
    above; mixing it in here would blur what each test is actually proving.

    Message ids below are realistic and monotonic (fix round 2, item 2's
    fixture note): the card (900), any previous alert (920), and a freshly
    sent alert (950) are strictly increasing, matching real Telegram
    per-chat message ids. The original values (card 900, alert send 100)
    were impossible on Telegram and would have hidden a regression that
    re-enables `render_route_card`'s repost heuristic on this path --
    negative `reference_message_id - state["message_id"]` never triggers a
    repost regardless of the bug, so these tests would have kept passing
    even with the old bug back.
    """

    @pytest.fixture(autouse=True)
    def _reset_card_state(self, monkeypatch):
        route_card_state.configure(_FakeRedis())
        route_card_state._locks.clear()
        monkeypatch.setattr(route_card, "_get_user_language", AsyncMock(return_value="en"))
        yield
        route_card_state.configure(None)
        route_card_state._locks.clear()

    @staticmethod
    def _existing_card_state(view=route_card_state.VIEW_NEXT):
        return {
            "chat_id": 555, "message_id": 900, "card_date": route_card.local_date_str(),
            "view": view, "content_sig": "stale-sig",
        }

    @staticmethod
    def _server(bot):
        server = _server_with_bot()
        server.bot_app = _driver_bot_app(bot)
        server._is_duplicate_event = AsyncMock(return_value=False)
        return server

    @staticmethod
    def _payload(event_id):
        return {
            "driver_id": 303, "telegram_id": 555, "event_id": event_id,
            "sound": True, "trigger": "delivery", "head_changed": True,
            "set_changed": False, "sequence_changed": False, "driver_initiated": False,
        }

    def test_first_alert_pings_and_edits_the_card(self, monkeypatch):
        """No prior `last_alert_at` -> uncapped: 1 send (pings) + 1 edit,
        0 delete, 0 pin."""
        asyncio.run(route_card_state.save(555, self._existing_card_state()))
        bot = _wired_bot(next_message_id=950)
        server = self._server(bot)
        monkeypatch.setattr(route_card, "api_client", _api_client_stub(_delivery_payload(2)))

        with patch('staff_bot.webhook_server.verify_webhook_signature',
                   AsyncMock(return_value=True)):
            resp = asyncio.run(server.route_updated_handler(_request_with(self._payload("evt-1"))))

        assert resp.status == 200
        bot.send_message.assert_awaited_once()
        assert bot.send_message.await_args.kwargs["disable_notification"] is False
        bot.edit_message_text.assert_awaited_once()
        bot.delete_message.assert_not_called()
        bot.pin_chat_message.assert_not_called()

    def test_superseding_alert_inside_window_deletes_old_and_edits_the_card(self, monkeypatch):
        """last_alert_at 30s ago (< 300s default) -> capped: delete the old
        alert, send the new one silently, still 1 edit for the card. 1
        delete + 1 send + 1 edit, 0 pin."""
        state = self._existing_card_state()
        state["last_alert_at"] = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
        state["last_alert_message_id"] = 920
        asyncio.run(route_card_state.save(555, state))
        bot = _wired_bot(next_message_id=950)
        server = self._server(bot)
        monkeypatch.setattr(route_card, "api_client", _api_client_stub(_delivery_payload(2)))

        with patch('staff_bot.webhook_server.verify_webhook_signature',
                   AsyncMock(return_value=True)):
            resp = asyncio.run(server.route_updated_handler(_request_with(self._payload("evt-2"))))

        assert resp.status == 200
        bot.delete_message.assert_awaited_once()
        assert bot.delete_message.await_args.kwargs["message_id"] == 920
        bot.send_message.assert_awaited_once()
        assert bot.send_message.await_args.kwargs["disable_notification"] is True
        bot.edit_message_text.assert_awaited_once()
        bot.pin_chat_message.assert_not_called()

    def test_alert_after_the_window_pings_again_and_edits_the_card(self, monkeypatch):
        """last_alert_at 1h ago (> 300s default) -> uncapped again: no delete,
        1 send (pings) + 1 edit, 0 pin."""
        state = self._existing_card_state()
        state["last_alert_at"] = (datetime.now(timezone.utc) - timedelta(seconds=3600)).isoformat()
        state["last_alert_message_id"] = 920
        asyncio.run(route_card_state.save(555, state))
        bot = _wired_bot(next_message_id=950)
        server = self._server(bot)
        monkeypatch.setattr(route_card, "api_client", _api_client_stub(_delivery_payload(2)))

        with patch('staff_bot.webhook_server.verify_webhook_signature',
                   AsyncMock(return_value=True)):
            resp = asyncio.run(server.route_updated_handler(_request_with(self._payload("evt-3"))))

        assert resp.status == 200
        bot.delete_message.assert_not_called()
        bot.send_message.assert_awaited_once()
        assert bot.send_message.await_args.kwargs["disable_notification"] is False
        bot.edit_message_text.assert_awaited_once()
        bot.pin_chat_message.assert_not_called()

    def test_alert_while_borrowed_fires_but_card_edit_is_skipped(self, monkeypatch):
        """Borrowed card: the alert still fires (it is a distinct message),
        but the card edit is skipped -- `update_card_for_driver`'s own
        pre-check returns before the API is ever touched. 1 send, 0 edit,
        0 delete (no prior alert), 0 pin, API never called."""
        asyncio.run(route_card_state.save(555, self._existing_card_state(
            view=route_card_state.VIEW_BORROWED,
        )))
        bot = _wired_bot(next_message_id=950)
        server = self._server(bot)
        api_stub = _api_client_stub(_delivery_payload(2))
        monkeypatch.setattr(route_card, "api_client", api_stub)

        with patch('staff_bot.webhook_server.verify_webhook_signature',
                   AsyncMock(return_value=True)):
            resp = asyncio.run(server.route_updated_handler(_request_with(self._payload("evt-4"))))

        assert resp.status == 200
        bot.send_message.assert_awaited_once()
        assert bot.send_message.await_args.kwargs["disable_notification"] is False
        bot.edit_message_text.assert_not_called()
        bot.delete_message.assert_not_called()
        bot.pin_chat_message.assert_not_called()
        api_stub.client.get_active_deliveries.assert_not_called()


@pytest.mark.unit
class TestMalformedTelegramId:
    """fix round 1, M1: a malformed telegram_id must fail loud (400), not
    silently succeed (200) -- the old shape let `int(data['telegram_id'])`
    raise deep inside the best-effort try/except that swallows Telegram-side
    failures, hiding a genuine backend bug behind a success response."""

    def test_non_numeric_telegram_id_returns_400_not_200(self):
        server = _server_with_bot()
        payload = {'telegram_id': 'not-a-number', 'driver_id': 5, 'sound': False}
        with patch('staff_bot.webhook_server.verify_webhook_signature',
                   AsyncMock(return_value=True)):
            resp = asyncio.run(server.route_updated_handler(_request_with(payload)))
        assert resp.status == 400
        server.bot_app.bot.send_message.assert_not_called()

    def test_missing_telegram_id_still_returns_400(self):
        """Unchanged pre-existing behaviour -- guards against a regression
        while adding the malformed-value check next to it."""
        server = _server_with_bot()
        payload = {'driver_id': 5, 'sound': False}
        with patch('staff_bot.webhook_server.verify_webhook_signature',
                   AsyncMock(return_value=True)):
            resp = asyncio.run(server.route_updated_handler(_request_with(payload)))
        assert resp.status == 400
