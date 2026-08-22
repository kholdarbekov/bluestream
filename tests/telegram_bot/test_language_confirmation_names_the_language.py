"""The language-switch confirmation must NAME the language, through the real i18n.

``telegram.language.now_using`` is seeded as ``"You're now using {language}"``.
``set_language`` used to render it in two steps — ``i18n.get(key, lang)`` and
then ``.format(language=...)`` on the result — which was correct only while
``get()`` handed templates back. It no longer does: a template the caller did
not fill is broken copy, so ``get()`` returns the humanised key ``"Now using"``,
``.format()`` finds nothing to substitute and succeeds, and the customer who
had just switched to Russian read ``"✅ Now using"`` — the confirmation for the
language switch itself, with the language missing.

THIS TEST DELIBERATELY USES THE REAL ``i18n.get``. The dispatcher harness
(``tests/telegram_bot/ptb_harness.py::_install_translations``) replaces ``get``
with a stub that still returns the raw template when the caller passes no
values — the pre-wave-2 rule. Every journey test that goes through the harness
therefore renders this screen CORRECTLY whether the handler is fixed or not.
Reaching the regression means installing the copy and letting
``shared.i18n_rendering.render_translation`` run.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from handlers import language as language_module
from tests.telegram_bot.helpers import DummyCallbackQuery, DummyUpdate, make_context


# Real seeded copy (scripts/seed_backend_translations.py). The placeholder
# spelling is the whole point of the test, so it is reproduced exactly.
SEEDED = {
    "ru": {
        "telegram.language.now_using": "Теперь вы используете язык {language_name}",
        "telegram.language.confirmation_title": "Язык обновлён",
        "telegram.language.confirmation_message": "Теперь все меню и сообщения будут на новом языке.",
        "telegram.language.changed_success": "Язык изменён",
    },
    "uz": {
        "telegram.language.now_using": "Endi {language_name} tilidan foydalanyapsiz",
        "telegram.language.confirmation_title": "Til yangilandi",
        "telegram.language.confirmation_message": "Barcha menyular endi yangi tilingizda.",
        "telegram.language.changed_success": "Til o'zgartirildi",
    },
}

# What `humanise_key('telegram.language.now_using')` produces — i.e. what the
# customer saw instead of the sentence.
HUMANISED = "Now using"


class _NoopAPIContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False

    async def update_user_profile(self, token, payload):
        return SimpleNamespace(success=True, data={}, error=None, status_code=200)


@pytest.fixture
def real_copy(monkeypatch):
    """Serve the seeded copy through the REAL `i18n.get` / render_translation."""
    monkeypatch.setattr(language_module.i18n, "translations", SEEDED)
    monkeypatch.setattr(language_module.i18n, "get_user_language", AsyncMock(return_value="uz"))
    monkeypatch.setattr(
        language_module.config.localization, "supported_languages", ["uz", "ru", "en"], raising=False
    )
    monkeypatch.setattr(language_module, "main_menu_for", AsyncMock(return_value="menu-kbd"))
    monkeypatch.setattr(language_module, "get_auth_token", AsyncMock(return_value="jwt-token"))
    monkeypatch.setattr(language_module, "api_client", _NoopAPIContext())


@pytest.mark.unit
@pytest.mark.anyio
class TestLanguageSwitchConfirmation:
    async def _switch_to(self, code, user_id=9101):
        handler = language_module.LanguageHandler()
        handler.user_repo = SimpleNamespace(update_user_language=AsyncMock())
        update = DummyUpdate(user_id=user_id)
        update.callback_query = DummyCallbackQuery(data=f"set_language_{code}")
        await handler.set_language(update, make_context())
        return update.callback_query.edit_message_text.call_args.kwargs["text"]

    async def test_the_confirmation_names_the_language_just_chosen(self, real_copy):
        text = await self._switch_to("ru")

        assert "Русский" in text, (
            "the customer who just switched to Russian is not told WHICH language "
            "they switched to — the sentence lost its only variable"
        )
        assert "Теперь вы используете язык Русский" in text
        assert HUMANISED not in text, (
            "the humanised key leaked to the customer: the copy was fetched "
            "without its values and then formatted afterwards"
        )
        assert "{" not in text and "}" not in text

    async def test_it_works_in_every_supported_language(self, real_copy, monkeypatch):
        monkeypatch.setattr(language_module.i18n, "get_user_language", AsyncMock(return_value="ru"))

        text = await self._switch_to("uz", user_id=9102)

        assert "O'zbekcha" in text
        assert "Endi O'zbekcha tilidan foydalanyapsiz" in text
        assert HUMANISED not in text

    async def test_a_row_still_carrying_the_old_placeholder_reads_as_a_label(
        self, real_copy, monkeypatch
    ):
        """Broken copy degrades to the humanised key — it never shows braces.

        Two ways this row goes wrong in production, both covered here:

        * the DB has not been reseeded since the placeholder was renamed from
          `{language}` to `{language_name}`. The rename was forced when
          `Translation.get` still bound `key`/`language` by keyword and so owned
          those names; they are positional-only now, but a stale row still
          carries the old placeholder;
        * someone edited the value from the admin UI, which needs no deploy.

        Either way the customer must read a plain label, never `{language}`.
        """
        legacy = {code: dict(rows) for code, rows in SEEDED.items()}
        legacy["ru"]["telegram.language.now_using"] = "Теперь вы используете язык {language}"
        monkeypatch.setattr(language_module.i18n, "translations", legacy)

        text = await self._switch_to("ru", user_id=9103)

        assert "{" not in text and "}" not in text
        assert HUMANISED in text
