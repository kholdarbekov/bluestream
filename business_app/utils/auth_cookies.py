"""The one writer of a website login's cookies (spec 2026-10-27 §9 "One cookie writer"; step 4 Task 16a).

Every door that signs a customer in on the website writes its token pair through here: the password login, the two
registrations and "Open on website" (the Telegram login button). `set_access_cookies` and `set_refresh_cookies` also
write each token's readable CSRF half, `csrf_access_token` and `csrf_refresh_token`. Customer page auth renews an
expired access cookie only when the readable `csrf_refresh_token` matches the refresh token
(`page_auth._renewed_access_token`), so a login written without it would never be renewed quietly.
"""

from typing import Mapping

from flask import Response
from flask_jwt_extended import set_access_cookies, set_refresh_cookies


def set_login_cookies(response: Response, tokens: Mapping[str, str]) -> None:
    """The login's access and refresh cookies, each with its readable CSRF half, on `response`."""
    set_access_cookies(response, tokens["access_token"])
    set_refresh_cookies(response, tokens["refresh_token"])
