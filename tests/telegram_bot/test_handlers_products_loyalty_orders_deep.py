"""Deeper handler coverage for products/orders/loyalty telegram flows."""

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
class TestProductHandlerDeepFlows:
    async def test_products_menu_handles_auth_error(self, monkeypatch):
        handler = products_module.ProductHandlers()
        handler._handle_auth_error = AsyncMock()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="menu_products")
        context = make_context()

        monkeypatch.setattr(products_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(products_module, "get_auth_token", AsyncMock(return_value=None))
        monkeypatch.setattr(products_module, "api_client", FakeAPIClientContext())

        await handler.products_menu(update, context)
        handler._handle_auth_error.assert_awaited_once_with(update, "en")

    async def test_products_menu_supports_flat_categories_payload(self, monkeypatch):
        handler = products_module.ProductHandlers()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="menu_products")
        context = make_context()

        payload = {"categories": [{"id": 1, "name": "Water"}]}
        monkeypatch.setattr(products_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(products_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(products_module, "get_auth_token", AsyncMock(return_value="jwt"))
        keyboard = SimpleNamespace(inline_keyboard=[[{"text": "Water"}]])
        monkeypatch.setattr(products_module.ProductKeyboards, "product_categories", lambda _c, _l: keyboard)
        monkeypatch.setattr(
            products_module,
            "api_client",
            FakeAPIClientContext(get_product_categories=_resp(success=True, data=payload)),
        )

        await handler.products_menu(update, context)

        update.callback_query.edit_message_text.assert_awaited_once_with(
            text="telegram.menu.products:en",
            reply_markup=keyboard,
        )

    async def test_products_menu_fallbacks_when_edit_fails(self, monkeypatch):
        handler = products_module.ProductHandlers()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="menu_products")
        update.callback_query.edit_message_text = AsyncMock(side_effect=RuntimeError("edit failed"))
        context = make_context()

        payload = {"categories": [{"id": 1, "name": "Water"}]}
        monkeypatch.setattr(products_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(products_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(products_module, "get_auth_token", AsyncMock(return_value="jwt"))
        keyboard = SimpleNamespace(inline_keyboard=[[{"text": "Water"}]])
        monkeypatch.setattr(products_module.ProductKeyboards, "product_categories", lambda _c, _l: keyboard)
        monkeypatch.setattr(
            products_module,
            "api_client",
            FakeAPIClientContext(get_product_categories=_resp(success=True, data=payload)),
        )

        await handler.products_menu(update, context)

        update.callback_query.message.delete.assert_awaited_once()
        update.callback_query.message.reply_text.assert_awaited_once_with(
            text="telegram.menu.products:en",
            reply_markup=keyboard,
        )

    async def test_category_handler_empty_category(self, monkeypatch):
        handler = products_module.ProductHandlers()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="category_5")
        context = make_context()

        monkeypatch.setattr(products_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(products_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(products_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(products_module.MenuKeyboards, "back_button", lambda _l: "back-kbd")
        monkeypatch.setattr(
            products_module,
            "api_client",
            FakeAPIClientContext(
                get_products=_resp(success=True, data={"data": {"items": []}, "meta": {"pages": 1}})
            ),
        )

        await handler.category_handler(update, context)

        update.callback_query.edit_message_text.assert_awaited_once_with(
            text="telegram.products.category_empty:en",
            reply_markup="back-kbd",
        )
        update.callback_query.answer.assert_awaited_once()

    async def test_quantity_handler_invalid_action(self, monkeypatch):
        handler = products_module.ProductHandlers()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="qty_bad_10_2")
        context = make_context()

        monkeypatch.setattr(products_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(products_module.i18n, "get", _i18n_get)

        await handler.quantity_handler(update, context)

        update.callback_query.answer.assert_awaited_once_with("telegram.products.invalid_action:en")

    async def test_quantity_handler_success_updates_cart(self, monkeypatch):
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
                get_product=_resp(success=True, data={"data": {"product": {"name": "Water", "pricing": {"base_price": 15000}}}}),
                update_cart_item=_resp(success=True),
            ),
        )

        await handler.quantity_handler(update, context)

        update.callback_query.edit_message_text.assert_awaited_once()
        update.callback_query.answer.assert_awaited_once()

    async def test_add_to_cart_calls_api_error_handler_on_failure(self, monkeypatch):
        handler = products_module.ProductHandlers()
        handler._handle_api_error = AsyncMock()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="add_to_cart_9")
        context = make_context()

        monkeypatch.setattr(products_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(products_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(products_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(
            products_module,
            "api_client",
            FakeAPIClientContext(
                get_product=_resp(success=True, data={"data": {"product": {"name": "Water", "pricing": {"base_price": 15000}}}}),
                add_to_cart=_resp(success=False, error="cart failed"),
            ),
        )

        await handler.add_to_cart(update, context)
        handler._handle_api_error.assert_awaited_once_with(update, "cart failed", "en")

    async def test_add_to_cart_replaces_photo_message_when_editing_text_fails(self, monkeypatch):
        handler = products_module.ProductHandlers()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="add_to_cart_9")
        update.callback_query.edit_message_text = AsyncMock(
            side_effect=RuntimeError("There is no text in the message to edit")
        )
        update.callback_query.message.photo = [object()]
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
                    data={"data": {"product": {"name": "Water", "pricing": {"base_price": 15000}}}},
                ),
                add_to_cart=_resp(
                    success=True,
                    data={"data": {"cart": {"cart_items": [{"product_id": 9, "quantity": 2}]}}},
                ),
            ),
        )

        await handler.add_to_cart(update, context)

        update.callback_query.message.delete.assert_awaited_once()
        update.callback_query.message.reply_text.assert_awaited_once_with(
            text="🛒 Water\n\ntelegram.quantity:en: 2\ntelegram.price:en: 30,000 UZS",
            reply_markup="qty-kbd",
        )
        update.callback_query.answer.assert_awaited_once()

    async def test_product_details_handles_malformed_callback_data(self, monkeypatch):
        handler = products_module.ProductHandlers()
        handler._handle_error = AsyncMock()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="product_bad")
        context = make_context()

        monkeypatch.setattr(products_module.i18n, "get_user_language", AsyncMock(return_value="en"))

        await handler.product_details(update, context)
        handler._handle_error.assert_awaited_once_with(update)


@pytest.mark.unit
@pytest.mark.anyio
class TestOrderHandlerDeepFlows:
    async def test_order_details_api_error_path(self, monkeypatch):
        handler = orders_module.OrderHandlers()
        handler._handle_api_error = AsyncMock()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="order_7")
        context = make_context()

        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(orders_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(orders_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(
            orders_module,
            "api_client",
            FakeAPIClientContext(get_order=_resp(success=False, error="order not found")),
        )

        await handler.order_details(update, context)
        handler._handle_api_error.assert_awaited_once_with(update, "order not found", "en")

    async def test_track_order_fallback_when_no_timeline(self, monkeypatch):
        handler = orders_module.OrderHandlers()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="track_order_8")
        context = make_context()

        tracking_data = {
            "data": {
                "order": {"status": "pending", "order_number": "ORD-8"},
                "delivery": {},
                "timeline": [],
                "estimated_time_remaining": {},
            }
        }
        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(orders_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(orders_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(orders_module.OrderKeyboards, "order_tracking", lambda _id, _lang: "trk-kbd")
        monkeypatch.setattr(
            orders_module,
            "api_client",
            FakeAPIClientContext(track_order=_resp(success=True, data=tracking_data)),
        )

        await handler.track_order(update, context)

        update.callback_query.edit_message_text.assert_awaited_once()
        update.callback_query.answer.assert_awaited_once()

    async def test_track_order_does_not_include_driver_info(self, monkeypatch):
        handler = orders_module.OrderHandlers()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="track_order_9")
        context = make_context()

        tracking_data = {
            "data": {
                "order": {"status": "out_for_delivery", "order_number": "ORD-9"},
                "delivery": {"driver_name": "John Driver", "driver_phone": "+998901112233"},
                "timeline": [
                    {
                        "status": "out_for_delivery",
                        "timestamp": "2026-03-05T10:00:00+00:00",
                        "is_current": True,
                        "notes": "",
                    }
                ],
                "estimated_time_remaining": {"total_minutes": 25, "hours": 0},
            }
        }
        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(orders_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(orders_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(orders_module.OrderKeyboards, "order_tracking", lambda _id, _lang: "trk-kbd")
        monkeypatch.setattr(
            orders_module,
            "api_client",
            FakeAPIClientContext(track_order=_resp(success=True, data=tracking_data)),
        )

        await handler.track_order(update, context)

        rendered_text = update.callback_query.edit_message_text.call_args.kwargs["text"]
        assert "John Driver" not in rendered_text
        assert "+998901112233" not in rendered_text
        assert "telegram.orders.driver:en" not in rendered_text

    async def test_checkout_handler_no_addresses_sets_waiting_state(self, monkeypatch):
        handler = orders_module.OrderHandlers()
        handler.user_repo = SimpleNamespace(update_user_state=AsyncMock())
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="checkout")
        context = make_context()

        monkeypatch.setattr(orders_module, "user_middleware", AsyncMock(return_value={"id": 1}))
        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(orders_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(orders_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(orders_module.MenuKeyboards, "back_button", lambda _l: "back-kbd")
        monkeypatch.setattr(
            orders_module,
            "api_client",
            FakeAPIClientContext(get_user_addresses=_resp(success=True, data={"data": {"addresses": []}})),
        )

        await handler.checkout_handler(update, context)

        update.callback_query.edit_message_text.assert_awaited_once_with(
            text="telegram.orders.no_address_prompt:en",
            reply_markup="back-kbd",
        )
        handler.user_repo.update_user_state.assert_awaited_once_with(update.effective_user.id, {"awaiting_input": "address_location"})

    async def test_address_handler_stores_selected_address(self, monkeypatch):
        handler = orders_module.OrderHandlers()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="address_55")
        context = make_context()

        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(orders_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(orders_module.OrderKeyboards, "payment_methods", lambda _methods, _lang: "pay-kbd")

        await handler.address_handler(update, context)

        assert context.user_data["selected_address_id"] == 55
        update.callback_query.edit_message_text.assert_awaited_once_with(
            text="telegram.orders.select_payment:en",
            reply_markup="pay-kbd",
        )
        update.callback_query.answer.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.anyio
class TestLoyaltyHandlerDeepFlows:
    async def test_loyalty_menu_auth_error(self, monkeypatch):
        handler = loyalty_module.LoyaltyHandlers()
        handler._handle_auth_error = AsyncMock()
        update = DummyUpdate()
        context = make_context()

        monkeypatch.setattr(loyalty_module, "user_middleware", AsyncMock(return_value={"id": 1}))
        monkeypatch.setattr(loyalty_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(loyalty_module, "get_auth_token", AsyncMock(return_value=None))
        monkeypatch.setattr(loyalty_module, "api_client", FakeAPIClientContext())

        await handler.loyalty_menu(update, context)
        handler._handle_auth_error.assert_awaited_once_with(update, "en")

    async def test_loyalty_menu_with_rewards_message(self, monkeypatch):
        handler = loyalty_module.LoyaltyHandlers()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="menu_loyalty")
        context = make_context()

        monkeypatch.setattr(loyalty_module, "user_middleware", AsyncMock(return_value={"id": 1}))
        monkeypatch.setattr(loyalty_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(loyalty_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(loyalty_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(
            loyalty_module,
            "api_client",
            FakeAPIClientContext(
                get_loyalty_points=_resp(success=True, data={"current_balance": 120, "lifetime_earned": 999}),
                get_loyalty_rewards=_resp(
                    success=True,
                    data={"rewards": [{"name": "Reward A", "points_cost": 100}, {"name": "Reward B", "points_cost": 200}]},
                ),
            ),
        )

        await handler.loyalty_menu(update, context)

        update.callback_query.edit_message_text.assert_awaited_once()
        update.callback_query.answer.assert_awaited_once()

    async def test_loyalty_menu_supports_api_envelope_payloads(self, monkeypatch):
        handler = loyalty_module.LoyaltyHandlers()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="menu_loyalty")
        context = make_context()

        monkeypatch.setattr(loyalty_module, "user_middleware", AsyncMock(return_value={"id": 1}))
        monkeypatch.setattr(loyalty_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(loyalty_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(loyalty_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(
            loyalty_module,
            "api_client",
            FakeAPIClientContext(
                get_loyalty_points=_resp(
                    success=True,
                    data={"data": {"points_balance": 120, "lifetime_points": 999}},
                ),
                get_loyalty_rewards=_resp(
                    success=True,
                    data={"data": {"rewards": [{"name": "Reward A", "points_cost": 100}]}},
                ),
            ),
        )

        await handler.loyalty_menu(update, context)

        text = update.callback_query.edit_message_text.await_args.kwargs["text"]
        assert "120" in text
        assert "999" in text

    async def test_loyalty_history_empty(self, monkeypatch):
        handler = loyalty_module.LoyaltyHandlers()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="loyalty_history")
        context = make_context()

        monkeypatch.setattr(loyalty_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(loyalty_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(loyalty_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(loyalty_module.MenuKeyboards, "back_button", lambda _l: "back-kbd")
        monkeypatch.setattr(
            loyalty_module,
            "api_client",
            FakeAPIClientContext(get_loyalty_history=_resp(success=True, data={"history": []})),
        )

        await handler.loyalty_history(update, context)

        update.callback_query.edit_message_text.assert_awaited_once_with(
            text="telegram.loyalty.points_history:en\n\ntelegram.loyalty.no_history:en",
            reply_markup="back-kbd",
        )
        update.callback_query.answer.assert_awaited_once()

    async def test_loyalty_history_with_transactions(self, monkeypatch):
        handler = loyalty_module.LoyaltyHandlers()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="loyalty_history")
        context = make_context()

        history = [{"created_at": "2026-02-23T10:00:00Z", "points": 40, "transaction_type": "earned"}]
        monkeypatch.setattr(loyalty_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(loyalty_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(loyalty_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(loyalty_module.MenuKeyboards, "back_button", lambda _l: "back-kbd")
        monkeypatch.setattr(
            loyalty_module,
            "api_client",
            FakeAPIClientContext(get_loyalty_history=_resp(success=True, data={"history": history})),
        )

        await handler.loyalty_history(update, context)
        update.callback_query.edit_message_text.assert_awaited_once()
        update.callback_query.answer.assert_awaited_once()

    async def test_loyalty_history_supports_paginated_response_items(self, monkeypatch):
        handler = loyalty_module.LoyaltyHandlers()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="loyalty_history")
        context = make_context()

        monkeypatch.setattr(loyalty_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(loyalty_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(loyalty_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(loyalty_module.MenuKeyboards, "back_button", lambda _l: "back-kbd")
        monkeypatch.setattr(
            loyalty_module,
            "api_client",
            FakeAPIClientContext(
                get_loyalty_history=_resp(
                    success=True,
                    data={
                        "data": {
                            "items": [
                                {
                                    "created_at": "2026-02-23T10:00:00Z",
                                    "points": 55,
                                    "transaction_type": "earned",
                                }
                            ]
                        }
                    },
                )
            ),
        )

        await handler.loyalty_history(update, context)

        text = update.callback_query.edit_message_text.await_args.kwargs["text"]
        assert "+55" in text

    async def test_redeem_reward_success_calls_loyalty_menu(self, monkeypatch):
        handler = loyalty_module.LoyaltyHandlers()
        handler.loyalty_menu = AsyncMock()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="redeem_42")
        context = make_context()

        monkeypatch.setattr(loyalty_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(loyalty_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(loyalty_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(
            loyalty_module,
            "api_client",
            FakeAPIClientContext(redeem_reward=_resp(success=True)),
        )

        await handler.redeem_reward(update, context)

        update.callback_query.answer.assert_awaited_once_with("telegram.loyalty.redeem_success:en")
        handler.loyalty_menu.assert_awaited_once_with(update, context)
