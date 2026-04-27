"""
Typed error hierarchy for Telegram bot handlers.

Used by `BaseHandler._handle_error` to dispatch user-facing replies and
structured logs based on error category. Handlers should raise these
(or let them propagate from api_client / services) rather than returning
ad-hoc error strings.
"""


class BotError(Exception):
    """Base class for bot-layer errors with optional i18n override and context."""

    default_i18n_key: str = 'telegram.error_occurred'

    def __init__(self, message: str = '', *, i18n_key: str | None = None, context: dict | None = None):
        super().__init__(message)
        self.message = message
        self.i18n_key = i18n_key or self.default_i18n_key
        self.context = context or {}


class BotAuthError(BotError):
    """Authentication / authorization failure (expired token, unauthorized user, etc.)."""

    default_i18n_key = 'telegram.error.auth_failed'


class BotAPIError(BotError):
    """Backend returned a non-success response with a user-presentable message."""

    default_i18n_key = 'telegram.error_occurred'


class BotNetworkError(BotError):
    """Transport-level failure reaching the backend (timeout, connection refused)."""

    default_i18n_key = 'telegram.error_occurred'


class BotValidationError(BotError):
    """User input failed validation. `message` should be the localized reason."""

    default_i18n_key = 'telegram.error.invalid_input'
