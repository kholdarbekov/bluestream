"""Handler tests for start/menu/language telegram bot flows."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from handlers import language as language_module
from handlers import menu as menu_module
from tests.telegram_bot.helpers import DummyCallbackQuery, DummyUpdate, make_context


@pytest.mark.unit
@pytest.mark.anyio
class TestMainMenuHandlerFlows:
    async def test_main_menu_handler_with_callback_query(self, monkeypatch):
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="back_to_main")
        context = make_context()
        cleanup_mock = AsyncMock(return_value=True)
        monkeypatch.setattr(menu_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(menu_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")
        monkeypatch.setattr(menu_module.MenuKeyboards, "main_menu", lambda _lang: "menu-kbd")
        monkeypatch.setattr(menu_module, "maybe_remove_stale_reply_keyboard", cleanup_mock)

        await menu_module.main_menu_handler(update, context)

        cleanup_mock.assert_awaited_once_with(update, context)
        update.callback_query.edit_message_text.assert_awaited_once_with(
            text="telegram.main_menu:en",
            reply_markup="menu-kbd",
        )
        update.callback_query.answer.assert_awaited_once()

    async def test_main_menu_handler_with_message(self, monkeypatch):
        update = DummyUpdate()
        context = make_context()
        cleanup_mock = AsyncMock(return_value=True)
        monkeypatch.setattr(menu_module.i18n, "get_user_language", AsyncMock(return_value="ru"))
        monkeypatch.setattr(menu_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")
        monkeypatch.setattr(menu_module.MenuKeyboards, "main_menu", lambda _lang: "menu-kbd")
        monkeypatch.setattr(menu_module, "maybe_remove_stale_reply_keyboard", cleanup_mock)

        await menu_module.main_menu_handler(update, context)

        cleanup_mock.assert_awaited_once_with(update, context)
        update.message.reply_text.assert_awaited_once_with(
            text="telegram.main_menu:ru",
            reply_markup="menu-kbd",
        )

    async def test_main_menu_handler_error_fallback(self, monkeypatch):
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="menu")
        context = make_context()
        monkeypatch.setattr(menu_module.MenuKeyboards, "main_menu", lambda _lang: (_ for _ in ()).throw(RuntimeError("boom")))
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
        monkeypatch.setattr(language_module.MenuKeyboards, "main_menu", lambda _lang: "menu-kbd")

        await handler.set_language(update, context)

        handler.user_repo.update_user_language.assert_awaited_once_with(update.effective_user.id, "ru")
        update.callback_query.edit_message_text.assert_awaited_once()
