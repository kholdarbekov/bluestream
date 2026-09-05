"""Second-wave Telegram handler coverage for products/orders/loyalty."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import eligibility
from handlers import loyalty as loyalty_module
from handlers import orders as orders_module
from handlers import products as products_module
from tests.telegram_bot.helpers import DummyCallbackQuery, DummyUpdate, FakeAPIClientContext, make_context


def _resp(success=True, data=None, error=None, status_code=200):
    return SimpleNamespace(success=success, data=data or {}, error=error, status_code=status_code)


def _i18n_get(key, language, *args, **kwargs):
    return f"{key}:{language}"


def _i18n_get_interpolated(key, language, *args, **kwargs):
    """Like `_i18n_get`, but also renders kwargs — needed wherever a test
    asserts on a MONEY VALUE that only reaches the screen through an i18n
    placeholder (e.g. the quote block's `estimate_reward_line`)."""
    rendered = f"{key}:{language}"
    if kwargs:
        rendered += "(" + ",".join(f"{k}={v}" for k, v in sorted(kwargs.items())) + ")"
    return rendered


def served_cart(*lines, **cart_extra):
    """A ``GET /api/v1/cart`` payload shaped like ``CartService.get_cart_details``.

    THE SINGLE FABRICATION POINT for cart payloads fed to the checkout screens.
    The real service publishes an authoritative per-line ``total_price`` and an
    authoritative cart-level ``subtotal`` — both composed by
    ``CartService.get_cart_summary`` from the SAME contract-aware unit prices
    order creation charges — and those are the two keys the confirmation screen
    and the reward picker read.

    A cart literal that omits them is a payload the backend never serves, and a
    test built on one silently exercises a zero total. See
    ``tests/integration/test_checkout_total_is_server_authoritative.py`` for the
    parity pin over the REAL service.

    ``lines`` are ``(product_dict, quantity)`` pairs. A product missing ``id``
    (most callers only care about name/price) gets a sequential one, since the
    checkout screen now reads it to build the estimate-quote payload.
    """
    items = [
        {
            "product": {"id": index + 1, **product},
            "quantity": quantity,
            "total_price": product["current_price"] * quantity,
        }
        for index, (product, quantity) in enumerate(lines)
    ]
    cart = {
        "cart_items": items,
        "subtotal": sum(item["total_price"] for item in items),
    }
    cart.update(cart_extra)
    return {"data": {"cart": cart}}


def served_estimate(cart_payload, payment_method="cash", **pricing_overrides):
    """A `POST cart/estimate` payload shaped like `CartService.
    calculate_cart_estimate`, over a cart already built by `served_cart` above
    — so the item lines and subtotal are not a second fabrication, they are
    read straight back out of the cart payload the test already built.
    """
    cart = cart_payload["data"]["cart"]
    subtotal = float(cart["subtotal"])
    pricing = {
        "items_subtotal": subtotal,
        "delivery_fee": 0.0,
        "discount_amount": 0.0,
        "loyalty_discount": 0.0,
        "tier_discount": 0.0,
        "tier_name": None,
        "tier_discount_percentage": 0.0,
        "cod_savings": 0.0,
        "payment_method": payment_method,
        "final_total": subtotal,
    }
    pricing.update(pricing_overrides)
    return {
        "data": {
            "items": [
                {
                    "product_id": item["product"]["id"],
                    "product_name": item["product"].get("name", ""),
                    "quantity": item["quantity"],
                    "unit_price": item["product"].get("current_price", 0),
                    "subtotal": item["total_price"],
                }
                for item in cart["cart_items"]
            ],
            "pricing": pricing,
        }
    }


@pytest.mark.unit
@pytest.mark.anyio
class TestProductHandlerWave2:
    async def test_cart_handler_dispatches_view(self, monkeypatch):
        handler = products_module.ProductHandlers()
        handler.show_cart = AsyncMock()
        handler._clear_cart = AsyncMock()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="cart_view")
        context = make_context()
        monkeypatch.setattr(products_module.i18n, "get_user_language", AsyncMock(return_value="en"))

        await handler.cart_handler(update, context)

        handler.show_cart.assert_awaited_once_with(update, context, edit_mode=False)
        handler._clear_cart.assert_not_awaited()

    async def test_cart_handler_dispatches_clear(self, monkeypatch):
        handler = products_module.ProductHandlers()
        handler.show_cart = AsyncMock()
        handler._clear_cart = AsyncMock()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="cart_clear")
        context = make_context()
        monkeypatch.setattr(products_module.i18n, "get_user_language", AsyncMock(return_value="en"))

        await handler.cart_handler(update, context)

        handler._clear_cart.assert_awaited_once_with(update, context)
        handler.show_cart.assert_not_awaited()

    async def test_cart_handler_dispatches_checkout(self, monkeypatch):
        handler = products_module.ProductHandlers()
        checkout = AsyncMock()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="cart_checkout")
        context = make_context()

        monkeypatch.setattr(products_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(orders_module, "order_handlers", SimpleNamespace(checkout_handler=checkout))

        await handler.cart_handler(update, context)
        checkout.assert_awaited_once_with(update, context)

    async def test_quantity_handler_calls_api_error_for_cart_update_failure(self, monkeypatch):
        handler = products_module.ProductHandlers()
        handler._handle_api_error = AsyncMock()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="qty_inc_9_1")
        context = make_context()

        monkeypatch.setattr(products_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(products_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(products_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(products_module, "api_client", FakeAPIClientContext(
            get_product=_resp(success=True, data={"data": {"product": {"name": "Water", "pricing": {"base_price": 20000}}}}),
            update_cart_item=_resp(success=False, error="update failed"),
        ))

        await handler.quantity_handler(update, context)
        handler._handle_api_error.assert_awaited_once_with(update, "update failed", "en")

    async def test_quantity_handler_prefers_current_price_over_base_price(self, monkeypatch):
        handler = products_module.ProductHandlers()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="qty_inc_9_1")
        context = make_context()

        monkeypatch.setattr(products_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(products_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(products_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(products_module.ProductKeyboards, "quantity_selector", lambda *_a, **_k: "qty-kbd")
        monkeypatch.setattr(
            products_module,
            "api_client",
            FakeAPIClientContext(
                get_product=_resp(
                    success=True,
                    data={"data": {"product": {"name": "Water", "pricing": {"base_price": 20000, "current_price": 14000}}}},
                ),
                update_cart_item=_resp(success=True),
            ),
        )

        await handler.quantity_handler(update, context)

        text = update.callback_query.edit_message_text.await_args.kwargs["text"]
        assert "28,000 UZS" in text
        assert "40,000 UZS" not in text

    async def test_format_product_details_prefers_current_price(self, monkeypatch):
        handler = products_module.ProductHandlers()
        monkeypatch.setattr(products_module.i18n, "get", _i18n_get)

        details = handler._format_product_details(
            {
                "name": "Water",
                "pricing": {"base_price": 20000, "current_price": 15000},
                "inventory": {"stock_quantity": 5},
                "specifications": {"volume": 19, "volume_unit": "L"},
            },
            "en",
        )

        assert "15,000" in details
        assert "20,000" not in details

    async def test_show_cart_empty_cart(self, monkeypatch):
        handler = products_module.ProductHandlers()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="cart_view")
        context = make_context()
        captured = {}

        def _cart_actions(lang, cart_is_empty, meets_minimum, edit_mode=False, cart_items=None, edit_return=None):
            captured["args"] = (lang, cart_is_empty, meets_minimum)
            return "cart-kbd"

        monkeypatch.setattr(products_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(products_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(products_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(products_module.OrderKeyboards, "cart_actions", _cart_actions)
        monkeypatch.setattr(products_module, "api_client", FakeAPIClientContext(
            get_cart=_resp(success=True, data={"data": {"cart": {"cart_items": []}}}),
        ))

        await handler.show_cart(update, context)

        assert captured["args"] == ("en", True, True)
        update.callback_query.edit_message_text.assert_awaited_once_with(
            text="telegram.cart_empty:en",
            reply_markup="cart-kbd",
        )
        update.callback_query.answer.assert_awaited_once()

    async def test_show_cart_below_minimum_amount(self, monkeypatch):
        handler = products_module.ProductHandlers()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="cart_view")
        context = make_context()
        captured = {}

        def _cart_actions(lang, cart_is_empty, meets_minimum, edit_mode=False, cart_items=None, edit_return=None):
            captured["args"] = (lang, cart_is_empty, meets_minimum)
            return "cart-kbd"

        # `served_cart` publishes the server's `total_price`/`subtotal`, which is
        # what the cart screen reads since the sweep-#7 fix. A literal without
        # them exercises a zero total and would pass for the wrong reason.
        cart_payload = served_cart(({"name": "Bottle", "current_price": 5000}, 2))
        monkeypatch.setattr(products_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(products_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(products_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(products_module.OrderKeyboards, "cart_actions", _cart_actions)
        monkeypatch.setattr(products_module, "api_client", FakeAPIClientContext(get_cart=_resp(success=True, data=cart_payload)))

        await handler.show_cart(update, context)

        assert captured["args"] == ("en", False, False)
        assert "telegram.cart_min_order_warning:en" in update.callback_query.edit_message_text.await_args.kwargs["text"]

    async def test_show_cart_includes_cod_prepayment_summary(self, monkeypatch):
        handler = products_module.ProductHandlers()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="cart_view")
        context = make_context()

        cart_payload = served_cart(
            ({"name": "Bottle", "current_price": 25000}, 1),
            cod_prepayment={
                "available_balance": 40000,
                "potential_applied_amount": 25000,
                "estimated_payable_after_prepayment": 0,
            },
        )
        monkeypatch.setattr(products_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(products_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(products_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(products_module, "api_client", FakeAPIClientContext(get_cart=_resp(success=True, data=cart_payload)))

        await handler.show_cart(update, context)

        text = update.callback_query.edit_message_text.await_args.kwargs["text"]
        assert "telegram.cart.cod_prepaid_balance:en" in text
        assert "telegram.cart.cod_prepaid_auto_applied_next:en" in text

    async def test_show_cart_prepayment_block_says_the_credit_is_cash_only(self, monkeypatch):
        """B4b: the cart screen is rendered BEFORE the rail is chosen.

        The confirmation screen and the post-order brief are already gated on
        `payment_method == 'cash'`; this one cannot be, so it must carry the
        condition. After B4a the balance can come from a cancelled CARD order,
        and a customer who reads "will be applied to your next order" and then
        pays by Click is charged in full.
        """
        handler = products_module.ProductHandlers()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="cart_view")
        context = make_context()

        cart_payload = served_cart(
            ({"name": "Bottle", "current_price": 25000}, 1),
            cod_prepayment={
                "available_balance": 40000,
                "potential_applied_amount": 25000,
                "estimated_payable_after_prepayment": 0,
            },
        )
        monkeypatch.setattr(products_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(products_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(products_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(products_module, "api_client", FakeAPIClientContext(get_cart=_resp(success=True, data=cart_payload)))

        await handler.show_cart(update, context)

        text = update.callback_query.edit_message_text.await_args.kwargs["text"]
        assert "telegram.payments.prepaid_cash_only:en" in text

    async def test_show_cart_omits_the_prepayment_block_without_a_balance(self, monkeypatch):
        """The note is part of the block, not a permanent fixture of the cart."""
        handler = products_module.ProductHandlers()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="cart_view")
        context = make_context()

        cart_payload = served_cart(
            ({"name": "Bottle", "current_price": 25000}, 1),
            cod_prepayment={
                "available_balance": 0,
                "potential_applied_amount": 0,
                "estimated_payable_after_prepayment": 25000,
            },
        )
        monkeypatch.setattr(products_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(products_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(products_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(products_module, "api_client", FakeAPIClientContext(get_cart=_resp(success=True, data=cart_payload)))

        await handler.show_cart(update, context)

        text = update.callback_query.edit_message_text.await_args.kwargs["text"]
        assert "telegram.payments.prepaid_cash_only:en" not in text
        assert "telegram.cart.cod_prepaid_balance:en" not in text

    async def test_clear_cart_success_answers_and_refreshes(self, monkeypatch):
        handler = products_module.ProductHandlers()
        handler.show_cart = AsyncMock()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="cart_clear")
        context = make_context()

        monkeypatch.setattr(products_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(products_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(products_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(products_module, "api_client", FakeAPIClientContext(clear_cart=_resp(success=True)))

        await handler._clear_cart(update, context)

        update.callback_query.answer.assert_awaited_once_with("telegram.products.cart_cleared:en")
        handler.show_cart.assert_awaited_once_with(update, context)

    async def test_search_products_auth_error(self, monkeypatch):
        handler = products_module.ProductHandlers()
        handler._handle_auth_error = AsyncMock()
        update = DummyUpdate()
        context = make_context()

        monkeypatch.setattr(products_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(products_module, "get_auth_token", AsyncMock(return_value=None))
        monkeypatch.setattr(products_module, "api_client", FakeAPIClientContext())

        await handler.search_products(update, context, "water")
        handler._handle_auth_error.assert_awaited_once_with(update, "en")

    async def test_search_products_with_results_clears_search_state(self, monkeypatch):
        handler = products_module.ProductHandlers()
        handler.user_repo = SimpleNamespace(disarm=AsyncMock())
        update = DummyUpdate()
        context = make_context()

        products_payload = {
            "products": [
                {
                    "name": "Bottle",
                    "pricing": {"base_price": 10000},
                    "inventory": {"stock_quantity": 10},
                    "specifications": {"volume": "19", "volume_unit": "L"},
                }
            ]
        }
        monkeypatch.setattr(products_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(products_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(products_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(products_module.ProductKeyboards, "product_list", lambda *_a, **_k: "prod-kbd")
        monkeypatch.setattr(products_module, "api_client", FakeAPIClientContext(get_products=_resp(success=True, data=products_payload)))

        await handler.search_products(update, context, "bottle")

        update.message.reply_text.assert_awaited_once()
        # Was: update_user_state.assert_awaited_once_with(update.effective_user.id, {})
        # Same facts (same user id, the flow this screen owns), against the new method.
        handler.user_repo.disarm.assert_awaited_once_with(update.effective_user.id, 'search_products')


@pytest.mark.unit
@pytest.mark.anyio
class TestOrderHandlerWave2:
    async def test_payment_handler_sets_method_and_shows_confirmation(self, monkeypatch):
        handler = orders_module.OrderHandlers()
        handler._show_order_confirmation = AsyncMock()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="payment_cash")
        context = make_context()

        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))

        await handler.payment_handler(update, context)

        assert context.user_data["selected_payment_method"] == "cash"
        handler._show_order_confirmation.assert_awaited_once_with(update, context)

    async def test_confirm_order_missing_info_answers(self, monkeypatch):
        handler = orders_module.OrderHandlers()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="confirm_order")
        context = make_context()

        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(orders_module.i18n, "get", _i18n_get)

        await handler.confirm_order(update, context)
        update.callback_query.answer.assert_awaited_once_with("telegram.orders.missing_info:en")

    async def test_confirm_order_cash_success(self, monkeypatch):
        handler = orders_module.OrderHandlers()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="confirm_order")
        context = make_context()
        context.user_data["selected_address_id"] = 12
        context.user_data["selected_payment_method"] = "cash"

        order_data = {
            "data": {
                "order": {
                    "id": 77,
                    "total_amount": 50000,
                    "order_number": "ORD-77",
                    "order_items": [],
                }
            }
        }
        cart_data = {
            "data": {
                "cart": {
                    "cart_items": [
                        {"product": {"id": 1, "name": "Bottle", "current_price": 25000}, "quantity": 2},
                    ],
                    "cod_prepayment": {
                        "available_balance": 90000,
                        "potential_applied_amount": 50000,
                        "estimated_payable_after_prepayment": 0,
                    },
                }
            }
        }
        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(orders_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(orders_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(orders_module.MessageBuilder, "build_order_summary", lambda _o, _l: "order-summary")
        monkeypatch.setattr(orders_module, "main_menu_for", AsyncMock(return_value="menu-kbd"))
        monkeypatch.setattr(orders_module, "api_client", FakeAPIClientContext(
            get_cart=_resp(success=True, data=cart_data),
            create_order=_resp(success=True, data=order_data),
            clear_cart=_resp(success=True),
        ))

        await handler.confirm_order(update, context)

        assert context.user_data == {}
        update.callback_query.edit_message_text.assert_awaited_once()
        text = update.callback_query.edit_message_text.await_args.kwargs["text"]
        # Bot now routes the COD prepayment brief through i18n; the mock
        # returns "<key>:<language>", so we just verify the right key was used
        # with the expected formatted amounts.
        assert "telegram.orders.cod_prepayment_applied:en" in text
        update.callback_query.answer.assert_awaited_once_with("telegram.orders.placed_success:en")

    async def test_confirm_order_failure_consumes_selected_reward(self, monkeypatch):
        # A loyalty reward selected before checkout must be consumed once per
        # attempt regardless of outcome. On a failure path (here a generic API
        # error from create_order) the selection must NOT leak into the user's
        # next order and silently re-apply the reward.
        handler = orders_module.OrderHandlers()
        handler._handle_api_error = AsyncMock()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="confirm_order")
        context = make_context()
        context.user_data["selected_address_id"] = 12
        context.user_data["selected_payment_method"] = "cash"
        context.user_data["selected_reward_id"] = 99

        cart_data = {
            "data": {
                "cart": {
                    "cart_items": [
                        {"product": {"id": 1, "name": "Bottle", "current_price": 25000}, "quantity": 2},
                    ],
                }
            }
        }
        captured_order_data = {}

        fake_client = FakeAPIClientContext(
            get_cart=_resp(success=True, data=cart_data),
        )

        async def _create_order(_token, order_data, *_a, **_kw):
            captured_order_data.update(order_data)
            return _resp(success=False, error="order failed", status_code=400)

        fake_client.create_order = _create_order

        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(orders_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(orders_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(orders_module, "api_client", fake_client)

        await handler.confirm_order(update, context)

        # The reward was forwarded to the order attempt...
        assert captured_order_data.get("reward_id") == 99
        # ...and consumed despite the failure, so it cannot leak forward.
        assert "selected_reward_id" not in context.user_data
        handler._handle_api_error.assert_awaited_once()

    async def test_show_order_confirmation_auth_error(self, monkeypatch):
        handler = orders_module.OrderHandlers()
        handler._handle_auth_error = AsyncMock()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="payment_card")
        context = make_context()
        # A LIVE checkout: `_show_order_confirmation` now refuses to draw the
        # card at all without both keys, because a card missing them offers a
        # Confirm button whose only possible answer is "missing information".
        # tests/telegram_bot/test_checkout_screens_after_state_loss.py
        context.user_data["selected_address_id"] = 1
        context.user_data["selected_payment_method"] = "cash"

        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(orders_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(orders_module, "get_auth_token", AsyncMock(return_value=None))
        monkeypatch.setattr(orders_module, "api_client", FakeAPIClientContext())

        await handler._show_order_confirmation(update, context)
        handler._handle_auth_error.assert_awaited_once_with(update, "en")

    async def test_show_order_confirmation_success(self, monkeypatch):
        handler = orders_module.OrderHandlers()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="payment_card")
        context = make_context()
        context.user_data["selected_address_id"] = 5
        context.user_data["selected_payment_method"] = "card"

        cart_data = served_cart(({"name": "Bottle", "current_price": 12000}, 2))
        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(eligibility, "is_loyalty_eligible", AsyncMock(return_value=True))
        monkeypatch.setattr(orders_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(orders_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(orders_module.OrderKeyboards, "order_confirmation", lambda *_a, **_k: "confirm-kbd")
        monkeypatch.setattr(orders_module, "api_client", FakeAPIClientContext(
            get_cart=_resp(success=True, data=cart_data),
            estimate_cart=_resp(success=True, data=served_estimate(cart_data, payment_method="card")),
        ))

        await handler._show_order_confirmation(update, context)

        update.callback_query.edit_message_text.assert_awaited_once()
        text = update.callback_query.edit_message_text.await_args.kwargs["text"]
        assert "telegram.orders.confirmation_title:en" in text
        # The item lines now live in the quote block (no separate header).
        assert "telegram.orders.payment_info:en" in text
        # The money on screen is the server's, both per line and in total.
        assert "24,000" in text
        update.callback_query.answer.assert_awaited_once()

    async def test_show_order_confirmation_includes_cod_prepayment_summary(self, monkeypatch):
        handler = orders_module.OrderHandlers()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="payment_cash")
        context = make_context()
        context.user_data["selected_address_id"] = 5
        context.user_data["selected_payment_method"] = "cash"

        cart_data = served_cart(
            ({"name": "Bottle", "current_price": 12000}, 2),
            cod_prepayment={
                "available_balance": 40000,
                "potential_applied_amount": 24000,
                "estimated_payable_after_prepayment": 0,
            },
        )
        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(eligibility, "is_loyalty_eligible", AsyncMock(return_value=True))
        monkeypatch.setattr(orders_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(orders_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(orders_module.OrderKeyboards, "order_confirmation", lambda *_a, **_k: "confirm-kbd")
        monkeypatch.setattr(orders_module, "api_client", FakeAPIClientContext(
            get_cart=_resp(success=True, data=cart_data),
            estimate_cart=_resp(success=True, data=served_estimate(cart_data, payment_method="cash")),
        ))

        await handler._show_order_confirmation(update, context)

        text = update.callback_query.edit_message_text.await_args.kwargs["text"]
        assert "telegram.orders.cod_prepaid_balance:en" in text
        assert "telegram.orders.cod_prepaid_auto_applied:en" in text
        assert "telegram.orders.cod_estimated_payable:en" in text

    async def test_show_order_confirmation_shows_discount_reward(self, monkeypatch):
        from utils import format_price

        handler = orders_module.OrderHandlers()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="payment_card")
        context = make_context()
        context.user_data["selected_address_id"] = 5
        context.user_data["selected_payment_method"] = "card"
        context.user_data["selected_reward_id"] = 7

        # subtotal 24000, published by the server (see served_cart)
        cart_data = served_cart(({"name": "Bottle", "current_price": 12000}, 2))
        rewards_data = {"data": {"rewards": [
            {"id": 7, "name": "10k Off", "reward_type": "discount",
             "discount_type": "fixed", "discount_value": 10000, "min_order_value": 0},
        ]}}
        # This test asserts on the DISCOUNT AMOUNT, which now reaches the
        # screen only through the quote block's `estimate_reward_line`
        # placeholder — the shared `_i18n_get` deliberately drops kwargs, so
        # this one test needs the interpolating stub to see the money.
        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(eligibility, "is_loyalty_eligible", AsyncMock(return_value=True))
        monkeypatch.setattr(orders_module.i18n, "get", _i18n_get_interpolated)
        monkeypatch.setattr(orders_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(orders_module.OrderKeyboards, "order_confirmation", lambda *_a, **_k: "confirm-kbd")
        monkeypatch.setattr(orders_module, "api_client", FakeAPIClientContext(
            get_cart=_resp(success=True, data=cart_data),
            get_loyalty_rewards=_resp(success=True, data=rewards_data),
            estimate_cart=_resp(success=True, data=served_estimate(
                cart_data, payment_method="card", loyalty_discount=10000.0,
            )),
        ))

        await handler._show_order_confirmation(update, context)

        text = update.callback_query.edit_message_text.await_args.kwargs["text"]
        assert "telegram.loyalty.reward_applied:en" in text
        assert "10k Off" in text
        # fixed 10000 discount reflected on the discount line
        assert format_price(10000) in text

    async def test_show_order_confirmation_shows_free_product_reward(self, monkeypatch):
        handler = orders_module.OrderHandlers()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="payment_card")
        context = make_context()
        context.user_data["selected_address_id"] = 5
        context.user_data["selected_payment_method"] = "card"
        context.user_data["selected_reward_id"] = 9

        # subtotal 24000, published by the server (see served_cart)
        cart_data = served_cart(({"name": "Bottle", "current_price": 12000}, 2))
        rewards_data = {"data": {"rewards": [
            {"id": 9, "name": "Free Bottle", "reward_type": "free_product",
             "free_product_id": 2, "free_product_quantity": 2, "min_order_value": 0},
        ]}}
        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(eligibility, "is_loyalty_eligible", AsyncMock(return_value=True))
        monkeypatch.setattr(orders_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(orders_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(orders_module.OrderKeyboards, "order_confirmation", lambda *_a, **_k: "confirm-kbd")
        monkeypatch.setattr(orders_module, "api_client", FakeAPIClientContext(
            get_cart=_resp(success=True, data=cart_data),
            get_loyalty_rewards=_resp(success=True, data=rewards_data),
            estimate_cart=_resp(success=True, data=served_estimate(cart_data, payment_method="card")),
        ))

        await handler._show_order_confirmation(update, context)

        text = update.callback_query.edit_message_text.await_args.kwargs["text"]
        assert "telegram.loyalty.reward_applied:en" in text
        assert "Free Bottle" in text
        assert "2×" in text
        assert "telegram.loyalty.free_suffix:en" in text


@pytest.mark.unit
@pytest.mark.anyio
class TestCheckoutRewardSelection:
    """Selecting a loyalty reward from inside the checkout confirmation flow."""

    async def test_order_confirmation_keyboard_has_reward_row(self, monkeypatch):
        monkeypatch.setattr(orders_module.i18n, "get", _i18n_get)

        no_reward = orders_module.OrderKeyboards.order_confirmation(
            "en", meets_minimum=True, has_reward=False
        )
        cbs = [b.callback_data for row in no_reward.inline_keyboard for b in row]
        assert "checkout_choose_reward" in cbs
        assert "checkout_remove_reward" not in cbs

        with_reward = orders_module.OrderKeyboards.order_confirmation(
            "en", meets_minimum=True, has_reward=True
        )
        cbs2 = [b.callback_data for row in with_reward.inline_keyboard for b in row]
        assert "checkout_choose_reward" in cbs2
        assert "checkout_remove_reward" in cbs2

    async def test_choose_reward_lists_full_catalog_with_locks_and_balance(self, monkeypatch):
        # Balance 0 + two rewards: both shown as locked text lines, zero buttons.
        handler = orders_module.OrderHandlers()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="checkout_choose_reward")
        context = make_context()
        context.user_data["selected_address_id"] = 5

        # subtotal 24000, published by the server (see served_cart)
        cart_data = served_cart(({"name": "Bottle", "current_price": 12000}, 2))
        rewards_data = {"data": {
            "user_points_balance": 0,
            "rewards": [
                {"id": 1, "name": "Coin Locked", "points_cost": 100,
                 "can_redeem": False, "points_needed": 100, "min_order_value": 0},
                {"id": 2, "name": "Order Locked", "points_cost": 50,
                 "can_redeem": True, "points_needed": 0, "min_order_value": 999999},
            ],
        }}
        captured = {}

        def _picker(rewards, language):
            captured["rewards"] = rewards
            return "picker-kbd"

        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(eligibility, "is_loyalty_eligible", AsyncMock(return_value=True))
        monkeypatch.setattr(orders_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(orders_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(
            orders_module.OrderKeyboards, "checkout_reward_picker", staticmethod(_picker)
        )
        monkeypatch.setattr(orders_module, "api_client", FakeAPIClientContext(
            get_cart=_resp(success=True, data=cart_data),
            get_loyalty_rewards=_resp(success=True, data=rewards_data),
        ))

        await handler.checkout_choose_reward(update, context)

        # No reward is tappable: picker gets an empty affordable list.
        assert captured["rewards"] == []
        text = update.callback_query.edit_message_text.await_args.kwargs["text"]
        # Balance header present.
        assert "telegram.loyalty.balance_header:en" in text
        # Both rewards listed by name + cost.
        assert "Coin Locked" in text and "Order Locked" in text
        # Coin-shortfall lock line for reward 1, min-order lock line for reward 2.
        assert "telegram.loyalty.lock_need_coins:en" in text
        assert "telegram.loyalty.lock_add_order:en" in text

    async def test_choose_reward_affordable_in_budget_is_a_button(self, monkeypatch):
        handler = orders_module.OrderHandlers()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="checkout_choose_reward")
        context = make_context()
        context.user_data["selected_address_id"] = 5

        # subtotal 24000, published by the server (see served_cart)
        cart_data = served_cart(({"name": "Bottle", "current_price": 12000}, 2))
        rewards_data = {"data": {
            "user_points_balance": 500,
            "rewards": [
                {"id": 1, "name": "Affordable", "points_cost": 100,
                 "can_redeem": True, "points_needed": 0, "min_order_value": 0},
                {"id": 2, "name": "Order Locked", "points_cost": 50,
                 "can_redeem": True, "points_needed": 0, "min_order_value": 999999},
            ],
        }}
        captured = {}

        def _picker(rewards, language):
            captured["rewards"] = rewards
            return "picker-kbd"

        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(eligibility, "is_loyalty_eligible", AsyncMock(return_value=True))
        monkeypatch.setattr(orders_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(orders_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(
            orders_module.OrderKeyboards, "checkout_reward_picker", staticmethod(_picker)
        )
        monkeypatch.setattr(orders_module, "api_client", FakeAPIClientContext(
            get_cart=_resp(success=True, data=cart_data),
            get_loyalty_rewards=_resp(success=True, data=rewards_data),
        ))

        await handler.checkout_choose_reward(update, context)

        ids = [r["id"] for r in captured["rewards"]]
        assert ids == [1]  # only affordable + in-budget reward becomes a button
        text = update.callback_query.edit_message_text.await_args.kwargs["text"]
        assert "telegram.loyalty.balance_header:en" in text
        # The min-order-locked reward still appears as a text lock line.
        assert "telegram.loyalty.lock_add_order:en" in text

    async def test_apply_reward_stores_and_rerenders(self, monkeypatch):
        handler = orders_module.OrderHandlers()
        handler._show_order_confirmation = AsyncMock()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="checkout_apply_reward_7")
        context = make_context()

        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(eligibility, "is_loyalty_eligible", AsyncMock(return_value=True))
        monkeypatch.setattr(orders_module.i18n, "get", _i18n_get)

        await handler.checkout_apply_reward(update, context)

        assert context.user_data["selected_reward_id"] == 7
        handler._show_order_confirmation.assert_awaited_once_with(update, context)

    async def test_remove_reward_clears_and_rerenders(self, monkeypatch):
        handler = orders_module.OrderHandlers()
        handler._show_order_confirmation = AsyncMock()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="checkout_remove_reward")
        context = make_context()
        context.user_data["selected_reward_id"] = 7

        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(eligibility, "is_loyalty_eligible", AsyncMock(return_value=True))
        monkeypatch.setattr(orders_module.i18n, "get", _i18n_get)

        await handler.checkout_remove_reward(update, context)

        assert "selected_reward_id" not in context.user_data
        handler._show_order_confirmation.assert_awaited_once_with(update, context)


@pytest.mark.unit
@pytest.mark.anyio
class TestLoyaltyHandlerWave2:
    async def test_loyalty_menu_returns_when_user_missing(self, monkeypatch):
        handler = loyalty_module.LoyaltyHandlers()
        update = DummyUpdate()
        context = make_context()

        monkeypatch.setattr(loyalty_module, "user_middleware", AsyncMock(return_value=None))

        result = await handler.loyalty_menu(update, context)

        assert result is None
        assert update.message.reply_text.await_count == 0

    async def test_loyalty_history_calls_api_error_handler_on_failure(self, monkeypatch):
        handler = loyalty_module.LoyaltyHandlers()
        handler._handle_api_error = AsyncMock()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="loyalty_history")
        context = make_context()

        monkeypatch.setattr(loyalty_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(eligibility, "is_loyalty_eligible", AsyncMock(return_value=True))
        monkeypatch.setattr(loyalty_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(loyalty_module, "api_client", FakeAPIClientContext(
            get_loyalty_history=_resp(success=False, error="history failed"),
        ))

        await handler.loyalty_history(update, context)
        handler._handle_api_error.assert_awaited_once_with(update, "history failed", "en")

    async def test_redeem_reward_malformed_callback_hits_generic_error_handler(self, monkeypatch):
        handler = loyalty_module.LoyaltyHandlers()
        handler._handle_error = AsyncMock()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="redeem_bad")
        context = make_context()

        monkeypatch.setattr(loyalty_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(eligibility, "is_loyalty_eligible", AsyncMock(return_value=True))

        await handler.redeem_reward(update, context)
        handler._handle_error.assert_awaited_once()
        assert handler._handle_error.await_args.args == (update,)
        kwargs = handler._handle_error.await_args.kwargs
        assert isinstance(kwargs.get("exc"), ValueError)
        assert kwargs.get("operation") == "redeem_reward"

    def test_loyalty_guide_url_none_for_localhost(self, monkeypatch):
        # Telegram rejects localhost URL buttons ("wrong http url") — guard returns None.
        monkeypatch.setenv("COMPANY_WEBSITE", "http://localhost:5000")
        assert loyalty_module.LoyaltyHandlers._loyalty_guide_url("ru") is None

    def test_loyalty_guide_url_built_for_public_host(self, monkeypatch):
        monkeypatch.setenv("COMPANY_WEBSITE", "https://aqua-element.uz")
        # uz (default language) keeps the path clean; other languages get ?lang=.
        assert loyalty_module.LoyaltyHandlers._loyalty_guide_url("uz") == "https://aqua-element.uz/loyalty-guide"
        assert (
            loyalty_module.LoyaltyHandlers._loyalty_guide_url("ru")
            == "https://aqua-element.uz/loyalty-guide?lang=ru"
        )

    async def _render_loyalty_menu(self, monkeypatch):
        handler = loyalty_module.LoyaltyHandlers()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="loyalty_menu")
        context = make_context()

        monkeypatch.setattr(loyalty_module, "user_middleware", AsyncMock(return_value={"id": 1}))
        monkeypatch.setattr(loyalty_module.i18n, "get_user_language", AsyncMock(return_value="ru"))
        monkeypatch.setattr(eligibility, "is_loyalty_eligible", AsyncMock(return_value=True))
        monkeypatch.setattr(loyalty_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(loyalty_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(loyalty_module, "api_client", FakeAPIClientContext(
            get_loyalty_points=_resp(success=True, data={"current_balance": 100, "lifetime_earned": 200}),
            get_loyalty_rewards=_resp(success=True, data={"rewards": []}),
        ))

        await handler.loyalty_menu(update, context)
        update.callback_query.edit_message_text.assert_awaited_once()
        markup = update.callback_query.edit_message_text.await_args.kwargs["reply_markup"]
        return [btn for row in markup.inline_keyboard for btn in row]

    async def test_loyalty_menu_omits_guide_button_on_localhost(self, monkeypatch):
        # The original crash: a localhost URL button failed the whole edit_message_text call.
        monkeypatch.setenv("COMPANY_WEBSITE", "http://localhost:5000")
        buttons = await self._render_loyalty_menu(monkeypatch)
        assert all(btn.url is None for btn in buttons)

    async def test_loyalty_menu_includes_guide_button_on_public_host(self, monkeypatch):
        monkeypatch.setenv("COMPANY_WEBSITE", "https://aqua-element.uz")
        buttons = await self._render_loyalty_menu(monkeypatch)
        assert any(btn.url == "https://aqua-element.uz/loyalty-guide?lang=ru" for btn in buttons)

    async def test_loyalty_history_renders_signed_amounts_without_double_minus(self, monkeypatch):
        handler = loyalty_module.LoyaltyHandlers()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="loyalty_history")
        context = make_context()

        monkeypatch.setattr(loyalty_module.i18n, "get_user_language", AsyncMock(return_value="ru"))
        monkeypatch.setattr(eligibility, "is_loyalty_eligible", AsyncMock(return_value=True))
        monkeypatch.setattr(loyalty_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(loyalty_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(loyalty_module, "api_client", FakeAPIClientContext(
            get_loyalty_history=_resp(success=True, data={"items": [
                # Referral bonus — must read as "referral", not the old "other".
                {"created_at": "2026-06-19", "points": 500, "transaction_type": "bonus",
                 "action_type": "referral", "description": "Referral bonus for user #75"},
                # Redemptions are stored negative — must render a single minus.
                {"created_at": "2026-06-16", "points": -4000, "transaction_type": "redeemed",
                 "description": "Redeemed reward: 19 litrlik suv"},
                {"created_at": "2026-05-23", "points": 540, "transaction_type": "earned",
                 "action_type": "purchase"},
                # Refund is a positive adjustment — must read as a green credit, not yellow.
                {"created_at": "2026-06-16", "points": 2500, "transaction_type": "adjustment",
                 "action_type": "reward_refund", "order_id": 144},
            ]}),
        ))

        await handler.loyalty_history(update, context)
        text = update.callback_query.edit_message_text.await_args.kwargs["text"]
        # Colour/sign: credits green (with +), debits red (single minus).
        assert "--4000" not in text          # the original double-minus bug
        assert "🔴 -4000" in text
        assert "🟢 +540" in text
        assert "🟢 +500" in text
        assert "🟢 +2500" in text            # refund now reads as a credit (was yellow)
        assert "🟡" not in text              # no more confusing "other" bucket
        # Localized, category-based labels (mock i18n returns "<key>:<lang>").
        assert "telegram.loyalty.txn.referral:ru" in text
        assert "telegram.loyalty.txn.redeem_named:ru" in text
        assert "telegram.loyalty.txn.order_earn:ru" in text
        assert "telegram.loyalty.txn.refund_order:ru" in text

    async def test_loyalty_history_paginates(self, monkeypatch):
        # A history with more than one page must request the right page and render
        # prev/next navigation — not silently drop everything past the first 10.
        handler = loyalty_module.LoyaltyHandlers()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="loyalty_history_page_2")
        context = make_context()

        monkeypatch.setattr(loyalty_module.i18n, "get_user_language", AsyncMock(return_value="ru"))
        monkeypatch.setattr(eligibility, "is_loyalty_eligible", AsyncMock(return_value=True))
        monkeypatch.setattr(loyalty_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(loyalty_module, "get_auth_token", AsyncMock(return_value="jwt"))

        history_resp = _resp(success=True, data={
            "data": {"items": [
                {"created_at": "2026-06-10", "points": 120,
                 "transaction_type": "earned", "action_type": "purchase"},
            ]},
            "meta": {"page": 2, "per_page": 10, "total": 25, "pages": 3,
                     "has_next": True, "has_prev": True},
        })
        fake = FakeAPIClientContext(get_loyalty_history=history_resp)
        get_history_mock = AsyncMock(return_value=history_resp)
        fake.get_loyalty_history = get_history_mock
        monkeypatch.setattr(loyalty_module, "api_client", fake)

        await handler.loyalty_history(update, context)

        # The backend was asked for page 2 (not always page 1).
        get_history_mock.assert_awaited_once()
        assert get_history_mock.await_args.kwargs.get("page") == 2
        assert get_history_mock.await_args.kwargs.get("per_page") == 10

        text = update.callback_query.edit_message_text.await_args.kwargs["text"]
        markup = update.callback_query.edit_message_text.await_args.kwargs["reply_markup"]
        cbs = [b.callback_data for row in markup.inline_keyboard for b in row]
        assert "loyalty_history_page_1" in cbs   # prev → page 1
        assert "loyalty_history_page_3" in cbs   # next → page 3
        assert "telegram.loyalty.history_page_info:ru" in text

    async def test_referral_renders_telegram_deep_link(self, monkeypatch):
        handler = loyalty_module.LoyaltyHandlers()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="loyalty_referral")
        context = make_context()
        context.bot.username = "aqua_element_bot"  # runtime truth from getMe

        monkeypatch.setattr(loyalty_module.i18n, "get_user_language", AsyncMock(return_value="ru"))
        monkeypatch.setattr(eligibility, "is_loyalty_eligible", AsyncMock(return_value=True))
        monkeypatch.setattr(loyalty_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(loyalty_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(loyalty_module, "api_client", FakeAPIClientContext(
            get_referral_info=_resp(success=True, data={
                "referral_code": "REFMM8UQU",
                "referral_link": "https://aqua-element.uz/register?ref=REFMM8UQU",
                "statistics": {"total_referrals": 0},
            }),
        ))

        await handler.loyalty_referral(update, context)
        text = update.callback_query.edit_message_text.await_args.kwargs["text"]
        assert "https://t.me/aqua_element_bot?start=ref_REFMM8UQU" in text
        assert "http:///" not in text

    def test_referral_deep_link_falls_back_when_username_missing(self):
        # No username resolvable -> None, so the handler can use the web link.
        ctx = SimpleNamespace(bot=SimpleNamespace())
        assert loyalty_module.LoyaltyHandlers._referral_deep_link(ctx, "REFMM8UQU") is None
        ctx2 = SimpleNamespace(bot=SimpleNamespace(username="aqua_element_bot"))
        assert loyalty_module.LoyaltyHandlers._referral_deep_link(ctx2, "") is None
        assert (
            loyalty_module.LoyaltyHandlers._referral_deep_link(ctx2, "ABC123")
            == "https://t.me/aqua_element_bot?start=ref_ABC123"
        )


@pytest.mark.unit
@pytest.mark.anyio
class TestBackToPayment:
    def test_order_confirmation_keyboard_has_back_to_payment_both_branches(self, monkeypatch):
        monkeypatch.setattr(orders_module.i18n, "get", _i18n_get)

        meets = orders_module.OrderKeyboards.order_confirmation(
            "en", meets_minimum=True, has_reward=False
        )
        cbs = [b.callback_data for row in meets.inline_keyboard for b in row]
        assert "back_to_payment" in cbs

        below = orders_module.OrderKeyboards.order_confirmation(
            "en", meets_minimum=False, has_reward=False
        )
        cbs2 = [b.callback_data for row in below.inline_keyboard for b in row]
        assert "back_to_payment" in cbs2

    async def test_back_to_payment_rerenders_picker_and_preserves_reward(self, monkeypatch):
        handler = orders_module.OrderHandlers()
        handler._show_payment_picker = AsyncMock()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="back_to_payment")
        context = make_context()
        context.user_data["selected_address_id"] = 42
        context.user_data["selected_reward_id"] = 7

        await handler.back_to_payment(update, context)

        handler._show_payment_picker.assert_awaited_once_with(update, context, 42)
        # The reward selection must survive the round-trip back to payment.
        assert context.user_data["selected_reward_id"] == 7
