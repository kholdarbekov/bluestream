"""The bot's own words on the order screens must be real, trilingual copy (plan 2026-10-27 Task 13; spec §5.3, §16 M5).

Every other word on those screens is the backend's (`customer_actions` labels, confirm texts, reasons and the write
answers' sentences). These are the bot's: the buttons that skip without a reason and keep the order, and the answer
to a `/start order_<id>` / `sub_<id>` link this account does not own. ``render_translation`` never raises, so a key
that was never seeded would show the customer a humanised key on the one screen that decides whether an order goes.
"""

import re

import pytest

from scripts.seed_backend_translations import BACKEND_TRANSLATIONS, _category_for

LANGUAGES = ("en", "uz", "ru")

KEYS = (
    "telegram.orders.skip_no_reason",
    "telegram.orders.keep_delivery",
    "telegram.orders.keep_order",
    "telegram.deep_link.not_found",
)
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")
_PHONE_COPY = re.compile(r"call|позвон|qo'ng'iroq|qoʻngʻiroq|телефон|telefon", re.IGNORECASE)


@pytest.mark.parametrize("key", KEYS)
def test_every_key_is_seeded_in_all_three_languages_under_the_bots_category(key):
    assert key in BACKEND_TRANSLATIONS, f"{key} has no seed row"
    for language in LANGUAGES:
        assert BACKEND_TRANSLATIONS[key].get(language), f"{key} has no {language} copy"
        assert "{" not in BACKEND_TRANSLATIONS[key][language], f"{key} [{language}] carries a placeholder"
    assert _category_for(key) == "telegram"  # telegram_bot/i18n.py loads ONLY this category


@pytest.mark.parametrize("key", KEYS)
def test_the_russian_copy_is_russian_and_no_copy_says_call(key):
    assert _CYRILLIC.search(BACKEND_TRANSLATIONS[key]["ru"]), f"{key} [ru] carries no Cyrillic"
    for language in LANGUAGES:
        assert not _PHONE_COPY.search(BACKEND_TRANSLATIONS[key][language]), f"{key} [{language}]"
