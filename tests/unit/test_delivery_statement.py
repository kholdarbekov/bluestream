"""The approved delivery statement: one SSOT, printed as whole sentences."""
import re
from datetime import date

import pytest

from shared.constants import (
    DELIVERY_STATEMENT,
    DELIVERY_STATEMENT_REVIEWED_ON,
    get_delivery_statement,
)

APPROVED = {   # spec 5.3, character for character. This is the ONLY place a test types the sentences.
    "uz": (
        "Toshkent shahrida Aqua Element kunning birinchi yarmida berilgan buyurtmalarni odatda shu kunning o‘zida yetkazib beradi.",
        "Kechroq berilgan buyurtmalar odatda shu kuni kechqurun yoki ertasi kuni yetib boradi.",
        "Toshkent viloyatining qo‘shni hududlariga odatda buyurtma berilgan kuni yoki ertasi kuni yetkazamiz.",
    ),
    "ru": (
        "В Ташкенте Aqua Element, как правило, доставляет воду в тот же день, если заказ оформлен в первой половине дня.",
        "Более поздние заказы обычно приезжают тем же вечером или на следующий день.",
        "В соседние районы Ташкентской области обычно доставляем в день заказа или на следующий день.",
    ),
    "en": (
        "In Tashkent city, Aqua Element normally delivers the same day when you order in the first half of the day.",
        "Later orders normally arrive the same evening or the next day.",
        "In the neighbouring areas of Tashkent Region we normally deliver on the day you order or the next day.",
    ),
}
LANGS = ("uz", "ru", "en")
PRACTICE = {"uz": ("odatda",), "ru": ("как правило", "обычно"), "en": ("normally",)}
CITY = {"uz": "Toshkent shahrida", "ru": "В Ташкенте", "en": "In Tashkent city"}
FIRST_HALF = {"uz": "kunning birinchi yarmida", "ru": "в первой половине дня", "en": "in the first half of the day"}
FORBIDDEN = {
    "uz": ("bepul", "eng tez", "kafolat", "soat", "tushgacha", "ertalab"),
    "ru": ("бесплатн", "быстр", "гарант", "час", "до обеда", "утр"),
    "en": ("free", "fast", "guarantee", "hour", "morning", "%"),
}


@pytest.mark.parametrize("lang", LANGS)
def test_the_statement_is_exactly_the_approved_text(lang):
    assert get_delivery_statement(lang) == APPROVED[lang] == DELIVERY_STATEMENT[lang]


@pytest.mark.parametrize("lang", LANGS)
def test_every_sentence_is_true_when_lifted_alone(lang):
    for sentence in get_delivery_statement(lang):
        assert any(word in sentence for word in PRACTICE[lang])              # the practice word is inside each sentence
        assert sentence.endswith(".") and sentence.count(".") == 1          # one whole sentence, nothing after it
        assert not re.search(r"\d", sentence)                                # no clock hour, hours figure or percentage
        assert not any(word in sentence.lower() for word in FORBIDDEN[lang])


@pytest.mark.parametrize("lang", LANGS)
def test_sentence_one_names_the_brand_the_city_and_the_first_half_of_the_day(lang):
    first = get_delivery_statement(lang)[0]
    assert "Aqua Element" in first and first.startswith(CITY[lang]) and FIRST_HALF[lang] in first


def test_an_unknown_language_falls_back_to_english():
    assert get_delivery_statement("de") == APPROVED["en"] == get_delivery_statement()


def test_three_sentences_in_exactly_three_languages():
    assert set(DELIVERY_STATEMENT) == {"uz", "ru", "en"}
    assert all(isinstance(value, tuple) and len(value) == 3 for value in DELIVERY_STATEMENT.values())


def test_the_review_date_is_an_iso_date():
    assert date.fromisoformat(DELIVERY_STATEMENT_REVIEWED_ON) == date(2026, 11, 12)
