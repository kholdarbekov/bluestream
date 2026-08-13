"""Spec §4.6: the try-out flow strands staff with no reply keyboard — the same
bug fixed for drivers in tests/staff_bot/test_location_keeps_menu_keyboard.py.
Source-pinned in the same style: any re-introduction turns this red."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from staff_bot.handlers import tryouts as tryouts_mod
from staff_bot.handlers.tryouts import ENTER_TRYOUT_ADDRESS, TryoutHandler

TRYOUTS_FILE = Path(__file__).resolve().parents[2] / "staff_bot" / "handlers" / "tryouts.py"


@pytest.mark.unit
def test_tryout_handler_never_removes_the_reply_keyboard():
    source = TRYOUTS_FILE.read_text(encoding="utf-8")
    assert "ReplyKeyboardRemove" not in source, (
        "a one-time keyboard does not restore the previous one — re-attach "
        "MenuKeyboards.main_menu instead of removing the keyboard outright"
    )


@pytest.mark.unit
def test_tryout_geocode_failure_leaves_a_way_to_retry():
    """The failure path stays in ENTER_TRYOUT_ADDRESS, so it must leave the
    staff member a location button, not an empty keyboard."""
    source = TRYOUTS_FILE.read_text(encoding="utf-8")
    assert "MenuKeyboards.main_menu" in source or "CommonKeyboards.location_request" in source


# --- behavioral pin: a wrong-keyboard swap at the retry site is invisible to
# the source-greps above, so drive the actual handler. ----------------------


class _ApiClient:
    """Async-context-manager stub matching `async with api_client as client`."""

    def __init__(self, geocode_response):
        self.client = MagicMock()
        self.client.reverse_geocode_address = AsyncMock(return_value=geocode_response)

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, *args):
        return False


def _context():
    ctx = MagicMock()
    ctx.user_data = {
        "authenticated": True,
        "staff_roles": ["delivery_driver"],
        "language": "en",
        "new_tryout": {"items": []},
    }
    return ctx


def _location_update(latitude=41.31, longitude=69.28):
    update = MagicMock()
    update.message = MagicMock()
    update.message.location = MagicMock(latitude=latitude, longitude=longitude)
    update.message.reply_text = AsyncMock()
    update.callback_query = None
    update.effective_user = MagicMock(id=555)
    return update


def _handler(monkeypatch, geocode_response):
    handler = TryoutHandler()
    monkeypatch.setattr(handler, "_get_language", AsyncMock(return_value="en"))
    monkeypatch.setattr(handler, "_get_auth_token", AsyncMock(return_value="tok"))
    monkeypatch.setattr(tryouts_mod, "api_client", _ApiClient(geocode_response))
    return handler


@pytest.mark.unit
def test_geocode_failure_returns_a_working_location_button_not_a_menu(monkeypatch):
    """This is the actual bug: staff sending a location that fails to reverse
    geocode must stay in ENTER_TRYOUT_ADDRESS with a real location-request
    button, not a bare/removed keyboard and not the main menu (the handler
    hasn't left the conversation, so a menu button here would be a dead end
    too — the driver is still expected to answer this same prompt)."""
    failing_response = MagicMock(success=False, status_code=400, data=None)
    handler = _handler(monkeypatch, failing_response)
    update, ctx = _location_update(), _context()

    result = asyncio.run(handler.receive_create_location(update, ctx))

    assert result == ENTER_TRYOUT_ADDRESS
    update.message.reply_text.assert_awaited_once()
    markup = update.message.reply_text.await_args.kwargs.get("reply_markup")
    assert markup is not None, "the staff member must get a keyboard back, not a bare message"
    first_button = markup.keyboard[0][0]
    assert first_button.request_location is True, (
        "a retry needs a REAL location-request button, not the main menu and not None"
    )
