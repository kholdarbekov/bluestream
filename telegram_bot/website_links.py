"""Links from the customer bot to the public website.

One place decides which website address the bot may put in a button, so the loyalty guide link and the
"Open on website" sign-in button agree. Telegram refuses a URL button (and a `login_url` button) whose host is
not public, and one refused button fails the whole menu render, so each builder returns None and the caller leaves
the button out. A `login_url` button must also be https (Telegram's rule), hence `require_https`.
"""

import os
from typing import Optional
from urllib.parse import urlparse

from shared.telegram_login import login_url

_NON_PUBLIC_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}


def public_website_base(*, require_https: bool = False) -> Optional[str]:
    """`COMPANY_WEBSITE` without a trailing slash, or None unless Telegram would accept it in a button.

    Accepts an http(s) URL with a public-looking host: not localhost or loopback, and containing a dot (which
    rejects bare hostnames and docker service names such as ``business_app``). `require_https` narrows it to https."""
    base = os.environ.get("COMPANY_WEBSITE", "https://aqua-element.uz").rstrip("/")
    try:
        parsed = urlparse(base)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    schemes = ("https",) if require_https else ("http", "https")
    if parsed.scheme not in schemes or not host or host in _NON_PUBLIC_HOSTS or "." not in host:
        return None
    return base


def open_on_website_url(target: str) -> Optional[str]:
    """The `login_url` of an "Open on website" button that lands on `target`; None unless the site is https and
    public. `target` must be one the website's sign-in accepts (`shared.telegram_login`), else ValueError."""
    base = public_website_base(require_https=True)
    return login_url(base, target) if base else None
