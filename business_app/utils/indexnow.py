"""Best-effort IndexNow client (Bing, Yandex): tells search engines which public URLs changed.

The key is the stem of the one 32-character ``.txt`` file in the app's static folder, which nginx serves at the
site root. Every call is best-effort: a switched-off flag, a missing key or a network error is swallowed and
reported through the return value, never raised.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

import requests
from flask import current_app

from business_app.services.customer_links import website_url

INDEXNOW_ENDPOINT = "https://api.indexnow.org/indexnow"
ACCEPTED_STATUSES = frozenset({200, 202})  # 202: the key is still being validated
_KEY_FILE = re.compile(r"[0-9a-f]{32}")


def indexnow_host() -> str:
    """The host IndexNow is told the URLs belong to: the one in `COMPANY_WEBSITE`."""
    return urlsplit(website_url("/")).netloc


def get_key() -> str | None:
    """The stem of the one key file whose content equals its own name; None (and one warning) otherwise."""
    keys = [
        path.stem
        for path in Path(current_app.static_folder).glob("*.txt")
        if _KEY_FILE.fullmatch(path.stem) and path.read_text(encoding="utf-8").strip() == path.stem
    ]
    if len(keys) != 1:
        current_app.logger.warning("indexnow: expected exactly one key file, found %d", len(keys))
        return None
    return keys[0]


def submit_urls(urls: list[str]) -> int | None:
    """Submit `urls` and return the HTTP status (202: key being validated, 403: key not reachable, 422: URL not
    on this host), or None when nothing was sent. Never raises."""
    if not current_app.config.get("INDEXNOW_ENABLED") or not urls:
        return None
    key = get_key()
    if key is None:
        return None
    try:
        response = requests.post(
            INDEXNOW_ENDPOINT,
            json={"host": indexnow_host(), "key": key, "urlList": list(urls)},
            timeout=10,
        )
    except Exception as exc:
        current_app.logger.warning("indexnow: submission failed (%s)", type(exc).__name__)
        return None
    current_app.logger.info("indexnow: HTTP %s for %d URLs", response.status_code, len(urls))
    return response.status_code
