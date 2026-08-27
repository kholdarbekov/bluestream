"""Reviewer finding #1 (BLOCKER): cancelling at zero-address checkout must
not crash and must not strand the customer.

`checkout_handler` (orders.py) arms `address_flow_origin='checkout'` and
sends a location keyboard whose Cancel row is text, not a callback button
(orders.py:605-613), so the tap lands on `cancel_address_text` — a
MessageHandler with `update.callback_query is None`. With origin=='checkout'
it used to call `product_handlers.show_cart(update, context)` unconditionally,
but `show_cart` ended with
`await self._edit_or_replace_callback_message(update.callback_query, ...)`,
which requires a real `query` and re-raises when there isn't one. That
propagated into `cancel_address_text`'s `except Exception`, so the customer
got only `telegram.action_cancelled_short` with `ReplyKeyboardRemove()` — no
cart, no menu, no keyboard at all — and the cleanup pops of
`address_flow_origin`/`temp_address_data` a few lines later never ran.

Drives the real handler chain (`profile.cancel_address_text` ->
`products.show_cart`); the only thing mocked is the API client underneath."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import ReplyKeyboardRemove
from telegram.ext import ConversationHandler

from handlers.profile import ProfileHandlers

from tests.telegram_bot.helpers import DummyUpdate, FakeAPIClientContext, make_context

pytestmark = [pytest.mark.unit, pytest.mark.anyio]


@pytest.fixture
def echo_i18n(monkeypatch):
    """i18n.get returns a humanised key tail and SILENTLY DROPS kwargs on a
    missing key, so an unstubbed render test passes against broken code. Echo
    the key plus every interpolated value instead."""
    def _get(key, language=None, *args, **kwargs):
        if kwargs:
            return f"{key}|" + "|".join(f"{k}={v}" for k, v in sorted(kwargs.items()))
        return key

    monkeypatch.setattr("i18n.i18n.get", _get)
    monkeypatch.setattr("i18n.i18n.get_user_language", AsyncMock(return_value="en"))
    return _get


async def test_cancel_at_zero_address_checkout_shows_cart_and_cleans_up(echo_i18n, monkeypatch):
    """Customer with no saved address taps Checkout, then taps the text
    Cancel button. Must not crash, must end with a usable keyboard (the
    cart), and must clear the leaked address-flow state."""
    handler = ProfileHandlers()
    # `_clear_address_flow_keys` now also clears the durable `address_draft`
    # twin (SDD 2026-08-26-address-flow-bot-state, Task 6); the real
    # BotUserRepository needs a connected pool this unit test never sets up.
    handler.user_repo = SimpleNamespace(clear_address_draft=AsyncMock())

    update = DummyUpdate()
    # This is the exact shape that crashed: a MessageHandler update with no
    # callback_query at all.
    assert update.callback_query is None

    ctx = make_context()
    ctx.user_data["address_flow_origin"] = "checkout"
    ctx.user_data["temp_location"] = {"lat": 41.31, "lng": 69.28}
    ctx.user_data["temp_address"] = "Chilanzar 5"
    ctx.user_data["temp_address_data"] = {"latitude": 41.31, "longitude": 69.28}

    cart_response = MagicMock(
        success=True,
        data={"data": {"cart": {"cart_items": [], "subtotal": 0}}},
    )
    fake_client = FakeAPIClientContext(get_cart=cart_response)
    monkeypatch.setattr("handlers.products.api_client", fake_client)
    monkeypatch.setattr("handlers.products.get_auth_token", AsyncMock(return_value="tok"))

    result = await handler.cancel_address_text(update, ctx)

    assert result == ConversationHandler.END

    calls = update.message.reply_text.await_args_list
    assert calls, "the customer must receive at least one reply"

    # The customer must end with SOME usable keyboard -- never a bare
    # ReplyKeyboardRemove with nothing to replace it.
    last_markup = calls[-1].kwargs.get("reply_markup")
    assert last_markup is not None, "customer must not be left with no keyboard at all"
    assert not isinstance(last_markup, ReplyKeyboardRemove), (
        "customer must not end on a bare ReplyKeyboardRemove with nothing to replace it"
    )

    # The cleanup pops at profile.py:1994-1997 must have run -- they never
    # ran while show_cart's AttributeError propagated up.
    assert "address_flow_origin" not in ctx.user_data
    assert "temp_address_data" not in ctx.user_data
    assert "temp_location" not in ctx.user_data
    assert "temp_address" not in ctx.user_data


async def test_cancel_outside_checkout_still_goes_to_main_menu(echo_i18n, monkeypatch):
    """Guard against overcorrecting: a plain (non-checkout) address-flow
    cancel must keep going to the main menu, not the cart."""
    handler = ProfileHandlers()
    handler.user_repo = SimpleNamespace(clear_address_draft=AsyncMock())

    update = DummyUpdate()
    ctx = make_context()
    ctx.user_data["address_flow_origin"] = "profile"
    ctx.user_data["temp_address_data"] = {"latitude": 41.31, "longitude": 69.28}

    monkeypatch.setattr(
        "handlers.profile.main_menu_for", AsyncMock(return_value="main-menu-kbd")
    )
    # If this test regressed into calling show_cart, it would blow up here.
    monkeypatch.setattr("handlers.products.api_client", None)

    result = await handler.cancel_address_text(update, ctx)

    assert result == ConversationHandler.END
    calls = update.message.reply_text.await_args_list
    assert calls[-1].kwargs.get("reply_markup") == "main-menu-kbd"
    assert "address_flow_origin" not in ctx.user_data
    assert "temp_address_data" not in ctx.user_data
