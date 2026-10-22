"""Handler tests for start/menu/language telegram bot flows."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, create_autospec

import pytest

import menu_keyboard
from handlers import language as language_module
from handlers import menu as menu_module
from tests.telegram_bot.helpers import DummyCallbackQuery, DummyUpdate, make_context


@pytest.mark.unit
@pytest.mark.anyio
class TestMainMenuHandlerFlows:
    async def test_main_menu_handler_with_callback_query(self, monkeypatch):
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="back_to_main")
        update.callback_query.edit_message_reply_markup = AsyncMock()
        context = make_context()
        monkeypatch.setattr(menu_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(menu_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")

        await menu_module.main_menu_handler(update, context)

        update.callback_query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)
        update.callback_query.edit_message_text.assert_not_awaited()
        update.callback_query.answer.assert_awaited_once()

    async def test_main_menu_handler_error_fallback(self, monkeypatch):
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="menu")
        context = make_context()
        update.callback_query.edit_message_reply_markup = AsyncMock(side_effect=RuntimeError("boom"))
        monkeypatch.setattr(menu_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(menu_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")

        await menu_module.main_menu_handler(update, context)

        update.callback_query.answer.assert_awaited_once_with("telegram.error_occurred:en")


@pytest.mark.unit
@pytest.mark.anyio
class TestLanguageHandlerFlows:
    async def test_language_menu_callback(self, monkeypatch):
        handler = language_module.LanguageHandler()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="menu_language")
        context = make_context()

        monkeypatch.setattr(language_module, "user_middleware", AsyncMock(return_value={"id": 1}))
        monkeypatch.setattr(language_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(language_module.i18n, "get_language_flag", lambda _lang: "🇺🇸")
        monkeypatch.setattr(language_module.i18n, "get_language_name", lambda _lang, _display: "English")
        monkeypatch.setattr(language_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")
        monkeypatch.setattr(language_module.LanguageKeyboards, "language_selection", lambda _lang: "lang-kbd")

        await handler.language_menu(update, context)

        update.callback_query.edit_message_text.assert_awaited_once()
        update.callback_query.answer.assert_awaited_once()

    async def test_set_language_invalid_selection(self, monkeypatch):
        handler = language_module.LanguageHandler()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="set_language_xx")
        context = make_context()

        monkeypatch.setattr(language_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(language_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")

        await handler.set_language(update, context)

        update.callback_query.answer.assert_awaited_once_with("telegram.language.invalid_selection:en")

    async def test_set_language_already_selected(self, monkeypatch):
        handler = language_module.LanguageHandler()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="set_language_en")
        context = make_context()

        monkeypatch.setattr(language_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(language_module.i18n, "get_language_flag", lambda _lang: "🇺🇸")
        monkeypatch.setattr(language_module.i18n, "get_language_name", lambda _lang, _display: "English")
        monkeypatch.setattr(language_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")

        await handler.set_language(update, context)

        update.callback_query.answer.assert_awaited_once()
        assert "telegram.language.already_selected:en" in update.callback_query.answer.await_args.args[0]

    async def test_set_language_success_path(self, monkeypatch):
        handler = language_module.LanguageHandler()
        handler.user_repo = SimpleNamespace(update_user_language=AsyncMock())
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="set_language_ru")
        context = make_context()

        monkeypatch.setattr(language_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(language_module.i18n, "get_language_flag", lambda _lang: "🇷🇺")
        monkeypatch.setattr(language_module.i18n, "get_language_name", lambda _lang, _display: "Русский")
        monkeypatch.setattr(language_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")
        send = create_autospec(menu_keyboard.send_menu_keyboard)
        monkeypatch.setattr(language_module, "send_menu_keyboard", send)

        await handler.set_language(update, context)

        handler.user_repo.update_user_language.assert_awaited_once_with(update.effective_user.id, "ru")
        assert send.await_args.args[4] == "ru"
        assert "telegram.language.confirmation_title:ru" in send.await_args.args[3]
        update.callback_query.edit_message_text.assert_not_awaited()
