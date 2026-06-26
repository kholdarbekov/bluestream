"""Balance-anchored bottle-return prompt: keyboard + handler wiring."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from staff_bot.keyboards.delivery import DeliveryKeyboards
from staff_bot.handlers.delivery.status_update import StatusUpdateHandler


def _callbacks(markup):
    return [btn.callback_data for row in markup.inline_keyboard for btn in row]


def _new_handler():
    return StatusUpdateHandler.__new__(StatusUpdateHandler)


@pytest.mark.unit
class TestBottleReturnOptionsKeyboard:
    def test_positive_balance_shows_all_custom_none(self):
        markup = DeliveryKeyboards.bottle_return_options("en", 42, 7)
        assert _callbacks(markup) == [
            "staff_bottles_full_42",
            "staff_bottles_custom_42",
            "staff_bottles_none_42",
        ]

    def test_zero_balance_shows_zero_and_custom_only(self):
        markup = DeliveryKeyboards.bottle_return_options("en", 42, 0)
        cbs = _callbacks(markup)
        assert cbs == [
            "staff_bottles_full_42",
            "staff_bottles_custom_42",
        ]
        assert "staff_bottles_none_42" not in cbs


@pytest.mark.unit
class TestSuggestedReturnCount:
    def test_reads_customer_bottle_balance(self):
        h = _new_handler()
        ctx = MagicMock()
        ctx.user_data = {"current_delivery": {"customer_bottle_balance": 7,
                                              "expected_returnable_bottles": 3}}
        assert h._get_suggested_return_count(ctx) == 7

    def test_defaults_to_zero_when_absent(self):
        h = _new_handler()
        ctx = MagicMock()
        ctx.user_data = {"current_delivery": {}}
        assert h._get_suggested_return_count(ctx) == 0

    def test_expected_bottles_still_reads_order_returnable_qty(self):
        """The gate must stay on this order's returnable quantity, not the balance."""
        h = _new_handler()
        ctx = MagicMock()
        ctx.user_data = {"current_delivery": {"customer_bottle_balance": 7,
                                              "expected_returnable_bottles": 3}}
        assert h._get_expected_bottles(ctx) == 3


@pytest.mark.unit
class TestBuildBottlePrompt:
    def _ctx(self, balance):
        ctx = MagicMock()
        ctx.user_data = {"current_delivery": {"customer_bottle_balance": balance}}
        return ctx

    def test_positive_balance_keyboard_has_none_button(self):
        h = _new_handler()
        keyboard, message = h._build_bottle_prompt("en", 55, self._ctx(7))
        cbs = [b.callback_data for row in keyboard.inline_keyboard for b in row]
        assert "staff_bottles_none_55" in cbs
        assert "staff_bottles_full_55" in cbs
        assert isinstance(message, str) and message

    def test_zero_balance_keyboard_omits_none_button(self):
        h = _new_handler()
        keyboard, message = h._build_bottle_prompt("en", 55, self._ctx(0))
        cbs = [b.callback_data for row in keyboard.inline_keyboard for b in row]
        assert "staff_bottles_none_55" not in cbs
        assert "staff_bottles_full_55" in cbs
        assert isinstance(message, str) and message

    def test_message_differs_between_positive_and_zero_balance(self):
        h = _new_handler()
        _, msg_pos = h._build_bottle_prompt("en", 55, self._ctx(7))
        _, msg_zero = h._build_bottle_prompt("en", 55, self._ctx(0))
        # Positive balance uses bottles_return_prompt; zero uses
        # bottles_return_prompt_no_balance — distinct copy, never the same key.
        assert msg_pos != msg_zero


@pytest.mark.unit
class TestConfirmFullBottleReturnSubmitsBalance:
    def test_submits_suggested_balance_not_order_quantity(self):
        h = _new_handler()
        h._get_language = AsyncMock(return_value="en")
        h._submit_delivery_completion = AsyncMock()
        ctx = MagicMock()
        ctx.user_data = {
            "authenticated": True,
            "staff_roles": ["delivery_driver"],
            "current_delivery": {"customer_bottle_balance": 7,
                                 "expected_returnable_bottles": 3},
            "pending_delivery_cash_flow": {"delivery_id": 99, "cash_amount": 0},
        }
        update = MagicMock()
        update.callback_query = MagicMock()
        update.callback_query.answer = AsyncMock()

        asyncio.run(h.confirm_full_bottle_return(update, ctx))

        # Submits the balance (7), NOT the order returnable quantity (3).
        assert ctx.user_data["pending_delivery_cash_flow"]["bottles_returned"] == 7
        h._submit_delivery_completion.assert_awaited_once()
