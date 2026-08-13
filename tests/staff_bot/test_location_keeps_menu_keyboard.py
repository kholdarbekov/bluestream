"""§10 / spec 9(a): the location-share ACK must not wipe the driver's
main-menu reply keyboard. location.py:110 sent ReplyKeyboardRemove() with
nothing ever restoring it — recovery required discovering /menu.

Source-pinned guard (same style as tests/staff_bot/test_conversation_menu_escape.py's
BOT_FILE reads): the handler module must not reference ReplyKeyboardRemove at
all. Any future re-introduction — under any code path — turns this red."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from staff_bot.handlers.delivery import location as location_mod
from staff_bot.handlers.delivery.location import LocationHandler
from staff_bot.keyboards.menu import MenuKeyboards

LOCATION_FILE = (
    Path(__file__).resolve().parents[2] / "staff_bot" / "handlers" / "delivery" / "location.py"
)


@pytest.mark.unit
def test_location_handler_never_removes_reply_keyboard():
    source = LOCATION_FILE.read_text(encoding="utf-8")
    assert "ReplyKeyboardRemove" not in source, (
        "location.py must not wipe the main-menu reply keyboard "
        "(route-UX plan 2026-08-11 Task 15, spec §9a/§10)"
    )


@pytest.mark.unit
def test_location_handler_still_acks_with_inline_view_button():
    """The ACK itself stays: the inline 'active deliveries' button is how the
    driver reaches the freshly optimized list."""
    source = LOCATION_FILE.read_text(encoding="utf-8")
    assert "staff_active_deliveries" in source
    assert "InlineKeyboardMarkup" in source


# --- Behavioral guard -------------------------------------------------
#
# Dropping ReplyKeyboardRemove is necessary but not sufficient: Telegram's
# `one_time_keyboard=True` (used by CommonKeyboards.location_request, see
# staff_bot/keyboards/common.py) only auto-hides *itself* on use — it does
# NOT bring back whatever reply keyboard was showing before it. Per the
# Telegram Bot API docs, the keyboard "will still be available" for recall
# via the client's expand button, but it is the SAME one-time keyboard
# (Share Location / Cancel), not the driver's main menu. So merely omitting
# reply_markup on the ACK leaves the stale location-request keyboard as the
# recallable one, not the main menu — the bug's promised outcome ("driver
# ends up with the main menu") requires explicitly re-attaching it via the
# real menu builder (MenuKeyboards.main_menu), the same one login/menu/
# language-switch already use, so a driver never sees a hand-assembled
# second copy of the layout.


class _LocationApiClient:
    def __init__(self, response):
        self.client = MagicMock()
        self.client.update_driver_location = AsyncMock(return_value=response)

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, *args):
        return False


def _context(staff_roles):
    ctx = MagicMock()
    ctx.user_data = {
        "authenticated": True,
        "staff_roles": staff_roles,
        "language": "en",
    }
    return ctx


def _location_update():
    update = MagicMock()
    update.message = MagicMock()
    update.message.location = MagicMock(latitude=41.31, longitude=69.28)
    update.message.reply_text = AsyncMock()
    update.edited_message = None
    update.effective_user = MagicMock(id=555)
    return update


def _run_ack(monkeypatch, staff_roles):
    handler = LocationHandler()
    monkeypatch.setattr(handler, "_get_language", AsyncMock(return_value="en"))
    monkeypatch.setattr(handler, "_get_auth_token", AsyncMock(return_value="tok"))
    monkeypatch.setattr(
        location_mod, "api_client",
        _LocationApiClient(MagicMock(success=True)),
    )
    update = _location_update()
    ctx = _context(staff_roles)

    asyncio.run(handler.handle_location_update(update, ctx))

    return update.message.reply_text.call_args_list


@pytest.mark.unit
def test_location_ack_restores_the_driver_main_menu(monkeypatch):
    calls = _run_ack(monkeypatch, ["delivery_driver"])

    ack_call = calls[0]
    actual_markup = ack_call.kwargs.get("reply_markup")
    expected_markup = MenuKeyboards.main_menu("en", ["delivery_driver"])

    assert actual_markup is not None, (
        "the ACK must explicitly restore a reply keyboard, not merely omit "
        "reply_markup — a one-time keyboard does not auto-restore a prior one"
    )
    assert actual_markup.keyboard == expected_markup.keyboard


@pytest.mark.unit
def test_location_ack_never_removes_reply_keyboard_via_kwarg():
    """Behavioral counterpart to the source-pinned guard above: the ACK call
    must not pass a keyboard-removing markup."""
    from telegram import ReplyKeyboardRemove

    # Defensive: if this ever regresses, the source-pinned test above would
    # already fail at import time — this asserts the type directly too.
    assert not isinstance(MenuKeyboards.main_menu("en", ["delivery_driver"]), ReplyKeyboardRemove)


@pytest.mark.unit
def test_location_ack_gives_dual_role_driver_the_combined_menu(monkeypatch):
    """A driver who is ALSO an operator must see the combined menu — reusing
    MenuKeyboards.main_menu with the real staff_roles from context, never a
    hand-assembled driver-only copy that would hide their operator rows."""
    calls = _run_ack(monkeypatch, ["delivery_driver", "operator"])

    ack_call = calls[0]
    actual_markup = ack_call.kwargs.get("reply_markup")
    combined_markup = MenuKeyboards.main_menu("en", ["delivery_driver", "operator"])
    driver_only_markup = MenuKeyboards.main_menu("en", ["delivery_driver"])

    assert actual_markup.keyboard == combined_markup.keyboard
    assert actual_markup.keyboard != driver_only_markup.keyboard


@pytest.mark.unit
def test_location_ack_second_message_keeps_inline_view_button(monkeypatch):
    """The 2-message ACK shape is untouched by this fix: the second message
    still carries the inline 'active deliveries' button."""
    from telegram import InlineKeyboardMarkup

    calls = _run_ack(monkeypatch, ["delivery_driver"])

    assert len(calls) == 2
    second_markup = calls[1].kwargs.get("reply_markup")
    assert isinstance(second_markup, InlineKeyboardMarkup)
