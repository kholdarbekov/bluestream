"""One plural rule set for the bot, the email and the backend (spec 2026-10-09 §7.5, §15.9).

`plural_category` is CLDR's integer rule: Russian one/few/many, English and Uzbek one/other. The
copy keys are `<base>.<category>`, so the literal Russian phrases below come from the production
renderer over a seeded-style table, never from a re-implemented rule.
"""

import pytest

from business_app.utils import translations as translations_module
from business_app.utils.translations import get_plural_rules, get_plural_translation
from shared.i18n_rendering import plural_category, plural_key, render_translation

pytestmark = pytest.mark.unit

DAYS = "telegram.reorder_reminder.days"
# The spec's §7.5 table, as it will be seeded (Task 14).
RU_DAYS = {
    f"{DAYS}.one": "{n} день",
    f"{DAYS}.few": "{n} дня",
    f"{DAYS}.many": "{n} дней",
    f"{DAYS}.other": "{n} дня",
}


@pytest.mark.parametrize(
    "n, expected",
    [(1, "1 день"), (2, "2 дня"), (5, "5 дней"), (11, "11 дней"), (14, "14 дней"), (21, "21 день"), (22, "22 дня")],
)
def test_the_russian_day_phrases_read_right(n, expected):
    key = plural_key(DAYS, "ru", n)

    assert render_translation(key, RU_DAYS[key], (), {"n": n}) == expected


@pytest.mark.parametrize(
    "n, expected",
    [
        (0, "many"), (1, "one"), (2, "few"), (4, "few"), (5, "many"), (11, "many"), (12, "many"),
        (14, "many"), (21, "one"), (22, "few"), (25, "many"), (101, "one"), (111, "many"),
        (112, "many"), (122, "few"),
    ],
)
def test_russian_has_one_few_and_many(n, expected):
    assert plural_category("ru", n) == expected


@pytest.mark.parametrize("language", ["en", "uz"])
@pytest.mark.parametrize("n, expected", [(0, "other"), (1, "one"), (2, "other"), (11, "other"), (21, "other")])
def test_english_and_uzbek_have_one_and_other(language, n, expected):
    assert plural_category(language, n) == expected


def test_a_regional_or_unknown_code_uses_its_base_language_else_english():
    assert plural_category("ru-RU", 2) == "few"
    assert plural_category("RU", 5) == "many"
    assert plural_category("de", 1) == "one"
    assert plural_category("", 3) == "other"


def test_the_key_suffix_is_the_category():
    assert plural_key(DAYS, "ru", 3) == "telegram.reorder_reminder.days.few"
    assert plural_key(DAYS, "uz", 3) == "telegram.reorder_reminder.days.other"


@pytest.mark.parametrize(
    "language, forms, indices",
    [
        ("ru", ["one", "few", "other"], {0: 2, 1: 0, 2: 1, 4: 1, 5: 2, 11: 2, 12: 2, 14: 2, 21: 0, 22: 1, 25: 2, 111: 2}),
        ("en", ["singular", "plural"], {0: 1, 1: 0, 2: 1, 11: 1, 21: 1}),
        ("uz", ["singular", "plural"], {0: 1, 1: 0, 2: 1, 11: 1, 21: 1}),
        ("de", ["singular", "plural"], {0: 1, 1: 0, 2: 1}),
        ("ru-RU", ["singular", "plural"], {1: 0, 2: 1, 5: 1}),
    ],
    ids=["ru", "en", "uz", "unknown-falls-back-to-english", "regional-code-unchanged"],
)
def test_get_plural_rules_keeps_its_forms_and_indices(language, forms, indices):
    """§7.5: existing callers see no change, including today's English fallback for "ru-RU"."""
    rules = get_plural_rules(language)

    assert rules["forms"] == forms
    assert {n: rules["rule"](n) for n in indices} == indices


@pytest.mark.parametrize(
    "language, count, expected_key",
    [
        ("ru", 1, "product.item"),
        ("ru", 2, "product.item_few"),
        ("ru", 5, "product.item_plural"),
        ("ru", 11, "product.item_plural"),
        ("ru", 21, "product.item"),
        ("ru", 22, "product.item_few"),
        ("en", 0, "product.item_plural"),
        ("en", 1, "product.item"),
        ("en", 2, "product.item_plural"),
        ("uz", 1, "product.item"),
        ("uz", 3, "product.item_plural"),
    ],
)
def test_existing_plural_translation_callers_pick_the_same_key(monkeypatch, language, count, expected_key):
    asked = []

    def _get_translation(key, lang, **kwargs):
        asked.append((key, lang, kwargs["count"], kwargs["n"]))
        return f"<{key}>"

    monkeypatch.setattr(translations_module.translation_service, "get_translation", _get_translation)

    assert get_plural_translation("product.item", count, language) == f"<{expected_key}>"
    assert asked == [(expected_key, language, count, count)]
