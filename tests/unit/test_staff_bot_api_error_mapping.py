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
