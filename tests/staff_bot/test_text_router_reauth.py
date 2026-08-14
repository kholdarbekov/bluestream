"""A reply keyboard lives on the driver's phone, not in the bot's memory.
`context.user_data['authenticated']` is process memory with no PTB
persistence configured, so after every restart or deploy the keyboard is
still there and every tap was silently swallowed.

Recovery is scoped to text that IS a menu label (in any supported
language): the underlying recovery path costs a Redis read plus an
unrate-limited signed POST to /api/staff/auth/login plus two DB queries, so
running it for arbitrary text would let a stranger who never ran /start
drive backend load just by typing at the bot. A failed recovery is also
rate-bounded per user so replaying one label in a loop can't sustain
continuous load."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from staff_bot.bot import StaffBot
from staff_bot.i18n import i18n
from staff_bot.permissions import require_auth, require_delivery_driver


def _menu_tap_text():
    """The exact text the reply keyboard emits for the Active Deliveries
    button, built from the real (DB-backed) translation the same way the
    keyboard does -- see staff_bot/keyboards/menu.py. Deliberately not
    hardcoded: the bot's guard now matches this text against the real,
    DB-backed staff.menu.* labels via _main_menu_text_pattern(), so a
    literal string here would silently drift out of sync with the seeded
    translation content."""
    return f"\U0001F69A {i18n.get('staff.menu.active_deliveries', 'en')}"


def _update(text=None):
    update = MagicMock()
    update.message.text = text if text is not None else _menu_tap_text()
    update.message.delete = AsyncMock()
    update.message.reply_text = AsyncMock()
    update.message.message_id = 101
    # A normal (non-edited) message: effective_message resolves to message,
    # exactly like a real python-telegram-bot Update would.
    update.effective_message = update.message
    # Not a callback: a rejected permission guard must surface as a
    # reply_text we can assert on, not a TypeError awaiting a MagicMock
    # `callback_query.answer`.
    update.callback_query = None
    update.effective_user.id = 777
    return update


@pytest.mark.unit
class TestUnauthenticatedTap:
    def test_tap_after_restart_is_served_when_reauth_succeeds(self):
        bot = StaffBot.__new__(StaffBot)
        dispatched = []
        update, context = _update(), MagicMock()
        context.user_data = {}  # restart wiped it

        async def _reauth(upd, ctx):
            ctx.user_data['authenticated'] = True
            return "tok"

        bot._recover_session = _reauth
        bot._language_handler = MagicMock()
        bot._language_handler._get_language = AsyncMock(return_value="en")
        bot._match_menu_action = lambda text, lang: "staff_active_deliveries"
        bot._clear_all_pending_flows = AsyncMock()
        bot._delete_menu_echo = AsyncMock()
        bot._dispatch_menu_action = AsyncMock(side_effect=lambda *a: dispatched.append(a[0]))

        asyncio.run(bot._handle_text_message(update, context))
        assert dispatched == ["staff_active_deliveries"]

    def test_tap_gets_an_explanation_when_reauth_fails(self):
        bot = StaffBot.__new__(StaffBot)
        update, context = _update(), MagicMock()
        context.user_data = {}
        bot._recover_session = AsyncMock(return_value=None)
        bot._language_handler = MagicMock()
        bot._language_handler._get_language = AsyncMock(return_value="en")
        bot._dispatch_menu_action = AsyncMock()

        asyncio.run(bot._handle_text_message(update, context))

        update.message.reply_text.assert_awaited_once()
        sent = update.message.reply_text.await_args.args[0]
        assert sent == i18n.get('staff.session_expired', 'en')
        bot._dispatch_menu_action.assert_not_called()

    def test_non_menu_text_from_a_stranger_is_ignored_without_any_recovery_attempt(self):
        """The regression the review caught: before this test, ANY text from
        ANY unauthenticated sender (not just a real menu-label tap) triggered
        the full recovery chain -- Redis GET, an unrate-limited signed POST
        to /api/staff/auth/login, and two DB queries. A stranger's 'hello' is
        not a button; it must be dropped with zero backend calls, exactly as
        it was before _recover_session existed."""
        bot = StaffBot.__new__(StaffBot)
        update, context = _update(text="hello"), MagicMock()
        context.user_data = {}
        bot._recover_session = AsyncMock()
        bot._language_handler = MagicMock()
        bot._language_handler._get_language = AsyncMock(return_value="en")
        bot._dispatch_menu_action = AsyncMock()

        asyncio.run(bot._handle_text_message(update, context))

        bot._recover_session.assert_not_called()
        update.message.reply_text.assert_not_called()
        bot._dispatch_menu_action.assert_not_called()

    def test_second_failed_recovery_inside_cooldown_skips_the_auth_call(self):
        """FIX 1b: bound the residual. An attacker who knows a real menu
        label can still loop on it, so a FAILED recovery must not be retried
        immediately -- otherwise one label replayed in a loop is an
        unthrottled signed POST to the backend auth endpoint per message."""
        bot = StaffBot.__new__(StaffBot)
        update = _update()
        context = MagicMock()

        with patch(
            "staff_bot.handlers.base.BaseHandler._authenticate_staff_session",
            new=AsyncMock(return_value=None),
        ) as mocked_auth:
            first = asyncio.run(bot._recover_session(update, context))
            second = asyncio.run(bot._recover_session(update, context))

        assert first is None
        assert second is None
        assert mocked_auth.await_count == 1

    def test_repeat_taps_inside_the_cooldown_produce_exactly_one_explanation(self):
        """The reply is gated on the SAME cooldown as the auth attempt. A
        driver whose session cannot be recovered taps the (dead-looking)
        keyboard repeatedly; without this gate each tap costs a notification
        and an undeleted message that buries the pinned route card. They
        already have the explanation -- repeating it is pure noise."""
        bot = StaffBot.__new__(StaffBot)
        context = MagicMock()
        context.user_data = {}
        bot._language_handler = MagicMock()
        bot._language_handler._get_language = AsyncMock(return_value="en")
        bot._dispatch_menu_action = AsyncMock()

        replies = []
        with patch(
            "staff_bot.handlers.base.BaseHandler._authenticate_staff_session",
            new=AsyncMock(return_value=None),
        ):
            for _ in range(5):
                update = _update()
                asyncio.run(bot._handle_text_message(update, context))
                replies.extend(update.message.reply_text.await_args_list)

        assert len(replies) == 1
        assert replies[0].args[0] == i18n.get('staff.session_expired', 'en')

    def test_the_explanation_is_silent(self):
        """Invariant: drivers are driving. Only the head-change alert is
        allowed to make a sound."""
        bot = StaffBot.__new__(StaffBot)
        update, context = _update(), MagicMock()
        context.user_data = {}
        bot._recover_session = AsyncMock(return_value=None)
        bot._language_handler = MagicMock()
        bot._language_handler._get_language = AsyncMock(return_value="en")
        bot._dispatch_menu_action = AsyncMock()

        asyncio.run(bot._handle_text_message(update, context))

        assert update.message.reply_text.await_args.kwargs["disable_notification"] is True


class _CachedTokenManager:
    """A TokenManager holding a still-valid token in REDIS -- which survives
    a bot restart, unlike `context.user_data`. This is the exact production
    state the first draft of the restart fix broke on."""

    def __init__(self):
        self.get_valid_token = AsyncMock(return_value="cached-live-token")
        self.store_tokens = AsyncMock()
        self.invalidate_tokens = AsyncMock()


class _StaffLoginApi:
    """Stands in for `staff_bot.api_client.api_client` (imported inside
    `_authenticate_staff_session`, so it must be patched at its source)."""

    def __init__(self, roles=("delivery_driver",)):
        self.client = MagicMock()
        self.client.staff_login = AsyncMock(return_value=MagicMock(
            success=True,
            status_code=200,
            data={
                "access_token": "fresh-session-token",
                "refresh_token": "fresh-refresh-token",
                "expires_in": 3600,
                "user": {
                    "id": 5,
                    "staff_roles": list(roles),
                    "delivery_person_id": 3,
                    "preferred_language": "en",
                },
            },
        ))

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, *a):
        return False


class _GuardedHandler:
    """Stand-in for ActiveDeliveryHandler carrying the REAL permission
    decorators. "The dispatch happened" only means something if the guards
    actually let it through, and they need BOTH `authenticated` (require_auth)
    and `staff_roles` (require_delivery_driver)."""

    def __init__(self):
        self.served = []

    @require_auth
    @require_delivery_driver
    async def show_active_deliveries(self, update, context):
        self.served.append(list(context.user_data.get('staff_roles') or []))


@pytest.mark.unit
class TestRecoverSessionRealBody:
    """The tests above stub `_recover_session` wholesale, so what the real
    body delegates to is invisible to them. These exercise it for real."""

    def test_returns_the_token_on_success(self):
        bot = StaffBot.__new__(StaffBot)
        update = _update()
        context = MagicMock()

        with patch(
            "staff_bot.handlers.base.BaseHandler._authenticate_staff_session",
            new=AsyncMock(return_value="tok-123"),
        ):
            result = asyncio.run(bot._recover_session(update, context))

        assert result == "tok-123"

    def test_returns_none_and_does_not_propagate_when_the_auth_call_raises(self):
        bot = StaffBot.__new__(StaffBot)
        update = _update()
        context = MagicMock()

        with patch(
            "staff_bot.handlers.base.BaseHandler._authenticate_staff_session",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            result = asyncio.run(bot._recover_session(update, context))  # must not raise

        assert result is None

    def test_restart_tap_with_a_live_cached_token_reaches_the_guarded_handler(self):
        """THE regression this whole branch exists to prevent, driven end to
        end through the real router, the real `_recover_session`, the real
        `_authenticate_staff_session` and the REAL permission decorators.

        `BaseHandler._get_auth_token` -- the obvious thing to delegate to --
        returns a TokenManager-cached token from its FIRST branch without
        ever setting `authenticated` or `staff_roles`. Redis outlives the bot
        process, so on the very deploy this fix ships for, that branch is the
        one that runs: recovery would hand back a truthy token, the router
        would fall through and dispatch, and `@require_auth` would answer
        'session expired' on every tap forever. Asserting that recovery
        returned something truthy cannot see that; asserting the guarded
        handler RAN can."""
        handler = _GuardedHandler()
        token_manager = _CachedTokenManager()

        bot = StaffBot.__new__(StaffBot)
        bot._language_handler = MagicMock()
        bot._language_handler._get_language = AsyncMock(return_value="en")
        bot._clear_all_pending_flows = AsyncMock()
        bot._delete_menu_echo = AsyncMock()
        bot._delivery_handlers = {
            "active_delivery": handler,
            "status_update": MagicMock(),
            "tryouts": MagicMock(),
        }
        bot._common_handlers = {"profile": MagicMock(), "help": MagicMock()}

        update = _update()
        context = MagicMock()
        context.user_data = {}  # restart wiped it; the reply keyboard did not
        context.bot_data = {"token_manager": token_manager}

        with patch("staff_bot.api_client.api_client", _StaffLoginApi()):
            asyncio.run(bot._handle_text_message(update, context))

        assert handler.served == [["delivery_driver"]], (
            "the dispatched, permission-guarded handler never ran -- recovery "
            "returned a token without establishing a session"
        )
        assert context.user_data["authenticated"] is True
        assert context.user_data["staff_roles"] == ["delivery_driver"]
        update.message.reply_text.assert_not_called()
