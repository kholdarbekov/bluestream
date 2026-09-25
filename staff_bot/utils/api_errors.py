"""Turning a backend refusal into words: detail copy and the backend's reason."""
from dataclasses import dataclass, field
from html import escape
from typing import Any, Mapping, Optional

from shared.api_errors import RAW_EXCEPTION_ERROR_TYPES
from staff_bot.i18n import i18n

# Backend sentences that say nothing a person can act on.
GENERIC_SERVER_MESSAGES = frozenset({"validation failed", "an unexpected error occurred"})
BACKEND_REASON_MAX_CHARS = 300


@dataclass(frozen=True)
class ErrorDetailCopy:
    """Copy for one error code that names the backend's numbers.

    ``placeholders`` maps each ``{placeholder}`` in ``key``'s copy to the
    ``details`` field the backend publishes for it. ``formats`` names how a
    value is rendered: ``"money"`` (``format_currency``) or ``"delivery_status"``
    (``format_delivery_status``); unnamed values are shown as-is. Used only when
    EVERY field is present, so an older backend that sends no details falls back
    to the plain key.
    """

    key: str
    placeholders: Mapping[str, str]
    formats: Mapping[str, str] = field(default_factory=dict)


def _format_value(value: Any, fmt: Optional[str], language: str) -> str:
    # Imported here: formatters imports i18n, and this module is imported by
    # handlers/base.py at import time.
    from staff_bot.utils.formatters import format_currency, format_delivery_status

    if fmt == "money":
        return format_currency(value, language=language)
    if fmt == "delivery_status":
        return format_delivery_status(str(value), language)
    return str(value)


def render_detail_copy(spec: ErrorDetailCopy, details: Any, language: str, *, html: bool = False) -> Optional[str]:
    """``spec``'s copy filled from ``details``, or None when a field is missing."""
    if not isinstance(details, Mapping):
        return None
    values = {}
    for placeholder, detail_field in spec.placeholders.items():
        if details.get(detail_field) is None:
            return None
        rendered = _format_value(details[detail_field], spec.formats.get(placeholder), language)
        values[placeholder] = escape(rendered, quote=False) if html else rendered
    return i18n.get(spec.key, language, **values)


def displayable_backend_reason(status_code: Any, error_type: Any, server_message: Any) -> Optional[str]:
    """The backend's own sentence, if it is one a person should read.

    Owner ruling 2026-09-24: when the bot has no translated copy for a refusal,
    show the backend's reason rather than a generic "check the data". Only 4xx
    business refusals qualify: 401 belongs to the session layer, 429 has its
    own copy, and a 5xx or a raw Python exception's ``str()`` is internals.
    """
    if not isinstance(server_message, str) or not isinstance(status_code, int):
        return None
    if not 400 <= status_code < 500 or status_code in (401, 429):
        return None
    if isinstance(error_type, str) and error_type in RAW_EXCEPTION_ERROR_TYPES:
        return None
    reason = " ".join(server_message.split())
    if not reason or reason.lower() in GENERIC_SERVER_MESSAGES:
        return None
    if len(reason) > BACKEND_REASON_MAX_CHARS:
        reason = reason[: BACKEND_REASON_MAX_CHARS - 1].rstrip() + "…"
    return reason


class GeocoderUnavailable(Exception):
    """The geocoder itself failed (5xx or transport) — not "no such address"."""

    def __init__(self, response):
        super().__init__(getattr(response, "error", "geocoder unavailable"))
        self.response = response


def geocoder_down(response) -> bool:
    status = getattr(response, "status_code", None)
    return not getattr(response, "success", False) and (status is None or (isinstance(status, int) and status >= 500))
