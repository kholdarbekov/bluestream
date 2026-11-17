"""Every customer-bot line of the price change is real copy (Tasks 45-47 list their keys here).

`shared.i18n_rendering.render_translation` never raises: a dropped language or a renamed placeholder
reaches the customer as a humanised key. Same precedent as `test_typed_quantity_copy_is_seeded.py`.
"""

from __future__ import annotations

import re
from string import Formatter

import pytest

from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

LANGUAGES = ("en", "uz", "ru")

# key -> the placeholders its call site passes. Written by hand; Tasks 46-47 add theirs.
BOT_KEYS = {
    "telegram.price_change.upcoming_line": {"date", "price"},
    "telegram.price_change.courtesy_split": {"courtesy_qty", "courtesy_price", "regular_qty", "regular_price"},
    "telegram.price_change.courtesy_only": {"courtesy_qty", "courtesy_price"},
    "telegram.price_change.courtesy_thanks": set(),
    "telegram.price_change.price_changed_note": set(),
}

CURRENCY_WORD = {"en": "so'm", "uz": "so'm", "ru": "сум"}
_CURRENCY_WORDS = re.compile(r"(?<!\w)(UZS|so'm|сум)(?!\w)")
_PRESSURE = re.compile(
    r"missed|last chance|save|hurry|only until"
    r"|o'?tkazib yubor|so'nggi imkoniyat|tejang|shoshiling|faqat .* gacha"
    r"|упустите|последний шанс|сэкономьте|торопитесь|только до",
    re.IGNORECASE,
)


def _placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in Formatter().parse(text) if name}


@pytest.mark.parametrize("key", sorted(BOT_KEYS))
def test_every_price_change_key_is_seeded_in_all_three_languages(key):
    assert key in BACKEND_TRANSLATIONS, f"{key} has no seed row"
    for language in LANGUAGES:
        assert BACKEND_TRANSLATIONS[key].get(language), f"{key} has no {language} copy"


@pytest.mark.parametrize("key", sorted(BOT_KEYS))
def test_every_price_change_key_carries_exactly_the_values_it_is_passed(key):
    for language in LANGUAGES:
        assert _placeholders(BACKEND_TRANSLATIONS[key][language]) == BOT_KEYS[key], (
            f"{key} [{language}] does not carry the values the call site passes"
        )


def test_the_currency_word_is_one_word_per_language():
    row = BACKEND_TRANSLATIONS["telegram.currency.uzs"]
    assert {language: row[language] for language in LANGUAGES} == CURRENCY_WORD
    notice = BACKEND_TRANSLATIONS["telegram.customer_notice.price_change_item"]
    for language, word in CURRENCY_WORD.items():
        assert notice[language].endswith(word), f"the notice item line ends differently in {language}"


@pytest.mark.parametrize("key", sorted(BOT_KEYS))
def test_no_price_change_copy_types_a_price_a_currency_or_pressure(key):
    for language in LANGUAGES:
        text = BACKEND_TRANSLATIONS[key][language]
        assert not re.search(r"\d{2,}", text), f"{key} [{language}] types a number: {text!r}"
        assert not _CURRENCY_WORDS.search(text), f"{key} [{language}] types the currency word: {text!r}"
        assert not _PRESSURE.search(text), f"{key} [{language}] pressures the customer: {text!r}"
