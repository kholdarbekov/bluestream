"""The Support Inbox attachment copy is seeded, in three languages, and says what the page says.

Every admin-UI call passes an inline English fallback, so an unseeded key degrades silently to
English for a ru/uz admin and nothing fails. The seeded `en` value must be the call site's
`defaultValue` byte for byte, or the page changes wording the moment the seed runs.

The owner is `TRANSLATIONS` in scripts/seed_support_inbox_translations.py (category `ui`).
Data only: nothing here opens an app context or touches a database.
"""
import re
from pathlib import Path

import pytest

from scripts.seed_support_inbox_translations import TRANSLATIONS
from tests.unit.test_place_group_translation_seeds import (
    _JS_STRING,
    _i18next_placeholders,
    _unquote_js,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SEED = REPO_ROOT / "scripts" / "seed_support_inbox_translations.py"
ATTACHMENT_JSX = REPO_ROOT / "admin_ui" / "src" / "components" / "support" / "SupportAttachment.jsx"

# The strings the video-on-demand player and the distinct failure states render.
PLAYER_KEYS = {
    "ui.support.play_video",
    "ui.support.play_video_size",
    "ui.support.attachment_load_failed",
    "ui.support.retry",
    "ui.support.media_cant_play",
}

_DEFAULT = re.compile(
    r"""t\(\s*(['"])(ui\.support\.[a-z_]+)\1\s*,\s*\{\s*defaultValue:\s*(%s)""" % _JS_STRING,
    re.S,
)


def _attachment_defaults():
    """key -> every `defaultValue` SupportAttachment.jsx passes for it. A set, so drift shows up."""
    found = {}
    for _, key, literal in _DEFAULT.findall(ATTACHMENT_JSX.read_text(encoding="utf-8")):
        found.setdefault(key, set()).add(_unquote_js(literal))
    return found


@pytest.mark.unit
def test_importing_the_seed_does_not_write():
    assert 'if __name__ == "__main__":' in SEED.read_text(encoding="utf-8")


@pytest.mark.unit
def test_the_component_renders_every_player_key():
    assert PLAYER_KEYS <= set(_attachment_defaults())


@pytest.mark.unit
def test_every_key_the_component_renders_is_seeded_with_its_fallback_as_english():
    defaults = _attachment_defaults()
    assert defaults
    for key, fallbacks in defaults.items():
        assert fallbacks == {TRANSLATIONS["en"].get(key)}, key


@pytest.mark.unit
@pytest.mark.parametrize("language", ["uz", "ru"])
def test_every_language_carries_the_same_keys_and_interpolation_tokens(language):
    assert set(TRANSLATIONS[language]) == set(TRANSLATIONS["en"])
    for key, english in TRANSLATIONS["en"].items():
        value = TRANSLATIONS[language][key]
        assert isinstance(value, str) and value.strip(), (key, language)
        assert _i18next_placeholders(value) == _i18next_placeholders(english), (key, language)
