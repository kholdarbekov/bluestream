"""The "Open on website" sign-in's shared words (spec 2026-10-27 §9, §17 Q5; step 4 Task 16a).

The customer bot's "Open on website" button is a Telegram login button (`login_url`). Its URL is
`https://<site>/auth/telegram-login?next=<target>`; at the tap Telegram opens it with the customer's identity and a
signed `hash` appended, and the website's route checks them (`business_app/services/telegram_login_service.py`).
The bot builds the URL from here (`login_url`) and the route checks `next` against the same allowlist
(`is_allowed_login_target`), so the two cannot offer and accept different pages. The log redaction lives here too,
beside the path it redacts: by structure for a request (`loggable_request_url`), by pattern for free text
(`redact_login_query`), and by that pattern value by value for a JSON line (`loggable_json`).

The bot imports this module, and its image ships shared/ and telegram_bot/ only, so it uses the standard library and
shared/ alone, never business_app (tests/unit/test_shared_modules_load_without_the_backend.py).
"""

import json
import re
from typing import Any, Optional
from urllib.parse import parse_qs, urlencode

from shared.safe_paths import safe_next_path

TELEGRAM_LOGIN_PATH = "/auth/telegram-login"

# The fields Telegram signs on a login button's URL; the hash covers exactly the ones present.
TELEGRAM_LOGIN_FIELDS = ("auth_date", "first_name", "id", "last_name", "photo_url", "username")

# Where a sign-in may land: the pages "Open on website" opens, plus one subscription (`subscription_target`).
LOGIN_TARGETS = ("/my-subscriptions", "/my-orders", "/my-account")
_SUBSCRIPTION_TARGET = re.compile(r"/my-subscriptions\?sub=[1-9][0-9]{0,9}")

# The sign-in path and its query in free text (a raw request line, a Referer): up to the next whitespace. A raw URL is
# percent-encoded, so a name's quote or space cannot end it early; a quote that closes the text is swallowed with it.
_LOGIN_QUERY = re.compile(re.escape(TELEGRAM_LOGIN_PATH) + r"\?(\S*)")
_TELEGRAM_ID = re.compile(r"[0-9]{1,20}")

# Set on a log record (`extra={LOGIN_QUERY_REDACTED: True}`) whose every string already passed `redact_login_query` on
# its own, a `loggable_json` line. The log filter's pattern pass over a whole JSON line would read a value's closing
# quote and comma as part of a sign-in URL's query, so the line would stop being JSON and lose the id (step-4 live drive
# D3): the filter skips that pass for such a record.
LOGIN_QUERY_REDACTED = "login_query_redacted"


def subscription_target(subscription_id: int) -> str:
    """The sign-in target that opens one subscription on the website."""
    return f"/my-subscriptions?sub={int(subscription_id)}"


def is_allowed_login_target(value: Optional[str]) -> bool:
    """Whether a sign-in may land on `value`: an extra allowlist applied on top of the one "send me back to" rule.

    `value` must first pass `safe_next_path` (a path on this site), then be exactly one of `LOGIN_TARGETS` or
    `/my-subscriptions?sub=<id>`. A sign-in hands out a login, so it lands on these pages only."""
    page = safe_next_path(value)
    if page is None:
        return False
    return page in LOGIN_TARGETS or _SUBSCRIPTION_TARGET.fullmatch(page) is not None


def login_url(site_base: str, target: str) -> str:
    """The "Open on website" button's `login_url`: the website's sign-in page with `next` set to `target`.

    Raises ValueError for a target outside the allowlist, which the website would refuse."""
    if not is_allowed_login_target(target):
        raise ValueError(f"not a sign-in target: {target!r}")
    return f"{site_base.rstrip('/')}{TELEGRAM_LOGIN_PATH}?{urlencode({'next': target})}"


def loggable_login_url(base_url: str, telegram_id: Optional[str]) -> str:
    """A sign-in URL as a log may keep it: `base_url` (no query) plus `?id=<id>` when `telegram_id` is a number, else
    `base_url` alone. The hash signs the customer in while it is fresh, and the names are personal data."""
    if telegram_id and _TELEGRAM_ID.fullmatch(telegram_id):
        return f"{base_url}?id={telegram_id}"
    return base_url


def loggable_request_url(request) -> str:
    """A request's URL for a log line or an audit row (a Flask/werkzeug request).

    The sign-in page is redacted by structure: its URL without the query (`base_url`) plus the Telegram id read from
    the parsed query. Never by a pattern over `request.url`, which werkzeug decodes, so a name such as O'tkir, a
    quote or a no-break space would end the match early and leave the hash behind it. Any other page keeps its URL,
    with a sign-in URL in its query redacted (`redact_login_query`)."""
    if request.path == TELEGRAM_LOGIN_PATH:
        return loggable_login_url(request.base_url, request.args.get("id"))
    return redact_login_query(request.url)


def redact_login_query(text: Optional[str]) -> Optional[str]:
    """Free text with every sign-in URL's query cut down to the Telegram id: `…/auth/telegram-login?<query>` becomes
    `…/auth/telegram-login?id=<id>`, or the bare path when the query names no id; anything else passes through
    unchanged. For a request's own URL use `loggable_request_url`."""
    if not text:
        return text

    def _keep_the_id(match: "re.Match[str]") -> str:
        telegram_id = (parse_qs(match.group(1)).get("id") or [""])[0]
        return loggable_login_url(TELEGRAM_LOGIN_PATH, telegram_id)

    return _LOGIN_QUERY.sub(_keep_the_id, text)


def loggable_json(value: Any) -> str:
    """`value` as a JSON log line's text, what `json.dumps(value, default=str)` writes, with every string in it, keys
    included, passed through `redact_login_query` on its own first. Each match then ends where its value ends, so the
    JSON around it is never touched. Log the line with `extra={LOGIN_QUERY_REDACTED: True}`."""

    def _redacted(item: Any) -> Any:
        if isinstance(item, dict):
            return {_redacted(key): _redacted(inner) for key, inner in item.items()}
        if isinstance(item, (list, tuple)):
            return [_redacted(inner) for inner in item]
        if isinstance(item, str):
            return redact_login_query(item)
        if item is None or isinstance(item, (bool, int, float)):
            return item
        return redact_login_query(str(item))

    return json.dumps(_redacted(value))
