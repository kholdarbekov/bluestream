"""The Return prepayment copy is seeded, in three languages, and says what the admin UI says.

Every admin-UI call passes an inline English fallback, so an unseeded key degrades silently to English
for a ru/uz admin and nothing fails. These pin the rows the Prepayments drawer, the Users prepayment
card, the Return prepayment modal, the Undo modal and the collected-cash correction modal render.

The owner is `PREPAYMENT_TRANSLATIONS` in scripts/seed_prepayment_translations.py. The seeded `en` value
must be the call site's fallback byte for byte, or the page changes wording the moment the seed runs.

Data only: nothing here opens an app context or touches a database.
"""

import re
from collections import defaultdict
from pathlib import Path

import pytest

from scripts.seed_prepayment_translations import PREPAYMENT_TRANSLATIONS
from tests.unit.test_place_group_translation_seeds import (
    _JS_PAIR,
    _JS_STRING,
    _i18next_placeholders,
    _unquote_js,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

UI_FILES = (
    "src/components/prepayments/ReturnPrepaymentModal.jsx",
    "src/components/prepayments/PayoutRows.jsx",
    "src/pages/Prepayments.js",
    "src/pages/Users.js",
    "src/utils/cashScopeDisplay.js",
)

KEYS = {
    "ui.prepayments.return_button",
    "ui.prepayments.mode_amount",
    "ui.prepayments.mode_target",
    "ui.prepayments.paid_by_office",
    "ui.prepayments.paid_by_driver_current",
    "ui.prepayments.paid_by_driver_settled",
    "ui.prepayments.returned",
    "ui.prepayments.undone",
    "ui.prepayments.undo",
    "ui.prepayments.undo_title",
    "ui.prepayments.undo_success",
    "ui.prepayments.lifetime_returned",
    "ui.prepayments.returns_table",
    "ui.prepayments.card_click_share",
    "ui.prepayments.release_by_cancel",
    "ui.prepayments.correction_blocked_by_return",
    "ui.prepayments.return_amount",
    "ui.prepayments.return_paid_by",
    "ui.prepayments.return_driver",
    "ui.prepayments.return_driver_placeholder",
    "ui.prepayments.return_driver_required",
    "ui.prepayments.return_paid_at",
    "ui.prepayments.return_reason",
    "ui.prepayments.return_reason_required",
    "ui.prepayments.return_preview",
    "ui.prepayments.return_free_balance",
    "ui.prepayments.return_pending_orders",
    "ui.prepayments.return_live_subscriptions",
    "ui.prepayments.return_place_debts",
    "ui.prepayments.return_session_effect",
    "ui.prepayments.return_session_closes_warning",
    "ui.prepayments.return_success_amount",
    "ui.prepayments.return_amount_required",
    "ui.prepayments.return_drivers_failed",
    "ui.prepayments.return_paid_at_future",
    "ui.prepayments.error_amount",
    "ui.prepayments.error_paid_at",
    "ui.prepayments.error_reason",
    "ui.prepayments.error_payer",
    "ui.prepayments.error_key_reused",
    "ui.prepayments.error_request",
    "ui.prepayments.return_contract_effect",
    "ui.prepayments.order_status_pending",
    "ui.prepayments.order_status_confirmed",
    "ui.prepayments.order_status_preparing",
    "ui.prepayments.order_status_out_for_delivery",
    "ui.prepayments.sub_status_active",
    "ui.prepayments.sub_status_paused",
    "ui.prepayments.return_amount_locked_hint",
    "ui.prepayments.return_success",
    "ui.prepayments.block_no_free_balance",
    "ui.prepayments.block_amount_exceeds_free_balance",
    "ui.prepayments.block_target_not_below_free_balance",
    "ui.prepayments.block_driver_not_eligible",
    "ui.prepayments.block_no_active_session",
    "ui.prepayments.block_amount_exceeds_driver_cash",
    "ui.prepayments.block_paid_before_current_trip",
    "ui.prepayments.block_ambiguous_contract_mirror",
    "ui.prepayments.block_units_contract_mirror",
    "ui.prepayments.block_contract_account_missing",
    "ui.prepayments.undo_block_already_undone",
    "ui.prepayments.undo_block_funding_event_voided",
    "ui.prepayments.undo_block_session_checked",
}
ALL_KEYS = sorted(KEYS)

# Seeded elsewhere (scripts/seed_customer_link_translations.py family); this file must never re-seed it.
SEEDED_ELSEWHERE = {"ui.prepayments.linked_accounts"}

_PREPAYMENT_LITERAL = re.compile(r"""['"](ui\.prepayments\.[a-z_]+)['"]""")

# The blocking codes the preview can emit (Tasks 2, 4, 7) and the undo codes (Task 5), in the JS Maps' order.
BLOCK_CODES = (
    "no_free_balance",
    "amount_exceeds_free_balance",
    "target_not_below_free_balance",
    "driver_not_eligible",
    "no_active_session",
    "amount_exceeds_driver_cash",
    "paid_before_current_trip",
    "ambiguous_contract_mirror",
    "units_contract_mirror",
    "contract_account_missing",
)
UNDO_CODES = ("already_undone", "funding_event_voided", "session_checked")

_MAP_ENTRY = re.compile(r"""\[\s*['"]([a-z][a-z_]+)['"],\s*\[\s*['"]ui\.prepayments\.""")


def _ui_sources():
    return [(REPO_ROOT / "admin_ui" / rel).read_text(encoding="utf-8") for rel in UI_FILES]


def _fallbacks():
    """key -> every inline English fallback the UI passes for it. A set, so drift shows up."""
    found = defaultdict(set)
    for text in _ui_sources():
        for _, key, expr in _JS_PAIR.findall(text):
            found[key].add("".join(_unquote_js(lit) for lit in re.findall(_JS_STRING, expr, re.S)))
    return found


def _map_codes(name):
    src = (REPO_ROOT / "admin_ui/src/utils/cashScopeDisplay.js").read_text(encoding="utf-8")
    body = src.split(f"export const {name} = new Map([", 1)[1].split("]);", 1)[0] + "]);"
    return tuple(re.findall(_MAP_ENTRY, body))


@pytest.mark.unit
@pytest.mark.parametrize("key", ALL_KEYS)
def test_key_is_seeded_in_three_languages_under_the_ui_category(key):
    row = PREPAYMENT_TRANSLATIONS.get(key)
    assert row is not None, f"{key} is missing from PREPAYMENT_TRANSLATIONS"
    for lang in ("en", "uz", "ru"):
        assert isinstance(row[lang], str) and row[lang].strip(), (key, lang)
    assert key.split(".")[0] == "ui"  # the seed's main() files a row under this category


@pytest.mark.unit
@pytest.mark.parametrize("key", ALL_KEYS)
def test_seeded_english_is_the_call_sites_fallback(key):
    assert _fallbacks()[key] == {PREPAYMENT_TRANSLATIONS[key]["en"]}, key


@pytest.mark.unit
@pytest.mark.parametrize("key", ALL_KEYS)
def test_translations_keep_every_interpolation_token(key):
    row = PREPAYMENT_TRANSLATIONS[key]
    tokens = _i18next_placeholders(row["en"])
    assert _i18next_placeholders(row["uz"]) == tokens, key
    assert _i18next_placeholders(row["ru"]) == tokens, key


@pytest.mark.unit
def test_every_prepayment_key_the_ui_renders_is_seeded_and_the_new_ones_are_all_used():
    used = {key for text in _ui_sources() for key in _PREPAYMENT_LITERAL.findall(text)}
    assert KEYS <= used, sorted(KEYS - used)
    assert used - SEEDED_ELSEWHERE <= set(PREPAYMENT_TRANSLATIONS), sorted(used - SEEDED_ELSEWHERE - set(PREPAYMENT_TRANSLATIONS))


@pytest.mark.unit
def test_linked_accounts_is_not_reseeded_here():
    assert not SEEDED_ELSEWHERE & set(PREPAYMENT_TRANSLATIONS)


@pytest.mark.unit
def test_the_copy_maps_cover_every_code_the_backend_raises():
    # A quote style the regex misses fails here, loudly, instead of passing on an empty set.
    assert _map_codes("PAYOUT_BLOCK_COPY") == BLOCK_CODES
    assert _map_codes("UNDO_BLOCK_COPY") == UNDO_CODES
    services = REPO_ROOT / "business_app" / "services"
    payout_src = (services / "prepayment_payout_service.py").read_text(encoding="utf-8")
    contract_src = (services / "corporate_contract_service.py").read_text(encoding="utf-8")
    raised = payout_src + contract_src
    for code in BLOCK_CODES:
        assert re.search(rf"""['"f]{code}: """, raised), f"{code} is not a blocking reason the backend emits"
    for code in UNDO_CODES:
        assert f'"{code}"' in payout_src, f"{code} is not an undo code the backend emits"


@pytest.mark.unit
def test_the_collected_cash_block_code_is_emitted_and_mapped():
    src = (REPO_ROOT / "admin_ui/src/utils/cashScopeDisplay.js").read_text(encoding="utf-8")
    assert "['event_funds_prepayment_return', [\n    'ui.prepayments.correction_blocked_by_return'" in src
    backend = (REPO_ROOT / "business_app/services/order_cash_edit_service.py").read_text(encoding="utf-8")
    assert "event_funds_prepayment_return: " in backend


@pytest.mark.unit
def test_the_status_copy_maps_cover_every_status_the_warnings_can_carry():
    from business_app.services.cash_collection_service import CashCollectionService
    from shared.enums import LIVE_SUBSCRIPTION_STATUSES

    assert set(_map_codes("PAYOUT_ORDER_STATUS_COPY")) == {s.value for s in CashCollectionService.RESERVABLE_ORDER_STATUSES}
    assert set(_map_codes("PAYOUT_SUBSCRIPTION_STATUS_COPY")) == {s.value for s in LIVE_SUBSCRIPTION_STATUSES}
