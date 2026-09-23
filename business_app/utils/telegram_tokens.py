"""The staff bot's token, as the backend finds it.

Two names are in use for the one secret (`STAFF_BOT_TOKEN` in compose, `STAFF_TELEGRAM_BOT_TOKEN`
in `.env`), and either may sit in the Flask config or only in the environment. This is the one
place that knows the lookup order, so notification sends and the visit-photo proxy (D27) can never
disagree about which bot they are talking to.
"""

import os
from typing import Optional

from flask import current_app

_STAFF_TOKEN_NAMES = ("STAFF_BOT_TOKEN", "STAFF_TELEGRAM_BOT_TOKEN")


def get_staff_bot_token() -> Optional[str]:
    for name in _STAFF_TOKEN_NAMES:
        value = current_app.config.get(name)
        if value:
            return value
    for name in _STAFF_TOKEN_NAMES:
        value = os.environ.get(name)
        if value:
            return value
    return None
