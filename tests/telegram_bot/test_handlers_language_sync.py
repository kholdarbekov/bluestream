"""set_language must sync the language to BOTH the bot DB and the backend (Deliverable C8)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from handlers import language as language_module
from tests.telegram_bot.helpers import DummyCallbackQuery, DummyUpdate, make_context


def _resp(success=True, data=None, error=None, status_code=200):
    return SimpleNamespace(success=success, data=data or {}, error=error, status_code=status_code)


@pytest.mark.unit
@pytest.mark.anyio
class TestLanguageBackendSync:
    async def test_set_language_updates_bot_db_and_backend(self, monkeypatch):
        handler = language_module.LanguageHandler()
        handler.user_repo = SimpleNamespace(update_user_language=AsyncMock())
        update = DummyUpdate(user_id=801)
        update.callback_query = DummyCallbackQuery(data="set_language_ru")
        context = make_context()
        captured = {}

        class _APIContext:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                return False

            async def update_user_profile(self, token, payload):
                captured["token"] = token
                captured["payload"] = payload
                return _resp(success=True)

        monkeypatch.setattr(language_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(language_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")
        monkeypatch.setattr(language_module.i18n, "get_language_flag", lambda code: "FLAG")
        monkeypatch.setattr(language_module.i18n, "get_language_name", lambda code, in_lang: code)
        monkeypatch.setattr(
            language_module.config.localization, "supported_languages", ["uz", "ru", "en"], raising=False
        )
        monkeypatch.setattr(language_module, "main_menu_for", AsyncMock(return_value="menu-kbd"))
        monkeypatch.setattr(language_module, "get_auth_token", AsyncMock(return_value="jwt-token"))
        monkeypatch.setattr(language_module, "api_client", _APIContext())

        await handler.set_language(update, context)

        handler.user_repo.update_user_language.assert_awaited_once_with(801, "ru")
        assert captured["payload"] == {"preferred_language": "ru"}
        assert captured["token"] == "jwt-token"

    async def test_set_language_survives_backend_failure(self, monkeypatch):
        handler = language_module.LanguageHandler()
        handler.user_repo = SimpleNamespace(update_user_language=AsyncMock())
        update = DummyUpdate(user_id=802)
        update.callback_query = DummyCallbackQuery(data="set_language_ru")
        context = make_context()

        class _APIContext:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                return False

            async def update_user_profile(self, token, payload):
                raise RuntimeError("backend down")

        monkeypatch.setattr(language_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(language_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")
        monkeypatch.setattr(language_module.i18n, "get_language_flag", lambda code: "FLAG")
        monkeypatch.setattr(language_module.i18n, "get_language_name", lambda code, in_lang: code)
        monkeypatch.setattr(
            language_module.config.localization, "supported_languages", ["uz", "ru", "en"], raising=False
        )
        monkeypatch.setattr(language_module, "main_menu_for", AsyncMock(return_value="menu-kbd"))
        monkeypatch.setattr(language_module, "get_auth_token", AsyncMock(return_value="jwt-token"))
        monkeypatch.setattr(language_module, "api_client", _APIContext())

        # Must not raise: the bot-DB change still succeeds.
        await handler.set_language(update, context)
        handler.user_repo.update_user_language.assert_awaited_once_with(802, "ru")
        update.callback_query.edit_message_text.assert_awaited_once()
