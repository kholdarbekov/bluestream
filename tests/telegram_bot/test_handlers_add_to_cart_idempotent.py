"""Regression coverage for the product-details "Add to cart" accumulation bug.

Prod triage 2026-06-27 (customer +998917850017, user 267): each tap of the
product-details "Add to cart" button POSTed /cart/items, which the backend
treats as an INCREMENT (cart_item.quantity += quantity). A hesitant new user
tapping it repeatedly (add -> back -> add ...) silently piled on `min_order_qty`
each time, inflating the cart total in unit-price steps. From the customer's
side the order amount looked miscalculated.

The fix makes the entry point idempotent: it adds the product at its minimum
only when the product is not yet in the cart, and otherwise just re-opens the
quantity selector at the EXISTING cart quantity without incrementing.
"""

from unittest.mock import AsyncMock

import pytest

from api_client import APIResponse
from handlers import products as products_module
from tests.telegram_bot.helpers import (
    DummyCallbackQuery,
    DummyUpdate,
    FakeAPIClientContext,
    make_context,
)


def _i18n_get(key, language, *args, **kwargs):
    return f"{key}:{language}"


def _product_response(product_id=9, min_order_qty=2):
    return APIResponse(
        success=True,
        data={
            "data": {
                "product": {
                    "id": product_id,
                    "name": "Aqua Element 18.9 l",
                    "base_price": 18000,
                    "current_price": 18000,
                    "inventory": {
                        "min_order_quantity": min_order_qty,
                        "stock_quantity": 100,
                    },
                }
            }
        },
    )


def _cart_response(product_id, quantity):
    """Build a cart API payload. quantity=None means the product isn't in the cart."""
    items = [] if quantity is None else [{"product_id": product_id, "quantity": quantity}]
    return APIResponse(success=True, data={"data": {"cart": {"cart_items": items}}})


async def _run(monkeypatch, fake_client, callback_data="add_to_cart_9"):
    handler = products_module.ProductHandlers()
    handler._handle_error = AsyncMock()
    handler._handle_api_error = AsyncMock()
    handler._handle_auth_error = AsyncMock()
    handler._render_quantity_step = AsyncMock()

    update = DummyUpdate()
    update.callback_query = DummyCallbackQuery(data=callback_data)
    context = make_context()

    monkeypatch.setattr(products_module.i18n, "get_user_language", AsyncMock(return_value="en"))
    monkeypatch.setattr(products_module.i18n, "get", _i18n_get)
    monkeypatch.setattr(products_module, "get_auth_token", AsyncMock(return_value="tok"))
    monkeypatch.setattr(products_module, "api_client", fake_client)

    await handler.add_to_cart(update, context)
    return handler


@pytest.mark.unit
@pytest.mark.anyio
class TestAddToCartIdempotent:
    async def test_does_not_increment_when_product_already_in_cart(self, monkeypatch):
        """Re-tapping 'Add to cart' for a product already in the cart must NOT add
        more; it re-opens the selector at the existing quantity."""
        fake = FakeAPIClientContext()
        fake.get_product = AsyncMock(return_value=_product_response(9, 2))
        fake.get_cart = AsyncMock(return_value=_cart_response(9, 4))
        # If the handler (wrongly) increments, it would call add_to_cart -> 6.
        fake.add_to_cart = AsyncMock(return_value=_cart_response(9, 6))

        handler = await _run(monkeypatch, fake)

        fake.add_to_cart.assert_not_awaited()
        handler._render_quantity_step.assert_awaited_once()
        args = handler._render_quantity_step.await_args.args
        # (update, context, product_id, product, quantity, language)
        assert args[2] == 9
        assert args[4] == 4  # existing qty preserved, not 4 + min_order_qty

    async def test_adds_minimum_when_product_not_in_cart(self, monkeypatch):
        """First add of a product still creates the line at the product minimum."""
        fake = FakeAPIClientContext()
        fake.get_product = AsyncMock(return_value=_product_response(9, 2))
        fake.get_cart = AsyncMock(return_value=_cart_response(9, None))  # empty cart
        fake.add_to_cart = AsyncMock(return_value=_cart_response(9, 2))

        handler = await _run(monkeypatch, fake)

        fake.add_to_cart.assert_awaited_once()
        assert fake.add_to_cart.await_args.kwargs.get("quantity") == 2
        args = handler._render_quantity_step.await_args.args
        assert args[4] == 2
