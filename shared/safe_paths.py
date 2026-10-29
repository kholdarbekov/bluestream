"""The one rule for a "send me back to" path (spec 2026-10-27 §4.6, §9; step 4 Task 9).

The website's login page and customer page auth (`business_app/utils/page_auth.py`) use it, and so does the
Telegram sign-in allowlist built on top of it (`shared/telegram_login.py`, step 4 Task 16a), which the customer
bot imports. It therefore lives in shared/ and uses the standard library only: the telegram_bot image ships
shared/ and telegram_bot/ and no business_app/ (Dockerfile, docker-compose.yml), so an import of business_app
from here would stop the bot at start-up while every test, run with the whole repository on its path, stays
green (tests/unit/test_shared_modules_load_without_the_backend.py).
"""

import unicodedata
from typing import Optional

MAX_NEXT_PATH_LENGTH = 2048


def safe_next_path(value: Optional[str]) -> Optional[str]:
    """The one rule for a "send me back to" path: a path on this site, or None.

    At most 2048 characters, starting with `/` (never `//`), with no `\\`, `://`, whitespace or control
    character, so it can never name another host or break out of a header or a URL."""
    if not value or len(value) > MAX_NEXT_PATH_LENGTH:
        return None
    if not value.startswith("/") or value.startswith("//"):
        return None
    if "\\" in value or "://" in value:
        return None
    if any(char.isspace() or unicodedata.category(char) == "Cc" for char in value):
        return None
    return value
