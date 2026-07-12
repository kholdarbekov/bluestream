"""Support/help screens must not be navigational dead-ends.

Tapping Support (menu_support), FAQ, or Contact-support — and the /help
command — previously produced a text-only message with no way back to the
main menu, stranding users (especially elderly/less tech-savvy ones). Each now
renders a single '⬅️ Back' button whose callback_data is 'back_to_main', which
is already wired to main_menu_handler in bot.py. Message text is unchanged.
"""
from unittest.mock import AsyncMock

import pytest

from i18n import i18n as i18n_singleton
from handlers import support_handlers
from tests.telegram_bot.helpers import DummyCallbackQuery, DummyUpdate, make_context


def _callbacks(markup):
    return [btn.callback_data for row in markup.inline_keyboard for btn in row]


@pytest.fixture(autouse=True)
def _patch_i18n(monkeypatch):
    """Stub i18n so no DB is needed; keys echo back as ``key:lang``."""
    monkeypatch.setattr(i18n_singleton, "get_user_language", AsyncMock(return_value="en"))
    monkeypatch.setattr(i18n_singleton, "get", lambda key, lang="en", **_: f"{key}:{lang}")


@pytest.mark.unit
@pytest.mark.anyio
class TestSupportScreensHaveBackButton:
    async def test_support_menu_callback_has_back_to_main(self):
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="menu_support")

        await support_handlers.support_menu(update, make_context())

        update.callback_query.edit_message_text.assert_awaited_once()
        markup = update.callback_query.edit_message_text.await_args.kwargs["reply_markup"]
        assert _callbacks(markup) == ["back_to_main"]
        update.callback_query.answer.assert_awaited_once()

    async def test_support_menu_keeps_original_text(self):
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="menu_support")

        await support_handlers.support_menu(update, make_context())

        sent_text = update.callback_query.edit_message_text.await_args.args[0]
        assert sent_text == "telegram.support.menu_coming_soon:en"

    async def test_faq_callback_has_back_to_main(self):
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="faq")

        await support_handlers.faq_handler(update, make_context())

        markup = update.callback_query.edit_message_text.await_args.kwargs["reply_markup"]
        assert _callbacks(markup) == ["back_to_main"]

    async def test_contact_support_callback_has_back_to_main(self):
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="contact_support")

        await support_handlers.contact_support(update, make_context())

        markup = update.callback_query.edit_message_text.await_args.kwargs["reply_markup"]
        assert _callbacks(markup) == ["back_to_main"]

    async def test_help_command_message_has_back_to_main(self):
        # No callback_query → /help arrives as a plain command message.
        update = DummyUpdate()

        await support_handlers.help_handler(update, make_context())

        markup = update.message.reply_text.await_args.kwargs["reply_markup"]
        assert _callbacks(markup) == ["back_to_main"]
