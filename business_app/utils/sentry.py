"""
Sentry PII scrubbing and event pre-processing.

Reuses the `sanitize_request_data` redaction logic from error_handlers.py so
Sentry sees the same SENSITIVE_KEYS redaction policy that structured logs do.
`send_default_pii=False` on init already scrubs request headers/cookies at the
SDK level; this hook catches anything that ends up in event extras, breadcrumbs,
or exception frame locals.
"""

from typing import Any, Dict, Optional

from business_app.utils.error_handlers import sanitize_request_data


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


def before_send(event: Dict[str, Any], hint: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Redact sensitive keys from event payloads before they leave the process."""
    for section in ("extra", "contexts", "tags"):
        if section in event and isinstance(event[section], dict):
            event[section] = _scrub_value(event[section])

    request = event.get("request") or {}
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

    return event
