"""The Orders page's failed-delivery copy is seeded, in three languages, and says what the page
says:
- the Alerts tag and the "Delivery failed" filter (F8);
- the cancel/return confirmation, with its internal reason and the customer-visible note beside
  it (F6);
- the forward-move refusal (F18);
- the admin-only closing reason (F19).

Spec: docs/superpowers/specs/2026-09-25-failed-delivery-awaits-new-date-design.md §3.4, §3.6, §7.

This is the same contract as tests/unit/test_order_reschedule_ui_translations.py, which owns every
`ui.orders.reschedule*` row, including `reschedule_instead` and the F10 refusals. The owner of the
rows is `ADMIN_UI_ORDER_TRANSLATIONS` in scripts/seed_backend_translations.py. Its dotted `ui.*`
keys land in category `ui`, and each seeded `en` value must be the call site's fallback byte for
byte.

Data only: nothing here opens an app context or touches a database.
"""

import re
from collections import defaultdict
from pathlib import Path

import pytest

from scripts.seed_backend_translations import (
    ADMIN_UI_ORDER_TRANSLATIONS,
    BACKEND_TRANSLATIONS,
    _category_for,
)
from tests.unit.test_place_group_translation_seeds import (
    _JS_PAIR,
    _JS_STRING,
    _i18next_placeholders,
    _unquote_js,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
ADMIN_UI_SRC = REPO_ROOT / "admin_ui" / "src"

# Every file that renders one of KEYS.
UI_FILES = (
    "pages/Orders.js",
    "components/orders/CloseOrderDialog.jsx",
)

KEYS = {
    # F8: the Alerts tag (list row and detail) and the list filter.
    "ui.orders.delivery_failed_needs_new_date",
    "ui.orders.delivery_failed_only",
    # F6: the cancel/return confirmation and the Update Status modal.
    "ui.orders.awaiting_new_date_notice",
    "ui.orders.reason_internal_label",
    "ui.orders.notes_customer_visible",
    "ui.orders.return_order_title",
    "ui.orders.return_order_confirm",
    "ui.orders.mark_returned",
    # F18 and F6: PUT /admin/orders/<id>/status refusals, by `data.error_code`.
    "ui.orders.status_error.ORDER_AWAITING_NEW_DATE",
    "ui.orders.status_error.ADMIN_REASON_REQUIRED",
    "ui.orders.status_error.ADMIN_REASON_TOO_LONG",
    # F19.
    "ui.orders.closing_reason",
}

# Rows this change leaves with no caller.
# - `cancelled_by_admin` was the row-menu Cancel's hard-coded `notes`. It reached the customer's
#   timeline in the admin's UI language (F6).
# - `notes_optional` labelled the Update Status note, which is now `notes_customer_visible`.
RETIRED_KEYS = ("ui.orders.cancelled_by_admin", "ui.orders.notes_optional")


def _ui_sources():
    return [(ADMIN_UI_SRC / rel).read_text(encoding="utf-8") for rel in UI_FILES]


def _fallbacks():
    """key -> every inline English fallback the UI passes for it. A set, so drift shows up."""
    found = defaultdict(set)
    for text in _ui_sources():
        for _, key, expr in _JS_PAIR.findall(text):
            found[key].add("".join(_unquote_js(lit) for lit in re.findall(_JS_STRING, expr, re.S)))
    return found


@pytest.mark.unit
@pytest.mark.parametrize("key", sorted(KEYS))
def test_key_is_seeded_in_three_languages_under_the_shared_ui_category(key):
    row = ADMIN_UI_ORDER_TRANSLATIONS.get(key)
    assert row is not None, f"{key} is missing from ADMIN_UI_ORDER_TRANSLATIONS"
    for lang in ("en", "uz", "ru"):
        # `_ui_tr(en)` alone leaves uz/ru as None, which seeds the ENGLISH text into both.
        assert isinstance(row[lang], str) and row[lang].strip(), (key, lang)
    assert _category_for(key) == "ui"


@pytest.mark.unit
@pytest.mark.parametrize("key", sorted(KEYS))
def test_seeded_english_is_the_call_sites_fallback(key):
    assert _fallbacks()[key] == {ADMIN_UI_ORDER_TRANSLATIONS[key]["en"]}, key


@pytest.mark.unit
@pytest.mark.parametrize("key", sorted(KEYS))
def test_translations_keep_every_interpolation_token(key):
    row = ADMIN_UI_ORDER_TRANSLATIONS[key]
    tokens = _i18next_placeholders(row["en"])
    assert _i18next_placeholders(row["uz"]) == tokens, key
    assert _i18next_placeholders(row["ru"]) == tokens, key


@pytest.mark.unit
@pytest.mark.parametrize("key", RETIRED_KEYS)
def test_a_retired_row_is_neither_seeded_nor_rendered(key):
    assert key not in BACKEND_TRANSLATIONS, key
    rendered = [
        str(path.relative_to(REPO_ROOT))
        for path in ADMIN_UI_SRC.rglob("*")
        if path.suffix in {".js", ".jsx"} and key in path.read_text(encoding="utf-8")
    ]
    assert rendered == [], (key, rendered)
