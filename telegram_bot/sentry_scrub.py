"""Sentry scrubbing for the customer bot process.

Secrets go by the rule the backend and the staff bot apply, `shared.secret_scrub` (by name, by value wherever it
travels, by shape, and a URL's password), imported from shared/ so the bot container stays free of backend imports.
The customer bot also hides the customer's personal data, which its events are full of: whatever sits under a name
holding phone, telegram_id, chat_id or username.
"""
from typing import Any, Dict, Optional

from shared.secret_scrub import names_a_secret, scrub_event

_PERSONAL_NAME_PARTS = ('phone', 'telegram_id', 'chat_id', 'username')


def _names_a_secret_or_personal_data(name: str) -> bool:
    lowered = name.lower()
    return names_a_secret(name) or any(part in lowered for part in _PERSONAL_NAME_PARTS)


def before_send(event: Dict[str, Any], hint: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    return scrub_event(event, hint, names=_names_a_secret_or_personal_data)
