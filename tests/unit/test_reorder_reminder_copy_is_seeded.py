"""Every string a reorder reminder shows is seeded, trilingual and assembled in ONE place.

Spec docs/superpowers/specs/2026-10-09-lapsed-customer-reorder-reminders-design.md §7.5, §15.9.

`shared.i18n_rendering.render_translation` never raises: drop a language, rename a
placeholder or misspell a key and the customer silently reads a humanised key ("R1
personal") on the one message whose whole job is to sound like a person. The precedent is
tests/telegram_bot/test_agent_order_copy_is_seeded.py; this file adds three guards that one
does not need:

* `shared/reorder_reminder_copy.py` is the ONLY product module that spells a
  reorder-reminder key, so the bot route, the keyboard, the callbacks, the settings screen
  and the email cannot each grow their own idea of which key carries which placeholder;
* the composer is driven through every branch with a recording renderer, so the values it
  really passes are compared with every language's seed row;
* the literal Russian day phrases, because the old plural mapping printed "14 дня".
"""

from __future__ import annotations

import re
from pathlib import Path
from string import Formatter

import pytest

from scripts.seed_backend_translations import BACKEND_TRANSLATIONS
from shared import reorder_reminder_copy as copy
from shared.i18n_rendering import render_translation

LANGUAGES = ("en", "uz", "ru")
PREFIX = "telegram.reorder_reminder."
REPO = Path(__file__).resolve().parents[2]
COMPOSER = REPO / "shared" / "reorder_reminder_copy.py"

# key -> the placeholders the composer passes. Written by hand, so a rename has to be made in
# three places on purpose; test_the_composer_passes_exactly_these_placeholders ties it to
# what the code really passes.
KEYS = {
    "telegram.reorder_reminder.greeting": {"name"},
    "telegram.reorder_reminder.greeting_noname": set(),
    "telegram.reorder_reminder.r1_personal": {"greeting", "days_phrase", "gap_phrase"},
    "telegram.reorder_reminder.r1_preset": {"greeting", "days_phrase"},
    "telegram.reorder_reminder.r2": {"greeting"},
    "telegram.reorder_reminder.tap_line": {"items"},
    "telegram.reorder_reminder.email_cta_line": {"items"},
    "telegram.reorder_reminder.item": {"qty", "name"},
    "telegram.reorder_reminder.items_fallback": set(),
    "telegram.reorder_reminder.days.one": {"n"},
    "telegram.reorder_reminder.days.few": {"n"},
    "telegram.reorder_reminder.days.many": {"n"},
    "telegram.reorder_reminder.days.other": {"n"},
    "telegram.reorder_reminder.btn_order": set(),
    "telegram.reorder_reminder.btn_later": {"snooze_phrase"},
    "telegram.reorder_reminder.btn_stop": set(),
    "telegram.reorder_reminder.later_ok": {"snooze_phrase"},
    "telegram.reorder_reminder.stop_ok": {"profile_label", "notifications_label"},
    "telegram.reorder_reminder.expired": set(),
    "telegram.reorder_reminder.email_btn_website": set(),
    "telegram.reorder_reminder.email_btn_telegram": set(),
    "telegram.reorder_reminder.email_unsubscribe_line": set(),
    "telegram.reorder_reminder.settings_on": set(),
    "telegram.reorder_reminder.settings_off": set(),
    "telegram.reorder_reminder.settings_button_on": set(),
    "telegram.reorder_reminder.settings_button_off": set(),
}

# `{qty} × {name}` is the spec's own Russian line (§7.5): it has no word to translate.
NO_CYRILLIC_NEEDED = {"telegram.reorder_reminder.item"}

_CYRILLIC = re.compile(r"[А-Яа-яЁё]")
_KEY_LITERAL = re.compile(r"telegram\.reorder_reminder\.[a-z0-9_]+")
# Customer copy never asks the customer to call; it points to the bot chat. The Uzbek word is
# "qo'ng'iroq", and each of its apostrophes may be written as ' ‘ ’ ʻ or ʼ.
_APOSTROPHE = "['‘’ʻʼ]"
_PHONE_PROMPT = re.compile(
    rf"\bcall\b|\btel\.|tel:|phone|\+998|звон|телефон|telefon|qo{_APOSTROPHE}ng{_APOSTROPHE}iroq",
    re.IGNORECASE,
)
_PRODUCT_TREES = ("business_app", "telegram_bot", "staff_bot", "shared")
# Master amendment 13: every item carries its product name in all three languages.
ITEMS = [
    {"qty": 2, "names": {"uz": "Toza suv 19 l", "ru": "Чистая вода 19 л", "en": "Pure Water 19L"}},
    {"qty": 1, "names": {"uz": "Kuller stakanlari", "ru": "Стаканчики для кулера", "en": "Cooler cups"}},
    {"qty": 1, "names": {"uz": "Qoʻl nasosi", "ru": "Ручная помпа", "en": "Hand pump"}},
]


def _placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in Formatter().parse(text) if name}


def _seeded(language: str):
    """`render(key, **values)` over the seed rows, through production's own renderer."""

    def render(key, **values):
        return render_translation(key, BACKEND_TRANSLATIONS[key][language], (), values)

    return render


def _exercise(render, language: str) -> None:
    """Every composer entry point, every branch its arguments select."""
    copy.telegram_text(render, language, step=1, name="Alice", days=12, gap=7, items=ITEMS)
    copy.telegram_text(render, language, step=1, name=None, days=31, gap=None, items=[])
    copy.telegram_text(render, language, step=2, name="Alice", days=19, gap=7, items=ITEMS)
    copy.button_labels(render, language, snooze_days=14)
    copy.later_ok(render, language, snooze_days=14)
    copy.stop_ok(render)
    copy.expired(render)
    copy.email_parts(render, language, step=1, name="Alice", days=12, gap=7, items=ITEMS)
    for enabled in (True, False):
        copy.settings_status(render, enabled)
        copy.settings_button(render, enabled)
    for n in (1, 2, 5, 21, 22):
        copy.days_phrase(render, language, n)


@pytest.mark.parametrize("key", sorted(KEYS))
def test_every_reminder_key_is_seeded_in_all_three_languages(key):
    assert key in BACKEND_TRANSLATIONS, f"{key} is rendered but never seeded"
    for language in LANGUAGES:
        assert BACKEND_TRANSLATIONS[key].get(language), f"{key} has no {language} copy"


def test_the_seed_holds_no_reminder_key_this_file_does_not_check():
    seeded = {key for key in BACKEND_TRANSLATIONS if key.startswith(PREFIX)}
    assert seeded == set(KEYS), (
        f"only seeded: {sorted(seeded - set(KEYS))}; only listed here: {sorted(set(KEYS) - seeded)}"
    )


@pytest.mark.parametrize("key", sorted(KEYS))
def test_every_language_carries_exactly_the_same_placeholders(key):
    for language in LANGUAGES:
        assert _placeholders(BACKEND_TRANSLATIONS[key][language]) == KEYS[key], (key, language)


def test_the_composer_passes_exactly_these_placeholders():
    passed: dict[str, list[set[str]]] = {}

    def recording(language):
        seeded = _seeded(language)

        def render(key, **values):
            passed.setdefault(key, []).append(set(values))
            return seeded(key, **values)

        return render

    for language in LANGUAGES:
        _exercise(recording(language), language)

    reached = {key for key in passed if key.startswith(PREFIX)}
    assert reached == set(KEYS), f"never rendered: {sorted(set(KEYS) - reached)}"
    for key in reached:
        for values in passed[key]:
            assert values == KEYS[key], (key, values)


@pytest.mark.parametrize("key", sorted(set(KEYS) - NO_CYRILLIC_NEEDED))
def test_the_russian_copy_is_actually_russian(key):
    assert _CYRILLIC.search(BACKEND_TRANSLATIONS[key]["ru"]), BACKEND_TRANSLATIONS[key]["ru"]


@pytest.mark.parametrize("key", sorted(KEYS))
def test_no_reminder_copy_asks_the_customer_to_call(key):
    for language in LANGUAGES:
        assert not _PHONE_PROMPT.search(BACKEND_TRANSLATIONS[key][language]), (key, language)


@pytest.mark.parametrize(
    "text",
    [
        "Qo‘ng‘iroq qiling",  # U+2018 on both apostrophes
        "qo’ng’iroq qiling",  # U+2019 on both apostrophes
        "qoʻngʼiroq qiling",  # U+02BB then U+02BC
        "qo'ng‘iroq qiling",  # ASCII then U+2018
        "Tel. 90 123 45 67",
        "call the shop",
    ],
)
def test_the_phone_prompt_guard_catches_every_spelling_of_a_call_request(text):
    assert _PHONE_PROMPT.search(text)


def test_the_composer_spells_exactly_the_seeded_keys():
    rendered = set(_KEY_LITERAL.findall(COMPOSER.read_text(encoding="utf-8")))
    expected = {key for key in KEYS if not key.startswith(f"{copy.DAYS_KEY}.")} | {copy.DAYS_KEY}
    assert rendered == expected


def test_no_other_product_module_spells_a_reminder_key():
    offenders = []
    for tree in _PRODUCT_TREES:
        for path in (REPO / tree).rglob("*"):
            if path == COMPOSER or path.suffix not in {".py", ".html", ".js"} or "node_modules" in path.parts:
                continue
            if _KEY_LITERAL.search(path.read_text(encoding="utf-8", errors="ignore")):
                offenders.append(str(path.relative_to(REPO)))
    assert offenders == [], (
        "only shared/reorder_reminder_copy.py may name a reorder-reminder key; "
        f"render through it instead: {offenders}"
    )


@pytest.mark.parametrize(
    "n, phrase",
    [(1, "1 день"), (2, "2 дня"), (5, "5 дней"), (11, "11 дней"), (14, "14 дней"), (21, "21 день"), (22, "22 дня")],
)
def test_russian_day_phrases_read_as_a_russian_speaker_writes_them(n, phrase):
    assert copy.days_phrase(_seeded("ru"), "ru", n) == phrase


@pytest.mark.parametrize(
    "language, n, phrase",
    [("en", 1, "1 day"), ("en", 14, "14 days"), ("uz", 1, "1 kun"), ("uz", 14, "14 kun")],
)
def test_english_and_uzbek_day_phrases(language, n, phrase):
    assert copy.days_phrase(_seeded(language), language, n) == phrase


def test_the_russian_first_reminder_reads_as_one_sentence():
    assert copy.statement(_seeded("ru"), "ru", step=1, name="Алия", days=21, gap=14) == (
        "Здравствуйте, Алия! Последняя доставка воды была 21 день назад, "
        "а обычно вы заказываете примерно раз в 14 дней."
    )


def test_a_preset_reminder_without_a_name_greets_without_one():
    assert copy.statement(_seeded("en"), "en", step=1, name=None, days=31, gap=None) == (
        "Hello! Your last water delivery was 31 days ago. Running low?"
    )


def test_reminder_two_is_the_short_one():
    assert copy.statement(_seeded("uz"), "uz", step=2, name="Aziz", days=19, gap=7) == (
        "Assalomu alaykum, Aziz! Kichik eslatma."
    )


def test_the_telegram_message_names_at_most_two_order_lines():
    assert copy.telegram_text(_seeded("en"), "en", step=1, name="Alice", days=12, gap=7, items=ITEMS) == (
        "Hi Alice! Your last water delivery was 12 days ago, and you usually order about every 7 days."
        "\n\nTap 🛒 Order again and we'll bring your usual 2 × Pure Water 19L, 1 × Cooler cups."
    )


def test_with_no_order_lines_the_customer_is_offered_water():
    assert copy.items_text(_seeded("ru"), "ru", []) == "воду"


@pytest.mark.parametrize(
    "language, line",
    [
        ("ru", "2 × Чистая вода 19 л, 1 × Стаканчики для кулера"),
        ("uz", "2 ta Toza suv 19 l, 1 ta Kuller stakanlari"),
        ("en", "2 × Pure Water 19L, 1 × Cooler cups"),
    ],
)
def test_order_lines_are_named_in_the_language_the_copy_is_rendered_in(language, line):
    """Master amendment 13 / Review Focus #3: an item's name and the sentence around it are
    never in two different languages."""
    assert copy.items_text(_seeded(language), language, ITEMS) == line


def test_an_item_missing_the_copys_language_falls_back_to_a_name_it_has():
    assert copy.item_name({"qty": 1, "names": {"en": "Pure Water 19L"}}, "ru") == "Pure Water 19L"


def test_stop_names_the_real_settings_path():
    assert "«👤 Мой профиль» → «🔔 Уведомления»" in copy.stop_ok(_seeded("ru"))


# Exact values per composer branch. Written against the real seed rows, in English, so a swapped
# `if enabled` arm or a swapped button key changes a string the customer would read.


def test_the_settings_line_reads_the_effective_value():
    assert copy.settings_status(_seeded("en"), True) == "Reorder reminders: On"
    assert copy.settings_status(_seeded("en"), False) == "Reorder reminders: Off"


def test_the_settings_button_names_the_action_it_takes():
    """Reminders on -> the button turns them off, and the reverse."""
    assert copy.settings_button(_seeded("en"), True) == "🔕 Turn reorder reminders off"
    assert copy.settings_button(_seeded("en"), False) == "🔔 Turn reorder reminders on"


def test_each_reminder_button_gets_its_own_label():
    assert copy.button_labels(_seeded("en"), "en", snooze_days=14) == {
        "order": "🛒 Order again",
        "later": "⏰ Remind me in 14 days",
        "stop": "🔕 Stop these reminders",
    }


def test_the_email_names_website_and_telegram_as_their_own_buttons():
    assert copy.email_parts(_seeded("en"), "en", step=1, name="Alice", days=12, gap=7, items=ITEMS) == {
        "greeting": "Hi Alice!",
        "statement": "Hi Alice! Your last water delivery was 12 days ago, and you usually order about every 7 days.",
        "cta_line": (
            "Order on our website or in our Telegram bot and we'll bring your usual "
            "2 × Pure Water 19L, 1 × Cooler cups."
        ),
        "btn_website": "Order on the website",
        "btn_telegram": "Order in Telegram",
        "unsubscribe_line": "Don't want these reminders? Unsubscribe",
    }


def test_the_snooze_confirmation_names_the_snooze_length():
    assert copy.later_ok(_seeded("en"), "en", snooze_days=14) == "⏰ OK — we'll remind you in 14 days."


def test_the_expired_reminder_says_it_is_over():
    assert copy.expired(_seeded("en")) == "This reminder is no longer active."


# Order lines: a language, a name and a quantity are each checked before a line is printed.


def test_a_regional_language_code_reads_as_its_base_language():
    assert copy.item_name(ITEMS[0], "ru-RU") == "Чистая вода 19 л"
    assert copy.item_name(ITEMS[0], "uz_UZ") == "Toza suv 19 l"


def test_a_missing_requested_language_falls_back_to_en_then_ru_then_uz():
    only_en_and_uz = {"qty": 1, "names": {"uz": "Toza suv 19 l", "en": "Pure Water 19L"}}
    only_uz_and_ru = {"qty": 1, "names": {"uz": "Toza suv 19 l", "ru": "Чистая вода 19 л"}}
    only_uz = {"qty": 1, "names": {"uz": "Toza suv 19 l"}}
    assert copy.item_name(only_en_and_uz, "ru") == "Pure Water 19L"
    assert copy.item_name(only_uz_and_ru, "en") == "Чистая вода 19 л"
    assert copy.item_name(only_uz, "en") == "Toza suv 19 l"


def test_a_line_with_no_name_in_any_language_is_left_out_of_the_order():
    nameless = {"qty": 1, "names": {"uz": "", "ru": "   ", "en": None}}
    assert copy.items_text(_seeded("en"), "en", [nameless, ITEMS[1]]) == "1 × Cooler cups"
    assert copy.items_text(_seeded("en"), "en", [{"qty": 1}]) == "water"


def test_a_line_with_no_quantity_is_left_out_instead_of_printing_none():
    no_qty = {"qty": None, "names": ITEMS[0]["names"]}
    assert copy.items_text(_seeded("en"), "en", [no_qty, ITEMS[1]]) == "1 × Cooler cups"


def test_skipped_lines_do_not_use_up_the_two_order_lines():
    nameless = {"qty": 1, "names": {}}
    assert copy.items_text(_seeded("en"), "en", [nameless, *ITEMS]) == "2 × Pure Water 19L, 1 × Cooler cups"


# `printable` is the ONE "can this order line be printed" rule. `items_text` skips what it refuses,
# and the backend payload (business_app/services/reorder_reminders/messaging.py) drops the same
# lines before it takes its MAX_ITEMS (tests/integration/test_reorder_reminder_telegram_send.py).


@pytest.mark.parametrize(
    "item",
    [
        {"qty": 0, "names": ITEMS[0]["names"]},
        {"qty": -1, "names": ITEMS[0]["names"]},
        {"qty": None, "names": ITEMS[0]["names"]},
        {"names": ITEMS[0]["names"]},
        {"qty": True, "names": ITEMS[0]["names"]},
        {"qty": False, "names": ITEMS[0]["names"]},
        {"qty": "2", "names": ITEMS[0]["names"]},
        {"qty": 2.0, "names": ITEMS[0]["names"]},
        {"qty": 2, "names": {"uz": "", "ru": "   ", "en": None}},
        {"qty": 2, "names": {"de": "Wasser"}},
        {"qty": 2},
    ],
    ids=[
        "zero", "negative", "none", "no-qty", "bool-true", "bool-false", "string", "float",
        "blank-names", "only-another-language", "no-names",
    ],
)
def test_a_line_without_a_positive_whole_quantity_or_any_name_is_not_printable(item):
    assert copy.printable(item) is False


@pytest.mark.parametrize("only", copy.LANGUAGES)
def test_a_line_with_a_positive_quantity_and_one_name_is_printable_in_every_language(only):
    """A name in any one language is enough, and `item_name` then finds it whatever language the
    copy is rendered in: a printable line never prints an empty name."""
    item = {"qty": 1, "names": {only: "Water"}}
    assert copy.printable(item) is True
    assert [copy.item_name(item, language) for language in copy.LANGUAGES] == ["Water", "Water", "Water"]
    assert [copy.printable(line) for line in ITEMS] == [True, True, True]


def test_a_line_with_a_zero_or_boolean_quantity_is_left_out_of_the_order():
    """Never "0 × …" or "True × …": the composer skips every line `printable` refuses, and the
    skipped lines do not use up the two order lines."""
    zero = {"qty": 0, "names": ITEMS[0]["names"]}
    boolean = {"qty": True, "names": ITEMS[2]["names"]}
    assert copy.items_text(_seeded("en"), "en", [zero, boolean, ITEMS[1]]) == "1 × Cooler cups"
