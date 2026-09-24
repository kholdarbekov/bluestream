"""Guard the staff bot's API error code → i18n key map.

Regression: backend cash-collection paths raise ``COD_DRIVER_BLOCKED`` and
``COD_DEBT_LIMIT_REACHED`` error codes, but the bot's
``API_ERROR_CODE_KEY_MAP`` only had ``STAFF_DRIVER_COD_BLOCKED``. Unmapped
codes fell through to the generic 400 handler and surfaced as
"Please check the entered data and try again." with no useful guidance.
"""

import pytest

from staff_bot.handlers.base import BaseHandler


@pytest.mark.parametrize(
    "error_code, expected_key",
    [
        ("COD_DRIVER_BLOCKED", "staff.error.api.driver_cod_blocked"),
        ("STAFF_DRIVER_COD_BLOCKED", "staff.error.api.driver_cod_blocked"),
        ("COD_DEBT_LIMIT_REACHED", "staff.error.api.cod_debt_limit_reached"),
    ],
)
def test_cod_error_codes_map_to_specific_i18n_keys(error_code, expected_key):
    assert BaseHandler.API_ERROR_CODE_KEY_MAP.get(error_code) == expected_key


def test_unknown_400_still_falls_back_to_generic_validation_key():
    """Sanity check: unmapped codes should NOT be silently mapped — they
    legitimately fall through to the status-code-based generic handler."""
    assert "TOTALLY_MADE_UP_CODE" not in BaseHandler.API_ERROR_CODE_KEY_MAP


def test_account_deactivated_maps_to_specific_i18n_key():
    assert (
        BaseHandler.API_ERROR_CODE_KEY_MAP.get("STAFF_ACCOUNT_DEACTIVATED")
        == "staff.error.api.account_deactivated"
    )


# --------------------------------------------------------------------------- #
# Place-scope lock timeout (M2): the driver-facing rendering of a bounded wait
# --------------------------------------------------------------------------- #


def test_scope_lock_timeout_maps_to_its_own_retryable_key():
    """``BOTTLE_SCOPE_LOCK_TIMEOUT`` must not fall through to a generic message.

    The backend raises it as a 409. Without this entry the driver would get
    ``staff.error.api.conflict`` ("this action cannot be completed because of a
    conflict"), which reads as a PERMANENT refusal — the driver stops trying,
    at a customer's door, for a condition that clears in seconds.
    """
    assert (
        BaseHandler.API_ERROR_CODE_KEY_MAP.get("BOTTLE_SCOPE_LOCK_TIMEOUT")
        == "staff.error.api.scope_busy"
    )


def test_scope_busy_copy_is_seeded_in_all_three_languages():
    """DB-backed i18n: an unseeded key renders as the bare key to the driver."""
    from scripts.seed_staff_translations import STAFF_TRANSLATIONS as TRANSLATIONS

    entry = TRANSLATIONS.get("staff.error.api.scope_busy")
    assert entry is not None, "staff.error.api.scope_busy is mapped but never seeded"
    assert set(entry) >= {"en", "uz", "ru"}
    for lang, text in entry.items():
        assert text.strip(), f"empty {lang} copy for staff.error.api.scope_busy"


def test_scope_busy_copy_tells_the_driver_it_is_safe_to_retry():
    """The whole point of a distinct key: say NOTHING WAS SAVED, and say RETRY.

    A driver holding bottles at a door needs both facts — that the submission
    did not land, and that pressing again shortly will work.
    """
    from scripts.seed_staff_translations import STAFF_TRANSLATIONS as TRANSLATIONS

    english = TRANSLATIONS["staff.error.api.scope_busy"]["en"].lower()
    assert "try again" in english
    assert "nothing was saved" in english


def test_scope_lock_timeout_resolves_ahead_of_the_status_code_fallback(monkeypatch):
    """error_code wins over status_code in ``_resolve_api_error_message``.

    Pins the ordering the fix depends on: even arriving as a 409, the specific
    key is chosen, not ``staff.error.api.conflict``.
    """
    from staff_bot.handlers import base as base_mod

    monkeypatch.setattr(base_mod.i18n, "get", lambda key, language, **kw: key)

    resolved = BaseHandler._resolve_api_error_message(
        BaseHandler.__new__(BaseHandler),
        "uz",
        error="Conflict",
        status_code=409,
        error_code="BOTTLE_SCOPE_LOCK_TIMEOUT",
    )
    assert resolved == "staff.error.api.scope_busy"


# --------------------------------------------------------------------------- #
# A claim on a delivery that stopped being claimable (reschedule spec, R3/R22)
# --------------------------------------------------------------------------- #


def test_a_claim_on_a_no_longer_claimable_delivery_maps_to_its_own_key():
    """STAFF_DELIVERY_NOT_CLAIMABLE is a 400. Unmapped, it fell through to
    `staff.error.api.validation`, "Please check the entered data", shown to a
    driver who typed nothing. It is not `already_taken` either: nobody took the
    order. Dispatch moved it to another day, or it failed or was cancelled."""
    assert (
        BaseHandler.API_ERROR_CODE_KEY_MAP.get("STAFF_DELIVERY_NOT_CLAIMABLE")
        == "staff.error.api.delivery_not_claimable"
    )


def test_delivery_not_claimable_copy_is_seeded_in_all_three_languages():
    from scripts.seed_staff_translations import STAFF_TRANSLATIONS as TRANSLATIONS

    assert TRANSLATIONS.get("staff.error.api.delivery_not_claimable") == {
        "en": "This order is no longer available.",
        "uz": "Bu buyurtma endi mavjud emas.",
        "ru": "Этот заказ больше недоступен.",
    }


@pytest.mark.parametrize(
    "error_code",
    [
        "ORDER_NOT_RESCHEDULABLE",
        "DELIVERY_NOT_RESCHEDULABLE",
        "STAFF_DELIVERY_NOT_REDISPATCHABLE",
        "ORDER_RESCHEDULE_PAST_CONTRACT_END",
    ],
)
def test_redispatch_refusals_read_as_a_conflict_not_as_bad_input(error_code):
    """Re-dispatch is a reschedule to today (R18), and its refusals are 400s.

    Unmapped, a 400 falls through to ``staff.error.api.validation`` ("check the
    entered data") for an operator who entered nothing: the row changed under
    their card. The old cancelled-order code was a 409 and read as the conflict
    sentence, and these codes keep it.
    """
    assert BaseHandler.API_ERROR_CODE_KEY_MAP.get(error_code) == "staff.error.api.conflict"


def test_not_owned_maps_to_its_own_key():
    """R20: the delivery a driver's card acts on was rescheduled, reassigned or
    pooled after the card was drawn. The generic 409 copy ("cannot be completed
    because of a conflict") would not tell them the order is no longer theirs."""
    assert (
        BaseHandler.API_ERROR_CODE_KEY_MAP.get("STAFF_DELIVERY_NOT_OWNED")
        == "staff.error.api.delivery_not_owned"
    )


def test_not_owned_without_a_context_still_alerts_in_its_own_words(monkeypatch):
    """Only the three delivery-status call sites pass ``context``. Any other
    screen that met the code gets the alert, in the mapped sentence. It does not
    get a stale-card redraw for state it never held."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from staff_bot.handlers import base as base_mod

    monkeypatch.setattr(base_mod.i18n, "get", lambda key, language, **kw: key)
    handler = BaseHandler.__new__(BaseHandler)
    update = MagicMock()
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    response = MagicMock(
        success=False,
        error="This order is no longer assigned to you",
        status_code=409,
        error_code="STAFF_DELIVERY_NOT_OWNED",
    )

    asyncio.run(handler._handle_api_response_error(update, response, "uz"))

    update.callback_query.answer.assert_awaited_once_with(
        "❌ staff.error.api.delivery_not_owned", show_alert=True
    )
    update.callback_query.edit_message_text.assert_not_called()
