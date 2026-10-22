"""The Help screen's copy (spec 2026-10-21 §1, Deploy): it points at the bottom buttons, never at /menu."""

import pytest

from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

pytestmark = pytest.mark.unit


def test_the_help_hint_points_at_the_buttons_below():
    assert BACKEND_TRANSLATIONS["telegram.help.command_hint"] == {
        "uz": "Kerakli bo'limni pastdagi tugmalar orqali tanlang 👇",
        "ru": "Выберите нужный раздел кнопками внизу 👇",
        "en": "Choose a section with the buttons below 👇",
    }


def test_the_help_screen_intro_is_seeded_in_every_language():
    intro = BACKEND_TRANSLATIONS["telegram.support.menu_intro"]
    assert set(intro) == {"uz", "ru", "en"}
    assert all(value.strip() and "/menu" not in value for value in intro.values())
