"""At /start login, a deactivated driver must see the deactivated-account
message; a non-staff Telegram account still sees the generic not-staff message."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from staff_bot.handlers.base import BaseHandler
from staff_bot.handlers.start import StartHandler
from staff_bot.api_client import APIResponse
from staff_bot.utils import auth_refusals
import staff_bot.handlers.start as start_mod


class _FakeApiClient:
    """Stand-in for the module-level api_client used as an async context manager."""

    def __init__(self, response):
        self._response = response
        self.staff_login = AsyncMock(return_value=response)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _run_login(monkeypatch, response):
    # Return the i18n key verbatim so we can assert which message was chosen
    # without depending on DB-seeded translation text.
    monkeypatch.setattr(start_mod.i18n, "get", lambda key, *a, **k: key)
    monkeypatch.setattr(start_mod, "api_client", _FakeApiClient(response))

    handler = StartHandler.__new__(StartHandler)
    handler.user_repo = MagicMock()
    handler.user_repo.update_user_language = AsyncMock()

    msg = MagicMock()
    msg.reply_text = AsyncMock()
    update = MagicMock()
    update.message = msg
    update.effective_user = MagicMock(id=42)

    ctx = MagicMock()
    ctx.user_data = {"language": "en"}

    asyncio.run(handler._authenticate_with_binding(update, ctx))
    return msg.reply_text


@pytest.mark.unit
class TestLoginDeactivatedMessage:
    def test_deactivated_driver_sees_deactivated_message(self, monkeypatch):
        response = APIResponse(
            success=False,
            error="forbidden",
            status_code=403,
            error_code="STAFF_ACCOUNT_DEACTIVATED",
        )
        reply_text = _run_login(monkeypatch, response)

        reply_text.assert_called_once()
        # The map's key, the one the silent re-login shows too. It used to be a
        # twin key (`staff.account_deactivated`) with the same text, decided here.
        assert reply_text.call_args.args[0] == "staff.error.api.account_deactivated"

    def test_non_staff_still_sees_not_staff_message(self, monkeypatch):
        response = APIResponse(
            success=False,
            error="forbidden",
            status_code=403,
            error_code="STAFF_TELEGRAM_NOT_APPROVED",
        )
        reply_text = _run_login(monkeypatch, response)

        reply_text.assert_called_once()
        assert reply_text.call_args.args[0] == "staff.not_staff"


@pytest.mark.unit
@pytest.mark.parametrize(
    "code, key",
    [
        ("STAFF_INVALID_INVITE_TOKEN", "staff.error.api.invalid_invite"),
        ("STAFF_ACCOUNT_INACTIVE", "staff.error.api.account_inactive"),
        ("STAFF_NO_ROLE", "staff.not_staff"),
        ("STAFF_TELEGRAM_NOT_APPROVED", "staff.not_staff"),
    ],
)
def test_a_login_refusal_is_named_by_its_code(monkeypatch, code, key):
    response = APIResponse(success=False, error="forbidden", status_code=403, error_code=code)
    reply_text = _run_login(monkeypatch, response)
    assert reply_text.call_args.args[0] == key


@pytest.mark.unit
@pytest.mark.parametrize("code", sorted(auth_refusals.ACCOUNT_REFUSAL_CODES))
def test_an_account_refusal_at_login_reads_the_maps_copy(monkeypatch, code):
    """/start and the silent re-login must say the same sentence for a
    switched-off account; the map decides it for both."""
    response = APIResponse(success=False, error="forbidden", status_code=403, error_code=code)
    reply_text = _run_login(monkeypatch, response)
    assert reply_text.call_args.args[0] == BaseHandler.API_ERROR_CODE_KEY_MAP[code]
