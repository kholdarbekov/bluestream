"""`menu_tap`: the one reply-keyboard tap matcher (spec §1 "Matching taps").

Moved out of bot.py so the address flow and the flow exit share one decider.
`resolve_installed_label` is new: a persistent keyboard outlives a retitle, so
the labels a customer still has on screen must keep resolving.
"""
import pytest

import bot as bot_module
import menu_tap
from menu_tap import bare_label, resolve_installed_label

pytestmark = pytest.mark.unit

INSTALLED = {
    "telegram.menu.products": "💧 Suv buyurtma berish",
    "telegram.menu.support": "🆘 Yordam",
}


def test_bare_label_strips_only_the_leading_emoji():
    assert bare_label("  💧 Suv buyurtma berish ") == "Suv buyurtma berish"
    assert bare_label("+998 90 111 22 33") == "+998 90 111 22 33"


def test_an_installed_label_resolves_to_its_key():
    assert resolve_installed_label("💧 Suv buyurtma berish", INSTALLED) == "telegram.menu.products"
    assert resolve_installed_label("🆘 Yordam", INSTALLED) == "telegram.menu.support"


def test_the_emoji_may_differ_on_either_side_of_an_installed_label():
    assert resolve_installed_label("Suv buyurtma berish", INSTALLED) == "telegram.menu.products"
    assert resolve_installed_label("  🚑 Yordam  ", INSTALLED) == "telegram.menu.support"


def test_text_that_merely_contains_an_installed_label_is_not_a_tap():
    for typed in ("Yordam kerak", "Iltimos 🆘 Yordam", "💧 Suv buyurtma berish ertaga"):
        assert resolve_installed_label(typed, INSTALLED) is None, typed


def test_nothing_installed_or_nothing_typed_resolves_nothing():
    assert resolve_installed_label("🆘 Yordam", {}) is None
    assert resolve_installed_label(None, INSTALLED) is None
    assert resolve_installed_label("", INSTALLED) is None


def test_installed_labels_resolve_without_reading_todays_copy(monkeypatch):
    """A retitled button: today's copy no longer says it, but the customer's phone still does."""
    def _no_lookup(*_a, **_k):
        raise AssertionError("installed labels are what was SENT; they must not be re-read from i18n")

    monkeypatch.setattr("i18n.i18n.get", _no_lookup)
    assert resolve_installed_label("🆘 Yordam", INSTALLED) == "telegram.menu.support"


def test_the_matcher_has_one_home():
    for leftover in ("_EMOJI_PREFIX_RE", "_bare_label", "_is_tap_on_label", "_resolve_tapped_label"):
        assert not hasattr(bot_module, leftover), f"bot.py still carries its own copy: {leftover}"
    assert bot_module.MenuTapFilter is menu_tap.MenuTapFilter
