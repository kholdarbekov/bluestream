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
