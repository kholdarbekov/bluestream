"""Reply-keyboard MENU taps must escape operator/tryout ConversationHandler
text states too.

Those states use their own per-state MessageHandler(filters.TEXT) which wins
over the catch-all menu router, so a menu tap while typing a phone/name/note was
captured as that input. The fix prepends a shared menu-escape MessageHandler to
each text state: a menu label ends the conversation and navigates; anything else
falls through to the real receive_* handler.
"""

import asyncio
import importlib.util
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

# Same technique as tests/staff_bot/test_route_card_views.py: resolve real
# copy live from the seed script (via `_curated_value`, the SAME function
# `seed_translations()` calls) rather than pasting values by hand, so a
# future edit to seed_staff_translations.py can't leave this test asserting
# stale copy while production ships something else.
_SEED_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "seed_staff_translations.py"


def _load_seed_module():
    spec = importlib.util.spec_from_file_location("seed_staff_translations", _SEED_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SEED_MODULE = _load_seed_module()

_MAIN_MENU_KEYS = [
    'staff.menu.new_orders', 'staff.menu.active_deliveries', 'staff.menu.tryouts',
    'staff.menu.cash', 'staff.menu.profile', 'staff.menu.settings', 'staff.menu.help',
]


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
    update.message.delete = AsyncMock()
    # A normal (non-edited) message: effective_message resolves to message,
    # exactly like a real python-telegram-bot Update would. `_conv_menu_escape`
    # reads via effective_message so an edited-message tap doesn't
    # AttributeError on a None `.message`.
    update.effective_message = update.message
    update.callback_query = None
    update.effective_user = MagicMock(id=99)
    return update


def _ctx(user_data):
    ctx = MagicMock()
    ctx.user_data = user_data
    ctx.bot = MagicMock()
    return ctx


@pytest.mark.unit
class TestMainMenuTapRecognition:
    """`_match_menu_action` is the ONE predicate for "is this text a tap?".

    Was `TestMainMenuTextPattern`, against a regex (`_main_menu_text_pattern`)
    that answered the same question a second time and more loosely — it
    accepted any leading token, so text the matcher could not resolve was still
    claimed by the escape and the conversation died with no output at all. The
    regex is gone; the cases it pinned are pinned here, on the decider itself.
    """

    def test_matches_menu_labels_with_and_without_emoji(self):
        bot = StaffBot.__new__(StaffBot)
        assert bot._match_menu_action(f"💰 {i18n.get('staff.menu.cash', 'en')}", 'en')
        assert bot._match_menu_action(i18n.get('staff.menu.cash', 'en'), 'en')
        assert bot._match_menu_action(f"📦 {i18n.get('staff.menu.new_orders', 'en')}", 'en')
        assert bot._match_menu_action(f"👤 {i18n.get('staff.menu.profile', 'en')}", 'en')

    def test_does_not_match_user_typed_input(self):
        bot = StaffBot.__new__(StaffBot)
        assert bot._match_menu_action("+998901234567", 'en') is None
        assert bot._match_menu_action("Aziz Karimov", 'en') is None
        assert bot._match_menu_action("54000", 'en') is None
        assert bot._match_menu_action("Near the big mosque, 2nd floor", 'en') is None

    def test_a_word_typed_in_front_of_a_label_is_not_a_tap(self):
        """The decoration the router strips is an EMOJI, not "a few characters".

        `"Aziz Profile"` used to resolve to the Profile button (the matcher
        retried the text with its first 2-4 characters removed), and
        `"Sardor Profile"` used to be claimed by the escape filter and resolved
        by nobody. Both shapes are ordinary things a person types into a staff
        flow.
        """
        bot = StaffBot.__new__(StaffBot)
        label = i18n.get('staff.menu.profile', 'en')
        assert bot._match_menu_action(f"Aziz {label}", 'en') is None
        assert bot._match_menu_action(f"Sardor {label}", 'en') is None

    def test_a_trailing_space_in_the_translation_row_does_not_kill_the_button(self, monkeypatch):
        """A row seeded as "Cash " renders a button; it must still route."""
        from staff_bot.i18n import i18n as live_i18n

        merged = {**live_i18n.translations.get('en', {}), 'staff.menu.cash': 'Cash '}
        monkeypatch.setitem(live_i18n.translations, 'en', merged)

        bot = StaffBot.__new__(StaffBot)
        assert bot._match_menu_action("💰 Cash ", 'en') == 'staff_cash_hub'


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

    def test_edited_menu_tap_navigates_instead_of_crashing(self):
        """PTB's `filters.TEXT` matches EDITED messages too, and on those
        `update.message` is None while `effective_message` holds the edit.
        Reading `update.message.text` raised AttributeError before the
        conversation was ever cleared -- and now that this method also deletes
        the echo, it blew up before reaching that cleanup."""
        bot = _make_bot()
        update = _msg_update(f"💰 {i18n.get('staff.menu.cash', 'en')}")
        edited = update.message
        update.message = None
        update.effective_message = edited
        ud = {"authenticated": True, "language": "en", "new_order": {"items": []}}

        result = asyncio.run(bot._conv_menu_escape(update, _ctx(ud)))

        assert result == ConversationHandler.END
        bot._delivery_handlers["status_update"].show_cash_hub.assert_awaited_once()
        assert "new_order" not in ud
        edited.delete.assert_awaited_once()  # the echo is cleaned up too

    def test_update_without_any_message_ends_the_conversation_without_crashing(self):
        bot = _make_bot()
        update = _msg_update("x")
        update.message = None
        update.effective_message = None

        result = asyncio.run(bot._conv_menu_escape(update, _ctx({"authenticated": True})))

        assert result == ConversationHandler.END


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
        menu_escape = MessageHandler(
            bot._main_menu_tap_filter() & ~filters.COMMAND, AsyncMock()
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
class TestCrossLanguageMenuEscape:
    @pytest.fixture(autouse=True)
    def _seed_en_and_uz_menu_translations(self, monkeypatch):
        """Feed real, DISTINCT en/uz copy for the main-menu keys into the i18n
        singleton, scoped to this class only via `monkeypatch.setitem` (reverted
        after every test, so other test classes in this file -- and other test
        files -- keep seeing the empty-dict fallback they already rely on).

        Load-bearing: with an unpopulated `i18n.translations`, `i18n.get`
        derives an identical humanized fallback for every language, so
        `test_stale_keyboard_in_another_language_still_navigates` would pass
        even with the Step 4 fix reverted. Seeding real, divergent uz/en copy
        -- and asserting it diverges -- closes that gap.
        """
        for lang in ("en", "uz"):
            resolved = {}
            for key in _MAIN_MENU_KEYS:
                value = _SEED_MODULE._curated_value(key, lang)
                assert value, f"{key} has no curated {lang} value in seed_staff_translations.py"
                resolved[key] = value
            merged = {**i18n.translations.get(lang, {}), **resolved}
            monkeypatch.setitem(i18n.translations, lang, merged)

    def test_stale_keyboard_in_another_language_still_navigates(self):
        """`_main_menu_text_pattern` builds its regex from ALL supported
        languages while `_match_menu_action` resolved only the CURRENT one,
        so a driver holding a keyboard from before a language switch had
        their conversation killed with zero output.

        The seeded fixture is load-bearing: with an unpopulated i18n
        singleton every language returns the same humanized fallback, so
        this test passes even with the fix reverted.
        """
        from staff_bot.bot import StaffBot
        from staff_bot.i18n import i18n

        bot = StaffBot.__new__(StaffBot)
        uz_label = i18n.get('staff.menu.cash', 'uz')
        en_label = i18n.get('staff.menu.cash', 'en')
        assert uz_label != en_label, "fixture failed to seed distinct languages"
        assert bot._match_menu_action(uz_label, 'en') == 'staff_cash_hub'


@pytest.mark.unit
class TestConversationMenuEscapeWiring:
    """Static guards that the escape is wired into operator/tryout conversations."""

    def test_bot_defines_menu_escape_handler(self):
        text = BOT_FILE.read_text(encoding="utf-8")
        # The escape is guarded by the MATCHER, not by a second regex of its
        # own: `_main_menu_text_pattern` was that second regex and is gone.
        assert "_main_menu_text_pattern" not in text
        assert "self._main_menu_tap_filter()" in text
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
