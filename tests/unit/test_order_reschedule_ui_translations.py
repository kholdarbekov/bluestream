"""The Orders page's schedule and reschedule copy is seeded, in three languages, and says what
the page says.

Every admin-UI call passes an inline English fallback, so an unseeded key degrades silently to
English for a ru/uz admin and nothing fails. These pin the rows the Create Order picker, the
list column, the detail row and the Reschedule modal render
(docs/superpowers/specs/2026-09-23-admin-order-reschedule-design.md §6).

The owner is `ADMIN_UI_ORDER_TRANSLATIONS` in scripts/seed_backend_translations.py, which holds
every other `ui.orders.*` row. Its dotted `ui.*` keys land in category `ui`.
`AdminUiTranslationService` serves that category in the `common` bundle, which every namespace
falls back to, and in the `orders` bundle by the `ui.orders.` prefix. The seeded `en` value must
be the call site's fallback byte for byte, or the page changes wording the moment the seed runs.

Data only: nothing here opens an app context or touches a database.
"""

import re
from collections import defaultdict
from pathlib import Path

import pytest

from scripts.seed_backend_translations import ADMIN_UI_ORDER_TRANSLATIONS, _category_for
from tests.unit.test_place_group_translation_seeds import (
    _JS_PAIR,
    _JS_STRING,
    _i18next_placeholders,
    _unquote_js,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

# Every file that renders a schedule or reschedule string.
UI_FILES = (
    "src/pages/Orders.js",
    "src/components/orders/deliverySchedule.js",
    "src/components/orders/DeliverySchedulePicker.jsx",
    "src/components/orders/RescheduleOrderModal.jsx",
)

# Rendered by the Create Order picker and the list column since the scheduled-orders feature,
# never seeded until now.
SCHEDULE_KEYS = {
    "ui.orders.delivery_schedule",
    "ui.orders.awaiting_release",
    "ui.orders.deliver_asap",
    "ui.orders.window_between",
    "ui.orders.window_until",
    "ui.orders.window_after",
    "ui.orders.window_anytime",
    "ui.orders.window_morning",
    "ui.orders.window_afternoon",
    "ui.orders.window_evening",
    "ui.orders.window_custom",
    "ui.orders.window_from_any",
    "ui.orders.window_to_any",
}

RESCHEDULE_KEYS = {
    "ui.orders.delivery_status",
    "ui.orders.reschedule",
    "ui.orders.reschedule_title",
    "ui.orders.reschedule_reason",
    "ui.orders.reschedule_reason_placeholder",
    "ui.orders.reschedule_notifies_customer_telegram",
    "ui.orders.reschedule_notifies_customer_email",
    "ui.orders.reschedule_customer_unreachable",
    "ui.orders.reschedule_driver_loses_stop",
    "ui.orders.reschedule_success",
    "ui.orders.reschedule_error.ORDER_NOT_RESCHEDULABLE",
    "ui.orders.reschedule_error.DELIVERY_NOT_RESCHEDULABLE",
    "ui.orders.reschedule_error.DELIVERY_DATE_REQUIRED",
    "ui.orders.reschedule_error.ORDER_RESCHEDULE_PAST_CONTRACT_END",
    "ui.orders.reschedule_error.ORDER_RESCHEDULE_REASON_TOO_LONG",
}

ALL_KEYS = sorted(SCHEDULE_KEYS | RESCHEDULE_KEYS)

# Any schedule/reschedule literal in UI_FILES. The last test proves the two sets above are exactly
# what the UI renders, so a key added later cannot ship unseeded.
_SCHEDULE_LITERAL = re.compile(
    r"""['"](ui\.orders\.(?:reschedule[A-Za-z0-9_.]*|window_[a-z_]+|delivery_schedule"""
    r"""|delivery_status|awaiting_release|deliver_asap))['"]"""
)


def _ui_sources():
    return [(REPO_ROOT / "admin_ui" / rel).read_text(encoding="utf-8") for rel in UI_FILES]


def _fallbacks():
    """key -> every inline English fallback the UI passes for it. A set, so drift shows up."""
    found = defaultdict(set)
    for text in _ui_sources():
        for _, key, expr in _JS_PAIR.findall(text):
            found[key].add("".join(_unquote_js(lit) for lit in re.findall(_JS_STRING, expr, re.S)))
    return found


@pytest.mark.unit
@pytest.mark.parametrize("key", ALL_KEYS)
def test_key_is_seeded_in_three_languages_under_the_shared_ui_category(key):
    row = ADMIN_UI_ORDER_TRANSLATIONS.get(key)
    assert row is not None, f"{key} is missing from ADMIN_UI_ORDER_TRANSLATIONS"
    for lang in ("en", "uz", "ru"):
        # `_ui_tr(en)` alone leaves uz/ru as None, which seeds the ENGLISH text into both.
        assert isinstance(row[lang], str) and row[lang].strip(), (key, lang)
    assert _category_for(key) == "ui"


@pytest.mark.unit
@pytest.mark.parametrize("key", ALL_KEYS)
def test_seeded_english_is_the_call_sites_fallback(key):
    assert _fallbacks()[key] == {ADMIN_UI_ORDER_TRANSLATIONS[key]["en"]}, key


@pytest.mark.unit
@pytest.mark.parametrize("key", ALL_KEYS)
def test_translations_keep_every_interpolation_token(key):
    row = ADMIN_UI_ORDER_TRANSLATIONS[key]
    tokens = _i18next_placeholders(row["en"])
    assert _i18next_placeholders(row["uz"]) == tokens, key
    assert _i18next_placeholders(row["ru"]) == tokens, key


@pytest.mark.unit
def test_every_schedule_key_the_ui_renders_is_seeded():
    used = {key for text in _ui_sources() for key in _SCHEDULE_LITERAL.findall(text)}
    assert used, "no schedule keys found — did the files move?"
    assert used == SCHEDULE_KEYS | RESCHEDULE_KEYS
