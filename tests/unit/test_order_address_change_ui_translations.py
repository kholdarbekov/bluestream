"""The Orders page's address-change copy is seeded, in three languages, and says what the page says.

Every admin-UI call passes an inline English fallback, so an unseeded key degrades silently to
English for a ru/uz admin and nothing fails. These pin the rows the order detail's Delivery address
row, the Change address modal and the Order Changes history render
(docs/superpowers/specs/2026-10-18-admin-order-address-change-design.md).

The owner is `ADMIN_UI_ORDER_TRANSLATIONS` in scripts/seed_backend_translations.py. The seeded `en`
value must be the call site's fallback byte for byte, or the page changes wording the moment the
seed runs.

Data only: nothing here opens an app context or touches a database.
"""

import re
from collections import defaultdict
from pathlib import Path

import pytest

from scripts.seed_backend_translations import ADMIN_UI_ORDER_TRANSLATIONS, _category_for
from tests.unit.test_admin_ui_payload_fixture_contracts import _raised_codes
from tests.unit.test_place_group_translation_seeds import (
    _JS_PAIR,
    _JS_STRING,
    _i18next_placeholders,
    _unquote_js,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

UI_FILES = (
    "src/pages/Orders.js",
    "src/components/orders/ChangeAddressModal.jsx",
    "src/utils/addressLabel.js",
)

KEYS = {
    "ui.orders.delivery_address",
    "ui.orders.address_default",
    "ui.orders.change_address",
    "ui.orders.change_address_title",
    "ui.orders.change_address_select",
    "ui.orders.change_address_note",
    "ui.orders.change_address_success",
    "ui.orders.change_address_driver_notice",
    "ui.orders.change_address_agent_warning",
    "ui.orders.change_address_no_branch",
    "ui.orders.change_address_none_pickable",
    "ui.orders.change_address_reason_current",
    "ui.orders.change_address_reason_no_pin",
    "ui.orders.change_address_reason_outside_zone",
    "ui.orders.change_address_reason_cod_capped",
    "ui.orders.change_address_reason_self_decision",
    "ui.orders.change_address_error_locked",
    "ui.orders.change_address_error_invalid",
    "ui.orders.history_address_change",
    "ui.orders.history_branch_change",
}
ALL_KEYS = sorted(KEYS)

_LITERAL = re.compile(
    r"""['"](ui\.orders\.(?:change_address[a-z_]*|address_default|delivery_address"""
    r"""|history_(?:address_change|branch_change)))['"]"""
)

# The codes ChangeAddressModal explains itself: the Vitest `HANDLED` list, in the Map's order.
HANDLED_CODES = (
    "ORDER_ADDRESS_LOCKED",
    "DELIVERY_ADDRESS_LOCKED",
    "ORDER_AWAITING_STAFF_APPROVAL",
    "INVALID_DELIVERY_ADDRESS",
    "ADDRESS_CURRENT",
    "ADDRESS_NO_PIN",
    "ADDRESS_OUTSIDE_ZONE",
    "COD_DEBT_LIMIT_REACHED",
    "SALES_PAY_SELF_DECISION",
)

# Map entries: `['CODE', ['key', 'fallback']]` or `['CODE', REASON_COPY.X]` / `['CODE', LOCKED]`
# (the reason pairs are defined once, C11).
_MAP_ENTRY = re.compile(r"""\[\s*['"]([A-Z][A-Z0-9_]+)['"],\s*(?:\[|REASON_COPY\.|LOCKED\b)""")


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
def test_every_address_change_key_the_ui_renders_is_seeded():
    used = {key for text in _ui_sources() for key in _LITERAL.findall(text)}
    assert used, "no address-change keys found — did the files move?"
    assert used == KEYS


@pytest.mark.unit
def test_every_handled_code_is_raised_by_the_backend():
    modal_src = (REPO_ROOT / "admin_ui/src/components/orders/ChangeAddressModal.jsx").read_text(encoding="utf-8")
    parsed = re.findall(_MAP_ENTRY, modal_src)
    # A quote style the regex misses fails here, loudly, instead of passing on an empty set.
    assert tuple(parsed) == HANDLED_CODES
    services = REPO_ROOT / "business_app" / "services"
    raised = _raised_codes(
        [
            services / "order_address_change_service.py",
            services / "order_service.py",
            services / "sales" / "pay_rules.py",
            services / "sales" / "agent_order_approval_service.py",
        ]
    )
    assert set(parsed) <= raised, sorted(set(parsed) - raised)
