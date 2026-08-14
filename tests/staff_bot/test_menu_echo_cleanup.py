"""A reply-keyboard tap sends a plain TEXT message, which piles up in the
chat and buries the pinned route card. The bot deletes its own menu echoes
-- Telegram: "Bots can delete incoming messages in private chats", within
48 hours.

Ordering is the load-bearing part: the delete happens AFTER the dispatched
handler produced its output. Deleting first would mean a failed render
leaves the driver with a tap that vanished into literally nothing, which is
strictly worse than the bug being fixed."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from staff_bot.bot import StaffBot
from staff_bot.handlers.delivery import active_delivery as active_delivery_mod
from staff_bot.handlers.delivery.active_delivery import ActiveDeliveryHandler
from staff_bot.i18n import i18n
from staff_bot.utils import route_card_state


class _FakeRedis:
    def __init__(self):
        self.store = {}

    async def set(self, key, value, ex=None):
        self.store[key] = value

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        self.store.pop(key, None)


@pytest.fixture(autouse=True)
def _state():
    route_card_state.configure(_FakeRedis())
    route_card_state._locks.clear()
    yield
    route_card_state.configure(None)
    route_card_state._locks.clear()


def _update(text="🚚 Active Deliveries", message_id=101):
    update = MagicMock()
    update.message.text = text
    update.message.message_id = message_id
    update.message.delete = AsyncMock()
    # A real text Update: effective_message aliases message, and there is no
    # callback_query. Both are load-bearing for _delete_menu_echo.
    update.effective_message = update.message
    update.callback_query = None
    update.effective_user.id = 777
    return update


@pytest.mark.unit
class TestMenuEchoCleanup:
    def test_delete_menu_echo_deletes_the_message(self):
        bot = StaffBot.__new__(StaffBot)
        order = []
        update = _update()
        update.message.delete = AsyncMock(side_effect=lambda: order.append("delete"))

        asyncio.run(bot._delete_menu_echo(update))
        assert order == ["delete"]  # _delete_menu_echo itself only deletes

    def test_delete_failure_is_swallowed(self):
        bot = StaffBot.__new__(StaffBot)
        update = _update()
        update.message.delete = AsyncMock(side_effect=RuntimeError("too old"))
        asyncio.run(bot._delete_menu_echo(update))  # must not raise

    def test_successful_delete_increments_the_echo_counter(self):
        asyncio.run(route_card_state.save(777, {"chat_id": 777, "message_id": 100}))
        bot = StaffBot.__new__(StaffBot)
        asyncio.run(bot._delete_menu_echo(_update(message_id=101)))
        assert asyncio.run(route_card_state.load(777))["echoes_deleted"] == 1

    def test_failed_delete_does_not_increment_the_counter(self):
        asyncio.run(route_card_state.save(777, {"chat_id": 777, "message_id": 100}))
        bot = StaffBot.__new__(StaffBot)
        update = _update()
        update.message.delete = AsyncMock(side_effect=RuntimeError("too old"))
        asyncio.run(bot._delete_menu_echo(update))
        assert asyncio.run(route_card_state.load(777)).get("echoes_deleted") in (None, 0)

    def test_echo_above_the_card_is_deleted_but_not_counted(self):
        """The create case: the tap that triggered the card has a LOWER id
        than the card the render then sent. The echo must still be removed,
        but it never buried the card, so counting it would inflate the repost
        threshold for that card's whole life."""
        asyncio.run(route_card_state.save(777, {"chat_id": 777, "message_id": 100}))
        bot = StaffBot.__new__(StaffBot)
        update = _update(message_id=99)
        asyncio.run(bot._delete_menu_echo(update))
        update.message.delete.assert_awaited_once()
        assert asyncio.run(route_card_state.load(777)).get("echoes_deleted") in (None, 0)

    def test_counter_failure_never_escapes_after_a_successful_delete(self):
        """Navigation already succeeded and the echo is already gone. If the
        bookkeeping write raised here it would surface through PTB's global
        error_handler as an apology for a tap that WORKED."""
        asyncio.run(route_card_state.save(777, {"chat_id": 777, "message_id": 100}))
        bot = StaffBot.__new__(StaffBot)

        async def _boom(*_a, **_k):
            raise RuntimeError("state store exploded")

        with patch.object(route_card_state, "note_echo_deleted", _boom):
            asyncio.run(bot._delete_menu_echo(_update()))  # must not raise

    def test_an_edited_menu_tap_still_has_its_echo_removed(self):
        """`filters.TEXT` also matches EDITED messages, where `update.message`
        is None and `effective_message` is the edited one. Reading
        `update.message` here left those echoes accumulating forever."""
        asyncio.run(route_card_state.save(777, {"chat_id": 777, "message_id": 100}))
        bot = StaffBot.__new__(StaffBot)
        update = _update()
        edited = update.message
        update.message = None
        update.effective_message = edited

        asyncio.run(bot._delete_menu_echo(update))

        edited.delete.assert_awaited_once()
        assert asyncio.run(route_card_state.load(777))["echoes_deleted"] == 1

    def test_a_callback_update_is_refused_outright(self):
        """For a callback query `effective_message` is the BOT's own message
        -- for a driver, the PINNED ROUTE CARD. Deleting it would destroy the
        one thing this whole branch exists to keep alive."""
        bot = StaffBot.__new__(StaffBot)
        update = _update()
        update.callback_query = MagicMock()

        asyncio.run(bot._delete_menu_echo(update))

        update.effective_message.delete.assert_not_called()


@pytest.mark.unit
class TestRouterOrdering:
    def test_text_router_deletes_the_echo_after_dispatching(self):
        bot = StaffBot.__new__(StaffBot)
        order = []
        update = _update()
        update.message.delete = AsyncMock(side_effect=lambda: order.append("delete"))
        context = MagicMock()
        context.user_data = {"authenticated": True}

        bot._language_handler = MagicMock()
        bot._language_handler._get_language = AsyncMock(return_value="en")
        bot._match_menu_action = lambda text, lang: "staff_active_deliveries"
        bot._clear_all_pending_flows = AsyncMock()

        async def _dispatch(action, upd, ctx):
            order.append("dispatch")

        bot._dispatch_menu_action = _dispatch
        asyncio.run(bot._handle_text_message(update, context))
        assert order == ["dispatch", "delete"]

    def test_conversation_menu_escape_also_deletes_the_echo(self):
        """A menu tap made INSIDE an operator/tryout conversation leaves the
        same echo as one made outside it. `_conv_menu_escape` is a second
        dispatch path and used to skip the cleanup entirely, so those taps
        piled up and buried the pinned card."""
        bot = StaffBot.__new__(StaffBot)
        order = []
        update = _update()
        update.message.delete = AsyncMock(side_effect=lambda: order.append("delete"))
        context = MagicMock()
        context.user_data = {"authenticated": True}

        bot._language_handler = MagicMock()
        bot._language_handler._get_language = AsyncMock(return_value="en")
        bot._match_menu_action = lambda text, lang: "staff_active_deliveries"
        bot._clear_all_pending_flows = AsyncMock()

        async def _dispatch(action, upd, ctx):
            order.append("dispatch")

        bot._dispatch_menu_action = _dispatch
        asyncio.run(bot._conv_menu_escape(update, context))
        assert order == ["dispatch", "delete"]


class _RouteApi:
    """Stands in for `active_delivery.api_client`."""

    def __init__(self, payload):
        self.client = MagicMock()
        self.client.get_active_deliveries = AsyncMock(
            return_value=MagicMock(success=True, status_code=200, data=payload)
        )

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, *a):
        return False


class _FakeChat:
    """Models Telegram's per-chat MONOTONIC message ids.

    The repost heuristic is pure id arithmetic (`tap_id - card_id` against
    `echoes_deleted + 1`), so a test that hand-sets `echoes_deleted` proves
    nothing about the counter's real lifecycle. Allocating ids the way
    Telegram does -- one per message, deleted ids never reused -- is what
    makes the end-to-end assertion meaningful."""

    def __init__(self, start=100):
        self._next = start
        self.sends = []
        self.edits = []
        self.deletes = []

    def alloc(self) -> int:
        self._next += 1
        return self._next

    def telegram_bot(self):
        bot = MagicMock()

        async def _send(**kwargs):
            sent = MagicMock()
            sent.chat_id = 777
            sent.message_id = self.alloc()
            self.sends.append(sent.message_id)
            return sent

        async def _edit(**kwargs):
            self.edits.append(kwargs.get("message_id"))

        async def _delete(**kwargs):
            self.deletes.append(kwargs.get("message_id"))

        bot.send_message = AsyncMock(side_effect=_send)
        bot.edit_message_text = AsyncMock(side_effect=_edit)
        bot.delete_message = AsyncMock(side_effect=_delete)
        bot.pin_chat_message = AsyncMock()
        return bot


def _tap_update(chat):
    """A driver reply-keyboard tap: consumes the next chat message id."""
    update = MagicMock()
    update.callback_query = None
    update.message = MagicMock()
    update.message.text = f"\U0001F69A {i18n.get('staff.menu.active_deliveries', 'en')}"
    update.message.message_id = chat.alloc()
    update.message.chat.id = 777
    update.message.delete = AsyncMock()
    update.effective_message = update.message
    update.effective_user = MagicMock()
    update.effective_user.id = 777
    return update


@pytest.mark.unit
class TestEchoCounterEndToEnd:
    """Drive router -> render -> echo delete for real, repeatedly.

    Every existing repost test sets `echoes_deleted` by hand, which is
    precisely why an off-by-one in how the counter is PRODUCED stayed
    invisible: `render_route_card` resets the counter to 0 on create, and
    the router then deletes the tap echo that TRIGGERED that create -- an id
    BELOW the new card. Counting it inflated the threshold permanently, and
    because each later tap raises the gap and the counter together the slack
    never closed, so the spec §4.2 row-5 repost (an undeleted bot message
    buries the card) never fired."""

    def _bot(self):
        bot = StaffBot.__new__(StaffBot)
        bot._language_handler = MagicMock()
        bot._language_handler._get_language = AsyncMock(return_value="en")
        bot._clear_all_pending_flows = AsyncMock()
        handler = ActiveDeliveryHandler()
        bot._delivery_handlers = {
            "active_delivery": handler,
            "status_update": MagicMock(),
            "tryouts": MagicMock(),
        }
        bot._common_handlers = {"profile": MagicMock(), "help": MagicMock()}
        return bot

    def _ctx(self, telegram_bot):
        token_manager = MagicMock()
        token_manager.get_valid_token = AsyncMock(return_value="tok")
        ctx = MagicMock()
        ctx.bot = telegram_bot
        ctx.bot_data = {"token_manager": token_manager}
        ctx.user_data = {
            "authenticated": True,
            "staff_roles": ["delivery_driver"],
            "language": "en",
        }
        return ctx

    def _drive(self, monkeypatch):
        """Tap, render, delete echo -- three times, through the real router,
        the real `render_route_card` and the real state store. Then land one
        bot message we do NOT delete (in production: the head-change alert),
        which genuinely buries the card. Returns the chat and the bot."""
        payload = {"items": [], "total": 0, "location_status": "missing",
                   "route_summary": {}}
        monkeypatch.setattr(active_delivery_mod, "api_client", _RouteApi(payload))

        chat = _FakeChat()
        bot = self._bot()
        ctx = self._ctx(chat.telegram_bot())

        # Tap 1 creates the card: the echo (101) gets a LOWER id than the
        # card sent in reply to it (102). Taps 2 and 3 edit in place, and
        # those echoes DO sit below the card.
        for _ in range(3):
            asyncio.run(bot._handle_text_message(_tap_update(chat), ctx))
        chat.alloc()  # the undeleted alert
        return chat, bot, ctx

    def test_undeleted_alert_after_repeat_taps_still_triggers_a_repost(self, monkeypatch):
        """The single assertion that matters, isolated so its failure can
        only mean one thing: spec §4.2 row 5, the repost that never fired."""
        chat, bot, ctx = self._drive(monkeypatch)
        original_card = chat.sends[0]

        asyncio.run(bot._handle_text_message(_tap_update(chat), ctx))

        assert len(chat.sends) == 2, "the buried card was not reposted"
        assert original_card in chat.deletes, "the old card was not cleaned up"
        assert asyncio.run(route_card_state.load(777))["message_id"] == chat.sends[1]

    def test_counter_only_ever_grows_for_echoes_below_the_card(self, monkeypatch):
        """The producing seam, asserted on real ids rather than hand-set
        state: three taps, but only the two whose echoes landed BELOW the
        card may be counted."""
        chat, _bot, _ctx = self._drive(monkeypatch)
        state = asyncio.run(route_card_state.load(777))
        assert state["message_id"] == chat.sends[0]
        assert chat.edits == [chat.sends[0], chat.sends[0]]
        assert state["echoes_deleted"] == 2

    def test_repost_leaves_the_counter_clean_rather_than_pre_inflated(self, monkeypatch):
        chat, bot, ctx = self._drive(monkeypatch)
        asyncio.run(bot._handle_text_message(_tap_update(chat), ctx))
        assert asyncio.run(route_card_state.load(777))["echoes_deleted"] == 0
