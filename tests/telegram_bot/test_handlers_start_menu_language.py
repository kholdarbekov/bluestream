"""Handler tests for start/menu/language telegram bot flows."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from handlers import language as language_module
from handlers import menu as menu_module
from handlers import start as start_module
from tests.telegram_bot.helpers import DummyCallbackQuery, DummyUpdate, FakeAPIClientContext, make_context


def _resp(success=True, data=None, error=None, status_code=200):
    return SimpleNamespace(success=success, data=data or {}, error=error, status_code=status_code)


@pytest.mark.unit
@pytest.mark.anyio
class TestStartHandlerFlows:
    async def test_handle_auth_linking_success(self, monkeypatch):
        update = DummyUpdate()
        monkeypatch.setattr(start_module, "api_client", FakeAPIClientContext(_make_request=_resp(success=True)))
        monkeypatch.setattr(start_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(start_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")
        monkeypatch.setattr(start_module.MenuKeyboards, "main_menu", lambda _lang: "menu-kbd")

        result = await start_module.handle_auth_linking(update, "ABC123")

        assert result is True
        assert update.message.reply_text.await_count == 2
        update.message.reply_text.assert_any_await("telegram.auth.linking_success:en", parse_mode="Markdown")
        update.message.reply_text.assert_any_await("telegram.main_menu_prompt:en", reply_markup="menu-kbd")

    async def test_handle_auth_linking_failure_expired(self, monkeypatch):
        update = DummyUpdate()
        monkeypatch.setattr(
            start_module,
            "api_client",
            FakeAPIClientContext(_make_request=_resp(success=False, error="Token expired")),
        )
        monkeypatch.setattr(start_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(start_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")

        result = await start_module.handle_auth_linking(update, "EXPIRED")

        assert result is False
        update.message.reply_text.assert_awaited_once_with(
            "telegram.auth.linking_expired:en",
            parse_mode="Markdown",
        )

    async def test_start_handler_for_new_user_registration_success(self, monkeypatch):
        update = DummyUpdate()
        context = make_context(args=[])
        user_repo = SimpleNamespace(get_user_by_telegram_id=AsyncMock(return_value=None))
        monkeypatch.setattr(start_module, "BotUserRepository", lambda _db: user_repo)
        monkeypatch.setattr(
            start_module,
            "api_client",
            FakeAPIClientContext(register_telegram_user=_resp(success=True)),
        )
        monkeypatch.setattr(start_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(start_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")
        monkeypatch.setattr(start_module.LanguageKeyboards, "select_language", lambda: "lang-kbd")

        await start_module.start_handler(update, context)

        update.message.reply_text.assert_awaited_once()
        args = update.message.reply_text.await_args.args
        kwargs = update.message.reply_text.await_args.kwargs
        assert kwargs["reply_markup"] == "lang-kbd"
        assert "telegram.registration_welcome:en" in args[0]
        assert "telegram.registration_welcome:uz" in args[0]
        assert "telegram.registration_welcome:ru" in args[0]

    async def test_start_handler_for_existing_user(self, monkeypatch):
        update = DummyUpdate()
        context = make_context(args=[])
        user_repo = SimpleNamespace(get_user_by_telegram_id=AsyncMock(return_value={"id": 1}))
        monkeypatch.setattr(start_module, "BotUserRepository", lambda _db: user_repo)
        monkeypatch.setattr(start_module.i18n, "get_user_language", AsyncMock(return_value="uz"))
        monkeypatch.setattr(start_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")
        monkeypatch.setattr(start_module.MenuKeyboards, "main_menu", lambda _lang: "menu-kbd")

        await start_module.start_handler(update, context)

        update.message.reply_text.assert_awaited_once_with(
            "telegram.welcome:uz",
            reply_markup="menu-kbd",
        )

    async def test_start_handler_registration_failure_sends_error(self, monkeypatch):
        update = DummyUpdate()
        context = make_context(args=[])
        user_repo = SimpleNamespace(get_user_by_telegram_id=AsyncMock(return_value=None))
        monkeypatch.setattr(start_module, "BotUserRepository", lambda _db: user_repo)
        monkeypatch.setattr(
            start_module,
            "api_client",
            FakeAPIClientContext(register_telegram_user=_resp(success=False, error="bad request")),
        )
        monkeypatch.setattr(start_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")

        await start_module.start_handler(update, context)

        update.message.reply_text.assert_awaited_once_with("telegram.auth.registration_failed:en")


@pytest.mark.unit
@pytest.mark.anyio
class TestMainMenuHandlerFlows:
    async def test_main_menu_handler_with_callback_query(self, monkeypatch):
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="back_to_main")
        context = make_context()
        monkeypatch.setattr(menu_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(menu_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")
        monkeypatch.setattr(menu_module.MenuKeyboards, "main_menu", lambda _lang: "menu-kbd")

        await menu_module.main_menu_handler(update, context)

        update.callback_query.edit_message_text.assert_awaited_once_with(
            text="telegram.welcome:en",
            reply_markup="menu-kbd",
        )
        update.callback_query.answer.assert_awaited_once()

    async def test_main_menu_handler_with_message(self, monkeypatch):
        update = DummyUpdate()
        context = make_context()
        monkeypatch.setattr(menu_module.i18n, "get_user_language", AsyncMock(return_value="ru"))
        monkeypatch.setattr(menu_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")
        monkeypatch.setattr(menu_module.MenuKeyboards, "main_menu", lambda _lang: "menu-kbd")

        await menu_module.main_menu_handler(update, context)

        update.message.reply_text.assert_awaited_once_with(
            text="telegram.welcome:ru",
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
