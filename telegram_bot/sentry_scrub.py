"""Sentry PII scrubbing for the Telegram bot process.

Mirrors business_app/utils/sentry.py but standalone to keep the bot container
free of backend imports. Scrubs the same SENSITIVE_KEYS plus Telegram-specific
identifiers (phone, username, telegram_id) that are PII in our threat model.
"""
from typing import Any, Dict, Optional


_SENSITIVE_SUBSTRINGS = (
    'password', 'token', 'secret', 'authorization',
    'credit_card', 'card_number', 'cvv', 'pin', 'ssn',
    'passport', 'api_key', 'access_token', 'refresh_token',
    'phone', 'telegram_id', 'chat_id', 'username',
)

_REDACTED = '[REDACTED]'


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: _REDACTED if _is_sensitive(k) else _scrub(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def _is_sensitive(key: str) -> bool:
    key_lower = key.lower()
    return any(sub in key_lower for sub in _SENSITIVE_SUBSTRINGS)


def before_send(event: Dict[str, Any], hint: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    for section in ('extra', 'contexts', 'tags'):
        if section in event and isinstance(event[section], dict):
            event[section] = _scrub(event[section])

    request = event.get('request') or {}
    for key in ('data', 'query_string', 'headers', 'cookies'):
        if key in request and isinstance(request[key], dict):
            request[key] = _scrub(request[key])

    exception = event.get('exception') or {}
    for entry in exception.get('values', []) or []:
        stacktrace = (entry or {}).get('stacktrace') or {}
        for frame in stacktrace.get('frames', []) or []:
            if isinstance(frame.get('vars'), dict):
                frame['vars'] = _scrub(frame['vars'])

    breadcrumbs = (event.get('breadcrumbs') or {}).get('values') or []
    for crumb in breadcrumbs:
        if isinstance(crumb.get('data'), dict):
            crumb['data'] = _scrub(crumb['data'])

    return event
