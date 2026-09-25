"""Why this Telegram user's last re-authentication was refused, if it was.

`_authenticate_staff_session` runs silently inside `_get_auth_token`; when the
backend refuses because the ACCOUNT is off (deactivated / inactive), the
driver used to be told "session expired" and to log in again — which fails
the same way. `_handle_auth_error`, `require_auth` and the text router read
the reason here. Keyed by Telegram user id (there is no context in
`_handle_auth_error`'s 84 call sites); cleared on the next successful login.

This module decides only WHICH refusals are remembered. The sentence each one
reads is `BaseHandler.API_ERROR_CODE_KEY_MAP`'s, the same one every other
refusal of that code shows — repoint the map and every path moves with it.
"""
from typing import Dict, FrozenSet, Optional

# Refusals that re-logging in cannot fix.
ACCOUNT_REFUSAL_CODES: FrozenSet[str] = frozenset({
    'STAFF_ACCOUNT_DEACTIVATED',
    'STAFF_ACCOUNT_INACTIVE',
})

SESSION_EXPIRED_KEY = 'staff.session_expired'

_refusals: Dict[int, str] = {}


def remember(telegram_user_id: int, error_code: Optional[str]) -> None:
    if error_code in ACCOUNT_REFUSAL_CODES:
        _refusals[telegram_user_id] = error_code


def forget(telegram_user_id: int) -> None:
    _refusals.pop(telegram_user_id, None)


def refusal_key(telegram_user_id: int) -> Optional[str]:
    code = _refusals.get(telegram_user_id)
    if not code:
        return None
    # Function-level: handlers/base.py imports this module.
    from staff_bot.handlers.base import BaseHandler

    return BaseHandler.API_ERROR_CODE_KEY_MAP[code]


def session_lost_key(telegram_user_id: Optional[int]) -> str:
    """The copy for "your session is gone and was not re-established".

    The account refusal when the backend gave one, otherwise "session
    expired". Every place that says it asks here — `_handle_auth_error`,
    `require_auth` and the reply-keyboard text router — so they cannot
    disagree about which sentence a switched-off account reads.
    """
    return refusal_key(telegram_user_id) or SESSION_EXPIRED_KEY


def reset() -> None:
    """Test isolation (the harness calls this)."""
    _refusals.clear()
