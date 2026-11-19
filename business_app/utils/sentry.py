"""
What an error event or a performance trace may carry out of the process.

`before_send` (errors) and `before_send_transaction` (traces) apply the rule every process that reports to Sentry
applies, `shared.secret_scrub.scrub_event`: a secret is hidden by its name, by its value wherever it travels and by its
shape (a JWT, a `Bearer` value, a secret `name=value` pair, a token at the end of a Redis key or a link's path, a URL's
password). The backend adds what only a backend request carries: a Telegram sign-in link's query
(`_redact_sign_in_request`).

`send_default_pii=False` on init already drops request headers/cookies at the SDK level. Both hooks are wired in
config/production.py and config/staging.py.
"""

from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlsplit, urlunsplit

from shared.secret_scrub import scrub_event
from shared.telegram_login import TELEGRAM_LOGIN_PATH, loggable_login_url


def _redact_sign_in_request(request: Dict[str, Any]) -> None:
    """A sign-in link's query carries its hash and the customer's names (spec 2026-10-27 §9): its URL keeps the
    Telegram id only, read from the parsed query, and the query string goes."""
    url = request.get("url")
    if not isinstance(url, str):
        return
    parts = urlsplit(url)
    if parts.path != TELEGRAM_LOGIN_PATH:
        return
    query = request.get("query_string")
    query = query if isinstance(query, str) and query else parts.query
    telegram_id = (parse_qs(query).get("id") or [None])[0]
    request["url"] = loggable_login_url(urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")), telegram_id)
    request["query_string"] = ""


def before_send(event: Dict[str, Any], hint: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """An error event, scrubbed (`shared.secret_scrub.scrub_event`)."""
    if isinstance(event.get("request"), dict):
        _redact_sign_in_request(event["request"])
    return scrub_event(event, hint)


def before_send_transaction(event: Dict[str, Any], hint: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """A performance trace, scrubbed as an error is: its spans name Redis keys, and the SDK writes the request on it
    too, its body and raw query string included. With `attach_stacktrace` the SDK also adds the stack of whatever code
    closed the trace, every caller's locals with it (the WSGI environ, a test client's body): that says nothing about
    the trace, and it goes."""
    event.pop("threads", None)
    return before_send(event, hint)
