"""Startup policy regressions for staff bot translation loading."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BOT_FILE = ROOT / "staff_bot" / "bot.py"


def test_staff_bot_startup_does_not_block_on_translation_completeness():
    """Staff bot startup should not fail-fast on translation seed completeness."""
    text = BOT_FILE.read_text(encoding="utf-8")

    assert "await i18n.load_translations()" in text
    assert "self._validate_translation_coverage()" not in text
    assert "STAFF_ALLOW_PARTIAL_TRANSLATIONS" not in text


def test_staff_bot_startup_does_not_fail_on_empty_staff_catalog():
    """Startup should not require non-empty staff translation catalog."""
    text = BOT_FILE.read_text(encoding="utf-8")
    assert "STAFF_ALLOW_EMPTY_TRANSLATIONS" not in text
    assert "No staff bot translations found in database" not in text
