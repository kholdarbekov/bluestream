"""Regression: cancel_order_confirm_no must not mutate the immutable CallbackQuery.

Prod bug: tapping "No" on the order-cancel confirmation raised
`AttributeError: Attribute \`data\` of class \`CallbackQuery\` can't be set!`
because the handler did `query.data = f"order_{id}"` to re-dispatch into
order_details. python-telegram-bot CallbackQuery objects are frozen, so the
assignment throws and the user never returns to order details.
"""

from unittest.mock import AsyncMock

import pytest

from handlers import orders as orders_module
from tests.telegram_bot.helpers import DummyMessage, DummyUpdate, make_context


class FrozenCallbackQuery:
    """Mimics python-telegram-bot's immutable CallbackQuery: `data` can't be set."""

    def __init__(self, data: str = "noop"):
        object.__setattr__(self, "_data", data)
        self.message = DummyMessage()
        self.answer = AsyncMock()
        self.edit_message_text = AsyncMock()

    @property
    def data(self):
        return self._data

    @data.setter
    def data(self, value):  # noqa: D401 - mirrors PTB's frozen-object error
        raise AttributeError("Attribute `data` of class `CallbackQuery` can't be set!")


@pytest.mark.unit
@pytest.mark.anyio
class TestCancelOrderConfirmNo:
    async def test_returns_to_order_details_without_mutating_callback_data(self, monkeypatch):
        handler = orders_module.OrderHandlers()
        handler.order_details = AsyncMock()
        handler._handle_error = AsyncMock()

        update = DummyUpdate()
        update.callback_query = FrozenCallbackQuery(data="cancel_order_confirm_no")
        context = make_context()
        context.user_data["cancelling_order_id"] = 42

        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))

        await handler.cancel_order_confirm_no(update, context)

        # Re-dispatches into order_details with the id passed explicitly...
        handler.order_details.assert_awaited_once_with(update, context, order_id=42)
        # ...without ever mutating the frozen callback data...
        assert update.callback_query.data == "cancel_order_confirm_no"
        # ...and without falling into the error handler.
        handler._handle_error.assert_not_awaited()
        # The cancellation context is cleared.
        assert "cancelling_order_id" not in context.user_data
