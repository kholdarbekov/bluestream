"""Reply-keyboard MENU taps must escape operator/tryout ConversationHandler
text states too.

Those states use their own per-state MessageHandler(filters.TEXT) which wins
over the catch-all menu router, so a menu tap while typing a phone/name/note was
captured as that input. The fix prepends a shared menu-escape MessageHandler to
each text state: a menu label ends the conversation and navigates; anything else
falls through to the real receive_* handler.
"""

import asyncio
import re
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import Chat, Message, Update, User
from telegram.ext import ConversationHandler, MessageHandler, filters

from staff_bot.bot import StaffBot
from staff_bot.i18n import i18n

BOT_FILE = Path(__file__).resolve().parents[2] / "staff_bot" / "bot.py"


def _make_bot():
    bot = StaffBot.__new__(StaffBot)
    lang = MagicMock()
    lang._get_language = AsyncMock(return_value="en")
    lang.language_menu = AsyncMock()
    bot._language_handler = lang

    status_update = MagicMock()
    status_update.show_cash_hub = AsyncMock()
    profile = MagicMock()
    profile.show_profile = AsyncMock()
    help_h = MagicMock()
    help_h.show_help = AsyncMock()
    tryouts = MagicMock()
    tryouts.show_hub = AsyncMock()
    active = MagicMock()
    active.show_active_deliveries = AsyncMock()

    bot._delivery_handlers = {"status_update": status_update, "tryouts": tryouts,
                              "active_delivery": active}
    bot._common_handlers = {"profile": profile, "help": help_h}
    bot._route_new_orders = AsyncMock()
    return bot


def _msg_update(text):
    update = MagicMock()
    update.message = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    update.callback_query = None
    update.effective_user = MagicMock(id=99)
    return update


def _ctx(user_data):
    ctx = MagicMock()
    ctx.user_data = user_data
    ctx.bot = MagicMock()
    return ctx


@pytest.mark.unit
class TestMainMenuTextPattern:
    def test_matches_menu_labels_with_and_without_emoji(self):
        rx = re.compile(StaffBot.__new__(StaffBot)._main_menu_text_pattern())
        assert rx.match(f"💰 {i18n.get('staff.menu.cash', 'en')}")
        assert rx.match(i18n.get('staff.menu.cash', 'en'))
        assert rx.match(f"📦 {i18n.get('staff.menu.new_orders', 'en')}")
        assert rx.match(f"👤 {i18n.get('staff.menu.profile', 'en')}")

    def test_does_not_match_user_typed_input(self):
        rx = re.compile(StaffBot.__new__(StaffBot)._main_menu_text_pattern())
        assert not rx.match("+998901234567")
        assert not rx.match("Aziz Karimov")
        assert not rx.match("54000")
        assert not rx.match("Near the big mosque, 2nd floor")


@pytest.mark.unit
class TestConvMenuEscape:
    def test_escape_clears_flows_and_working_dicts_and_navigates_and_ends(self):
        bot = _make_bot()
        ud = {
            "authenticated": True, "language": "en",
            "pending_cod_collection_flow": {"customer_id": 1, "amount": 5},
            "new_order": {"items": []},
            "new_client": {"phone": "x"},
        }
        update = _msg_update(f"💰 {i18n.get('staff.menu.cash', 'en')}")
        ctx = _ctx(ud)

        result = asyncio.run(bot._conv_menu_escape(update, ctx))

        assert result == ConversationHandler.END
        assert "pending_cod_collection_flow" not in ud
        assert "new_order" not in ud
        assert "new_client" not in ud
        bot._delivery_handlers["status_update"].show_cash_hub.assert_awaited_once()

    def test_escape_routes_new_orders_label(self):
        bot = _make_bot()
        update = _msg_update(f"📦 {i18n.get('staff.menu.new_orders', 'en')}")
        ctx = _ctx({"authenticated": True, "language": "en", "new_address": {}})

        result = asyncio.run(bot._conv_menu_escape(update, ctx))

        assert result == ConversationHandler.END
        bot._route_new_orders.assert_awaited_once()


def _real_update(text):
    """A real telegram.Update so PTB filters can be evaluated offline."""
    chat = Chat(id=1, type="private")
    user = User(id=99, is_bot=False, first_name="Driver")
    msg = Message(
        message_id=1, date=datetime.now(timezone.utc), chat=chat,
        from_user=user, text=text,
    )
    return Update(update_id=1, message=msg)


@pytest.mark.unit
class TestStateListRoutingDecision:
    """Validate the actual PTB filter decision: with menu_escape prepended to a
    state's handler list, a menu-label update selects menu_escape FIRST; non-menu
    text falls through to the real receive_* handler. This is the mechanism the
    whole Tier-3 fix relies on, exercised through real telegram objects."""

    def _state_handlers(self):
        bot = StaffBot.__new__(StaffBot)
        pattern = bot._main_menu_text_pattern()
        menu_escape = MessageHandler(
            filters.Regex(pattern) & ~filters.COMMAND, AsyncMock()
        )
        receive = MessageHandler(filters.TEXT & ~filters.COMMAND, AsyncMock())
        return menu_escape, receive

    def test_menu_label_selects_escape_first(self):
        menu_escape, receive = self._state_handlers()
        state = [menu_escape, receive]  # the order used in bot.py
        update = _real_update(f"💰 {i18n.get('staff.menu.cash', 'en')}")

        first = next((h for h in state if h.check_update(update)), None)
        assert first is menu_escape

    def test_typed_phone_falls_through_to_receive(self):
        menu_escape, receive = self._state_handlers()
        state = [menu_escape, receive]
        update = _real_update("+998901234567")

        first = next((h for h in state if h.check_update(update)), None)
        assert first is receive

    def test_typed_name_falls_through_to_receive(self):
        menu_escape, receive = self._state_handlers()
        state = [menu_escape, receive]
        update = _real_update("Aziz Karimov")

        first = next((h for h in state if h.check_update(update)), None)
        assert first is receive


@pytest.mark.unit
class TestConversationMenuEscapeWiring:
    """Static guards that the escape is wired into operator/tryout conversations."""

    def test_bot_defines_menu_escape_handler(self):
        text = BOT_FILE.read_text(encoding="utf-8")
        assert "_main_menu_text_pattern" in text
        assert "self._conv_menu_escape" in text
        assert "menu_escape" in text

    def test_operator_and_tryout_text_states_include_menu_escape(self):
        """Each text-input conversation must prepend the menu_escape handler so a
        reply-keyboard tap ends the conversation instead of being captured."""
        text = BOT_FILE.read_text(encoding="utf-8")
        # Count how many states wire the escape — must cover the operator + tryout
        # conversations (create_user x3, search x1, create_order client+notes,
        # add_address x4, create_tryout x2+location). Use a conservative floor.
        occurrences = text.count("menu_escape,")
        assert occurrences >= 10, f"menu_escape wired into too few states: {occurrences}"
