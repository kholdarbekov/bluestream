"""A bot signs its user in again when the backend refuses a cached token (spec 2026-10-19 R9).

A password reset, a role change and the phone-link merge end every login of an account at
once. The backend then refuses the bot's cached access token on its very next request, although the
token is still valid by its own clock. Both bots recover the same way: drop the refused pair, sign
the owner in again through the bot's signed login, cache the new pair and repeat the call once.

This module is the part both bots share: whose a handed-out token is (`TokenOwners`, kept by each
bot's TokenManager) and the drop / sign-in / cache sequence (`sign_in_again`). Each bot brings its
own signed login: `/auth/telegram-login` (customer bot) or `/staff/auth/login` (staff bot).
"""

import logging
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)


class TokenOwners:
    """Access token -> the Telegram user it was handed out for, in process, oldest dropped first."""

    # One entry per recent user; far more than either bot has active at once.
    MAX_ENTRIES = 10_000

    def __init__(self, max_entries: int = MAX_ENTRIES):
        self._owners: Dict[str, int] = {}
        self._max_entries = max_entries

    def remember(self, access_token: str, telegram_id: int) -> None:
        self._owners.pop(access_token, None)
        self._owners[access_token] = telegram_id
        if len(self._owners) > self._max_entries:
            del self._owners[next(iter(self._owners))]

    def owner_of(self, access_token: str) -> Optional[int]:
        return self._owners.get(access_token)


async def sign_in_again(
    token_manager,
    refused_token: str,
    sign_in: Callable[[int], Awaitable[Optional[Dict[str, Any]]]],
) -> Optional[str]:
    """Replace a refused access token and return the new one.

    `sign_in(telegram_id)` is the bot's signed login, answering `{access_token, refresh_token,
    expires_in}` or None. Returns None, and changes nothing, when the token is not one the
    TokenManager handed out (a refresh token, or no TokenManager attached). Returns None, after
    dropping the refused pair, when the login fails.
    """
    telegram_id = token_manager.owner_of(refused_token) if token_manager else None
    if telegram_id is None:
        return None
    logger.info(f"Token refused for user {telegram_id}, signing in again")
    await token_manager.invalidate_tokens(telegram_id)
    tokens = await sign_in(telegram_id)
    if not tokens or not tokens.get("access_token") or not tokens.get("refresh_token"):
        return None
    await token_manager.store_tokens(
        telegram_id, tokens["access_token"], tokens["refresh_token"], tokens.get("expires_in", 3600)
    )
    return tokens["access_token"]
