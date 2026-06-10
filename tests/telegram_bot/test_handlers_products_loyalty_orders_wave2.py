"""Second-wave Telegram handler coverage for products/orders/loyalty."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from handlers import loyalty as loyalty_module
from handlers import orders as orders_module
from handlers import products as products_module
from tests.telegram_bot.helpers import DummyCallbackQuery, DummyUpdate, FakeAPIClientContext, make_context


def _resp(success=True, data=None, error=None, status_code=200):
    return SimpleNamespace(success=success, data=data or {}, error=error, status_code=status_code)


def _i18n_get(key, language, *args, **kwargs):
    return f"{key}:{language}"


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

        handler.show_cart.assert_awaited_once_with(update, context)
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

        def _cart_actions(lang, cart_is_empty, meets_minimum):
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

        def _cart_actions(lang, cart_is_empty, meets_minimum):
            captured["args"] = (lang, cart_is_empty, meets_minimum)
            return "cart-kbd"

        cart_payload = {
            "data": {
                "cart": {
                    "cart_items": [
                        {"product": {"name": "Bottle", "current_price": 5000}, "quantity": 2},
                    ]
                }
            }
        }
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

        cart_payload = {
            "data": {
                "cart": {
                    "cart_items": [
                        {"product": {"name": "Bottle", "current_price": 25000}, "quantity": 1},
                    ],
                    "cod_prepayment": {
                        "available_balance": 40000,
                        "potential_applied_amount": 25000,
                        "estimated_payable_after_prepayment": 0,
                    },
                }
            }
        }
        monkeypatch.setattr(products_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(products_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(products_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(products_module, "api_client", FakeAPIClientContext(get_cart=_resp(success=True, data=cart_payload)))

        await handler.show_cart(update, context)

        text = update.callback_query.edit_message_text.await_args.kwargs["text"]
        assert "telegram.cart.cod_prepaid_balance:en" in text
        assert "telegram.cart.cod_prepaid_auto_applied_next:en" in text

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
        handler.user_repo = SimpleNamespace(update_user_state=AsyncMock())
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
        handler.user_repo.update_user_state.assert_awaited_once_with(update.effective_user.id, {})


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
        monkeypatch.setattr(orders_module.MenuKeyboards, "main_menu", lambda _l: "menu-kbd")
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

    async def test_show_order_confirmation_auth_error(self, monkeypatch):
        handler = orders_module.OrderHandlers()
        handler._handle_auth_error = AsyncMock()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="payment_card")
        context = make_context()

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

        cart_data = {
            "data": {
                "cart": {
                    "cart_items": [
                        {"product": {"name": "Bottle", "current_price": 12000}, "quantity": 2},
                    ]
                }
            }
        }
        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(orders_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(orders_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(orders_module.OrderKeyboards, "order_confirmation", lambda *_a, **_k: "confirm-kbd")
        monkeypatch.setattr(orders_module, "api_client", FakeAPIClientContext(get_cart=_resp(success=True, data=cart_data)))

        await handler._show_order_confirmation(update, context)

        update.callback_query.edit_message_text.assert_awaited_once()
        text = update.callback_query.edit_message_text.await_args.kwargs["text"]
        assert "telegram.orders.confirmation_title:en" in text
        assert "telegram.orders.items_header:en" in text
        assert "telegram.orders.payment_info:en" in text
        update.callback_query.answer.assert_awaited_once()

    async def test_show_order_confirmation_includes_cod_prepayment_summary(self, monkeypatch):
        handler = orders_module.OrderHandlers()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="payment_cash")
        context = make_context()
        context.user_data["selected_address_id"] = 5
        context.user_data["selected_payment_method"] = "cash"

        cart_data = {
            "data": {
                "cart": {
                    "cart_items": [
                        {"product": {"name": "Bottle", "current_price": 12000}, "quantity": 2},
                    ],
                    "cod_prepayment": {
                        "available_balance": 40000,
                        "potential_applied_amount": 24000,
                        "estimated_payable_after_prepayment": 0,
                    },
                }
            }
        }
        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(orders_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(orders_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(orders_module.OrderKeyboards, "order_confirmation", lambda *_a, **_k: "confirm-kbd")
        monkeypatch.setattr(orders_module, "api_client", FakeAPIClientContext(get_cart=_resp(success=True, data=cart_data)))

        await handler._show_order_confirmation(update, context)

        text = update.callback_query.edit_message_text.await_args.kwargs["text"]
        assert "telegram.orders.cod_prepaid_balance:en" in text
        assert "telegram.orders.cod_prepaid_auto_applied:en" in text
        assert "telegram.orders.cod_estimated_payable:en" in text


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
        monkeypatch.setattr(loyalty_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(loyalty_module, "api_client", FakeAPIClientContext(
            get_loyalty_history=_resp(success=False, error="history failed"),
        ))

        await handler.loyalty_history(update, context)
        handler._handle_api_error.assert_awaited_once_with(update, "history failed", "en")

    async def test_redeem_reward_failure_calls_api_error_handler(self, monkeypatch):
        handler = loyalty_module.LoyaltyHandlers()
        handler._handle_api_error = AsyncMock()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="redeem_44")
        context = make_context()

        monkeypatch.setattr(loyalty_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(loyalty_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(loyalty_module, "api_client", FakeAPIClientContext(
            redeem_reward=_resp(success=False, error="not enough points"),
        ))

        await handler.redeem_reward(update, context)
        handler._handle_api_error.assert_awaited_once_with(update, "not enough points", "en")

    async def test_redeem_reward_malformed_callback_hits_generic_error_handler(self, monkeypatch):
        handler = loyalty_module.LoyaltyHandlers()
        handler._handle_error = AsyncMock()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="redeem_bad")
        context = make_context()

        monkeypatch.setattr(loyalty_module.i18n, "get_user_language", AsyncMock(return_value="en"))

        await handler.redeem_reward(update, context)
        handler._handle_error.assert_awaited_once()
        assert handler._handle_error.await_args.args == (update,)
        kwargs = handler._handle_error.await_args.kwargs
        assert isinstance(kwargs.get("exc"), ValueError)
        assert kwargs.get("operation") == "redeem_reward"
