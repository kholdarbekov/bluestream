"""Static regressions for staff bot language and i18n behavior."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STAFF_ROOT = ROOT / "staff_bot"
I18N_FILE = STAFF_ROOT / "i18n.py"
BASE_HANDLER_FILE = STAFF_ROOT / "handlers" / "base.py"
LANGUAGE_HANDLER_FILE = STAFF_ROOT / "handlers" / "language.py"
PERMISSIONS_FILE = STAFF_ROOT / "permissions.py"
BOT_FILE = STAFF_ROOT / "bot.py"


def test_staff_i18n_loads_staff_keys_even_if_category_drifted():
    """Loader should include staff.* keys even when category is not strictly staff_bot."""
    text = I18N_FILE.read_text(encoding="utf-8")

    assert "category = 'staff_bot' OR key LIKE 'staff.%'" in text


def test_staff_i18n_language_normalization_present():
    """Locale normalization should map variants like ru-RU/uz_UZ to canonical language codes."""
    text = I18N_FILE.read_text(encoding="utf-8")

    assert "def normalize_language" in text
    assert "replace(\"_\", \"-\")" in text
    assert "split(\"-\", 1)[0]" in text


def test_base_handler_normalizes_and_persists_language_context():
    """Shared language getter should normalize context language and persist canonical code."""
    text = BASE_HANDLER_FILE.read_text(encoding="utf-8")

    assert "lang = i18n.normalize_language(raw_lang)" in text
    assert "lang = await i18n.get_user_language(update.effective_user.id)" in text
    assert "context.user_data['language'] = lang" in text


def test_language_switch_refreshes_reply_keyboard_immediately():
    """After selecting a language, bot should also send updated reply keyboard labels."""
    text = LANGUAGE_HANDLER_FILE.read_text(encoding="utf-8")

    assert "candidate_code not in i18n.supported_languages" in text
    assert "query.message.reply_text" in text
    assert "MenuKeyboards.main_menu(lang_code, staff_roles)" in text


def test_staff_routing_and_permissions_use_normalized_language_resolution():
    """Core routing paths should resolve language via shared normalized getters."""
    bot_text = BOT_FILE.read_text(encoding="utf-8")
    permissions_text = PERMISSIONS_FILE.read_text(encoding="utf-8")

    assert "language = await self._language_handler._get_language(update, context)" in bot_text
    assert "return i18n.normalize_language(context.user_data.get('language'))" in permissions_text
