"""
Sentry PII scrubbing and event pre-processing.

Reuses the `sanitize_request_data` redaction logic from error_handlers.py so
Sentry sees the same SENSITIVE_KEYS redaction policy that structured logs do.
`send_default_pii=False` on init already scrubs request headers/cookies at the
SDK level; this hook catches anything that ends up in event extras, breadcrumbs,
or exception frame locals. A URL's password has no key, so `before_send` ends
with `shared.url_redaction.redact_event_urls`, which masks it in every string of
the event; both bots apply the same function.

Transactions (performance traces) never pass through `before_send`, but the SDK
puts the request's raw query string on them too, so `before_send_transaction`
applies the same request redaction. Both are wired in config/production.py and
config/staging.py.
"""

from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlsplit, urlunsplit

from business_app.utils.error_handlers import sanitize_request_data
from shared.telegram_login import TELEGRAM_LOGIN_PATH, loggable_login_url
from shared.url_redaction import redact_event_urls


_SENSITIVE_SUBSTRINGS = (
    "password",
    "token",
    "secret",
    "authorization",
    "credit_card",
    "card_number",
    "cvv",
    "pin",
    "ssn",
    "passport",
    "api_key",
    "access_token",
    "refresh_token",
)


def _scrub_value(value: Any) -> Any:
    if isinstance(value, dict):
        return sanitize_request_data(value)
    if isinstance(value, list):
        return [_scrub_value(v) for v in value]
    return value


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
    """Redact sensitive keys from event payloads before they leave the process, then every URL's password
    (`redact_event_urls`, the one rule the bots apply too)."""
    for section in ("extra", "contexts", "tags"):
        if section in event and isinstance(event[section], dict):
            event[section] = _scrub_value(event[section])

    request = event.get("request") or {}
    _redact_sign_in_request(request)
    for key in ("data", "query_string", "headers", "cookies"):
        if key in request and isinstance(request[key], dict):
            request[key] = sanitize_request_data(request[key])

    exception = event.get("exception") or {}
    for entry in exception.get("values", []) or []:
        stacktrace = (entry or {}).get("stacktrace") or {}
        for frame in stacktrace.get("frames", []) or []:
            if isinstance(frame.get("vars"), dict):
                frame["vars"] = sanitize_request_data(frame["vars"])

    breadcrumbs = (event.get("breadcrumbs") or {}).get("values") or []
    for crumb in breadcrumbs:
        if isinstance(crumb.get("data"), dict):
            crumb["data"] = sanitize_request_data(crumb["data"])

    return redact_event_urls(event)


def before_send_transaction(event: Dict[str, Any], hint: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """A transaction's request, redacted as `before_send` redacts an error's: the SDK writes the raw query string on
    every event it sends, a traced sign-in included."""
    request = event.get("request")
    if isinstance(request, dict):
        _redact_sign_in_request(request)
    return event
