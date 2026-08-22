"""The one line a customer reads when checkout SUCCEEDED but the screen did not.

``telegram_bot/handlers/orders.py::confirm_order`` disarms Confirm, creates the
order, clears the cart, and only then repaints the checkout bubble. When
Telegram refuses BOTH the edit and the replacement (blocked bot, chat gone),
the callback alert is the only surface left and it renders
``telegram.orders.order_placed_screen_not_updated``.

Unseeded, ``Translation.get`` humanises the last key segment instead, so a uz
or ru customer is shown "Order placed screen not updated" — English, at the one
moment in the whole flow where misreading the message means placing (and
paying for) a second order. That makes this key a money guard, not decoration:
it must exist in all three languages, carry no ``{...}`` placeholder (the call
site passes no kwargs, so a placeholder would ship raw braces to the customer),
and fit inside a Telegram callback alert or Telegram drops the answer whole.
"""

import pathlib
import re
import sys

import pytest

from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

# telegram_bot modules use workdir-relative BARE imports, so they are not
# importable as `telegram_bot.i18n` from tests/unit.
# Same pattern as tests/unit/test_customer_bot_cod_restriction_notice.py:24.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "telegram_bot"))

from i18n import Translation  # noqa: E402

KEY = "telegram.orders.order_placed_screen_not_updated"
LANGUAGES = ("en", "uz", "ru")

# Telegram's answerCallbackQuery `text` limit. Over it, the API rejects the
# answer and the customer gets NOTHING — the exact failure this key exists to
# prevent.
CALLBACK_ALERT_LIMIT = 200

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_post_placement_fallback_line_is_seeded_in_every_language(language):
    assert KEY in BACKEND_TRANSLATIONS, f"{KEY} is rendered by confirm_order but never seeded"
    value = BACKEND_TRANSLATIONS[KEY].get(language)
    assert isinstance(value, str) and value.strip(), f"{KEY}:{language} is empty"


@pytest.mark.parametrize("language", LANGUAGES)
def test_it_is_real_copy_and_not_the_humanised_english_fallback(language):
    """Seeding the humanised text would pass a presence check and still leak English.

    ``humanised_missing_key`` is the production formula, imported rather than
    re-derived, so this comparison cannot drift away from what ``get`` returns.
    """
    value = BACKEND_TRANSLATIONS[KEY][language]

    assert value != Translation.humanised_missing_key(KEY)


def test_the_three_languages_are_actually_different_copy():
    """A uz/ru row holding the English sentence is the same outage, seeded."""
    values = {BACKEND_TRANSLATIONS[KEY][language] for language in LANGUAGES}

    assert len(values) == len(LANGUAGES), f"{KEY} reuses one string across languages"


@pytest.mark.parametrize("language", LANGUAGES)
def test_it_fits_a_telegram_callback_alert(language):
    value = BACKEND_TRANSLATIONS[KEY][language]

    assert len(value) <= CALLBACK_ALERT_LIMIT, (
        f"{KEY}:{language} is {len(value)} chars; Telegram rejects the whole answer"
    )


@pytest.mark.parametrize("language", LANGUAGES)
def test_it_carries_no_placeholder_the_call_site_cannot_fill(language):
    value = BACKEND_TRANSLATIONS[KEY][language]

    assert not re.search(r"\{[A-Za-z_0-9]*\}", value), (
        f"{KEY}:{language} interpolates, but confirm_order calls i18n.get with no kwargs"
    )


def test_the_handler_really_renders_this_exact_key():
    """Call-site parity: a seed for a key nobody reads is dead weight, and a
    key whose spelling drifts from the handler's is an unseeded key again."""
    source = (REPO_ROOT / "telegram_bot" / "handlers" / "orders.py").read_text(encoding="utf-8")

    assert f"'{KEY}'" in source or f'"{KEY}"' in source
