"""Every audited refusal resolves to its OWN curated copy in en/uz/ru.

Each task appends its rows to AUDIT. A row is (error_code, expected key). The
test proves the map points there, that the key is curated in all three
languages (so /health and the driver see real words), that the three languages
differ (a copy-paste of English is not a translation), and that the copy is not
one of the generic sentences this audit exists to replace.
"""
import importlib.util
from pathlib import Path

import pytest

from staff_bot.handlers.base import BaseHandler
from staff_bot.utils import auth_refusals

_SEED_PATH = Path(__file__).resolve().parents[2] / "scripts" / "seed_staff_translations.py"
_spec = importlib.util.spec_from_file_location("seed_staff_translations_audit", _SEED_PATH)
_SEED = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_SEED)

GENERIC_KEYS = {
    "staff.error.api.validation", "staff.error.api.conflict", "staff.error.api.invalid_input",
    "staff.error.api.not_found", "staff.error.api.forbidden", "staff.error.api.unexpected",
}

AUDIT: list[tuple[str, str]] = [
    # Task 6: delivery, pool, re-dispatch and "preparing".
    ("STAFF_DELIVERY_NOT_FOUND", "staff.error.api.delivery_not_found"),
    ("STAFF_DRIVER_NOT_FOUND", "staff.error.api.driver_profile_missing"),
    ("ORDER_STATUS_CONFLICT", "staff.error.api.order_changed_concurrently"),
    ("ORDER_STATUS_TRANSITION_INVALID", "staff.error.api.order_status_changed"),
    ("INVENTORY_CONFIRMATION_FAILED", "staff.error.api.inventory_confirmation_failed"),
    ("CONTRACT_AMOUNT_MODE_AMBIGUOUS", "staff.error.api.contract_needs_admin"),
    ("STAFF_DELIVERY_NOT_REDISPATCHABLE", "staff.error.api.not_redispatchable"),
    ("ORDER_NOT_RESCHEDULABLE", "staff.error.api.order_closed_for_redispatch"),
    ("DELIVERY_NOT_RESCHEDULABLE", "staff.error.api.order_closed_for_redispatch"),
    ("ORDER_RESCHEDULE_PAST_CONTRACT_END", "staff.error.api.past_contract_end"),
    ("STAFF_ORDER_STATUS_INVALID_FOR_PREPARING", "staff.error.api.not_preparable"),
    ("STAFF_ORDER_NOT_FOUND", "staff.error.api.order_not_found"),
    ("STAFF_DELIVERY_NOT_CLAIMABLE", "staff.error.api.delivery_not_claimable"),
    # Task 7: bottle sessions, transfers, collections.
    ("BOTTLE_SESSION_NOT_FOUND", "staff.error.api.no_open_bottle_session"),
    ("BOTTLE_SESSION_TARGET_NOT_FOUND", "staff.error.api.bottle_session_gone"),
    ("BOTTLE_SESSION_NOT_OPEN", "staff.error.api.bottle_session_closed"),
    ("BOTTLE_SESSION_MEMBERSHIP_CLOSED", "staff.error.api.joined_session_closed"),
    ("BOTTLE_SESSION_REQUIRED_TO_INVITE", "staff.error.api.open_session_to_invite"),
    ("BOTTLE_SESSION_REQUIRED_TO_RECEIVE", "staff.error.api.open_session_to_receive"),
    ("BOTTLE_SESSION_ALREADY_OPEN", "staff.error.api.close_own_session_to_join"),
    ("BOTTLE_INVITEE_HAS_SESSION", "staff.error.api.invitee_has_session"),
    ("BOTTLE_INVITEE_IN_OTHER_SESSION", "staff.error.api.invitee_in_other_session"),
    ("BOTTLE_SESSION_MEMBERSHIP_ALREADY_ACTIVE", "staff.error.api.already_in_session"),
    ("BOTTLE_SESSION_MEMBERSHIP_NOT_FOUND", "staff.error.api.not_in_session"),
    ("BOTTLE_SCOPE_MEMBERSHIP_REQUIRED", "staff.error.api.customer_left_address"),
    ("BOTTLE_IDEMPOTENCY_KEY_REUSED", "staff.error.api.entry_already_submitted"),
    ("BOTTLE_TRANSFER_EXCEEDS_INVENTORY", "staff.error.api.transfer_exceeds_inventory"),
    ("BOTTLE_TRANSFER_NOT_FOUND", "staff.error.api.transfer_not_found"),
    ("BOTTLE_TRANSFER_NOT_RECEIVER", "staff.error.api.transfer_not_yours"),
    ("BOTTLE_TRANSFER_NOT_PENDING", "staff.error.api.transfer_already_handled"),
    # Task 8: cash and reconciliation.
    ("RECONCILIATION_AMOUNT_NOT_POSITIVE", "staff.error.api.handoff_amount_not_positive"),
    ("RECONCILIATION_NOTHING_TO_SUBMIT", "staff.error.api.nothing_to_reconcile"),
    ("COD_CUSTOMER_NOT_FOUND", "staff.error.api.customer_not_found"),
    # Task 9: operator flows. STAFF_PHONE_INVALID / STAFF_PHONE_EXISTS point at
    # operator keys that predate this audit, so they have no row here.
    ("STAFF_SEARCH_QUERY_TOO_SHORT", "staff.error.api.search_too_short"),
    ("STAFF_INVALID_DELIVERY_ADDRESS", "staff.error.api.address_not_customers"),
    ("STAFF_CLIENT_NOT_FOUND", "staff.error.api.customer_not_found"),
    ("STAFF_USER_NOT_FOUND", "staff.error.api.customer_not_found"),
    ("STAFF_PRODUCT_NOT_FOUND", "staff.error.api.product_unavailable"),
    ("ORDER_DELIVERY_ADDRESS_REQUIRED", "staff.error.api.address_required"),
    # Task 10: login and account refusals.
    ("STAFF_ACCOUNT_INACTIVE", "staff.error.api.account_inactive"),
    ("STAFF_TELEGRAM_ALREADY_LINKED", "staff.error.api.telegram_already_linked"),
    # Task 11: sales agents and driver try-outs.
    ("SALES_OUTLET_NOT_FOUND", "staff.error.api.outlet_not_found"),
    ("SALES_OUTLET_NOT_ASSIGNED", "staff.error.api.outlet_not_assigned"),
    ("SALES_VISIT_NOT_FOUND", "staff.error.api.visit_not_found"),
    ("SALES_VISIT_NOT_OWNED", "staff.error.api.visit_not_owned"),
    ("SALES_PAYMENT_METHOD_INVALID", "staff.error.api.agent_payment_method"),
    ("SALES_STATS_PERIOD_INVALID", "staff.error.api.stats_period_invalid"),
    ("ORDER_MIN_AMOUNT", "staff.error.api.order_min_amount"),
    ("ORDER_STOCK_UNAVAILABLE", "staff.error.api.stock_unavailable"),
    ("TRYOUT_TASK_NOT_FOUND", "staff.error.api.tryout_task_not_found"),
    ("TRYOUT_NOT_FOUND", "staff.error.api.tryout_not_found"),
    ("TRYOUT_TASK_TAKEN", "staff.error.api.tryout_task_taken"),
    ("TRYOUT_TASK_COMPLETED", "staff.error.api.tryout_task_completed"),
    ("TRYOUT_PICKUP_EXCEEDS_OUTSTANDING", "staff.error.api.tryout_pickup_exceeds"),
    ("TRYOUT_PHONE_INVALID", "staff.error.api.tryout_phone_invalid"),
    ("TRYOUT_PRODUCT_UNAVAILABLE", "staff.error.api.tryout_product_unavailable"),
]


@pytest.mark.parametrize("error_code, key", AUDIT)
def test_the_code_resolves_to_its_own_curated_copy(error_code, key):
    assert BaseHandler.API_ERROR_CODE_KEY_MAP.get(error_code) == key
    assert key not in GENERIC_KEYS
    values = {lang: _SEED._curated_value(key, lang) for lang in ("en", "uz", "ru")}
    assert all(values.values()), f"{key} is not curated in every language: {values}"
    assert len(set(values.values())) == 3, f"{key} repeats a language verbatim: {values}"
    generic_en = {_SEED._curated_value(g, "en") for g in GENERIC_KEYS}
    assert values["en"] not in generic_en


@pytest.mark.parametrize("error_code", sorted(auth_refusals.ACCOUNT_REFUSAL_CODES))
def test_a_remembered_account_refusal_reads_the_maps_curated_copy(error_code):
    """`auth_refusals` decides only WHICH refusals re-logging in cannot fix;
    the sentence is the map's. A code it remembers must have a map entry
    curated in every language, and the silent re-login path must resolve to
    exactly that key — so repointing the map moves every path at once."""
    key = BaseHandler.API_ERROR_CODE_KEY_MAP.get(error_code)
    assert key, f"{error_code} is remembered by auth_refusals but has no API_ERROR_CODE_KEY_MAP entry"
    values = {lang: _SEED._curated_value(key, lang) for lang in ("en", "uz", "ru")}
    assert all(values.values()), f"{key} is not curated in every language: {values}"

    telegram_user_id = 4242
    auth_refusals.reset()
    try:
        auth_refusals.remember(telegram_user_id, error_code)
        assert auth_refusals.session_lost_key(telegram_user_id) == key
    finally:
        auth_refusals.reset()
