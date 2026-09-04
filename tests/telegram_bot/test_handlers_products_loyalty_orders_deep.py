"""Deeper handler coverage for products/orders/loyalty telegram flows."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import eligibility
from handlers import loyalty as loyalty_module
from handlers import orders as orders_module
from handlers import products as products_module
from tests.telegram_bot.helpers import DummyCallbackQuery, DummyUpdate, FakeAPIClientContext, make_context
# `served_cart`/`served_estimate` are the SINGLE fabrication point for a
# `GET /cart` + `POST cart/estimate` pair shaped like the real
# `CartService` (see their docstrings in wave2). Reused here rather than
# re-invented so the checkout quote fixtures never drift between test files.
from tests.telegram_bot.test_handlers_products_loyalty_orders_wave2 import served_cart, served_estimate


def _resp(success=True, data=None, error=None, status_code=200):
    return SimpleNamespace(success=success, data=data or {}, error=error, status_code=status_code)


def _i18n_get(key, language, *args, **kwargs):
    return f"{key}:{language}"


# The unrestricted /payments/methods payload shape, matching
# test_address_handler_stores_selected_address's inline fixture. Shared by the
# Plan E checkout tests, which need the SAME success payload from two different
# awaits of the same mock.
_PAYMENT_METHODS_OK = {
    "data": {
        "available_methods": [
            {"method": "cash", "is_active": True},
            {"method": "click", "is_active": True},
        ],
        "payment_restrictions": {
            "cod_restricted": False,
            "active_cod_debt_count": 0,
        },
    }
}


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

        # 2+ categories so the products_menu renders the category picker
        # (1-category would trigger the single-category skip and route into
        # _render_products_in_category instead — exercised separately).
        payload = {"categories": [{"id": 1, "name": "Water"}, {"id": 2, "name": "Snacks"}]}
        monkeypatch.setattr(products_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(products_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(products_module, "get_auth_token", AsyncMock(return_value="jwt"))
        keyboard = SimpleNamespace(inline_keyboard=[[{"text": "Water"}]])
        monkeypatch.setattr(products_module.ProductKeyboards, "product_categories", lambda *_a, **_k: keyboard)
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

        # 2+ categories so we exercise the category-picker fallback path
        # (1-category would skip past it via _render_products_in_category).
        payload = {"categories": [{"id": 1, "name": "Water"}, {"id": 2, "name": "Snacks"}]}
        monkeypatch.setattr(products_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(products_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(products_module, "get_auth_token", AsyncMock(return_value="jwt"))
        keyboard = SimpleNamespace(inline_keyboard=[[{"text": "Water"}]])
        monkeypatch.setattr(products_module.ProductKeyboards, "product_categories", lambda *_a, **_k: keyboard)
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
                get_cart=_resp(success=True, data={"data": {"cart": {"cart_items": []}}}),
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
                get_cart=_resp(success=True, data={"data": {"cart": {"cart_items": []}}}),
                add_to_cart=_resp(
                    success=True,
                    data={"data": {"cart": {"cart_items": [{"product_id": 9, "quantity": 2}]}}},
                ),
            ),
        )

        await handler.add_to_cart(update, context)

        update.callback_query.message.delete.assert_awaited_once()
        update.callback_query.message.reply_text.assert_awaited_once_with(
            text="🛒 Water\n\ntelegram.quantity:en: 2\ntelegram.total:en: 30,000 UZS",
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
        handler._handle_error.assert_awaited_once()
        assert handler._handle_error.await_args.args == (update,)
        kwargs = handler._handle_error.await_args.kwargs
        assert isinstance(kwargs.get("exc"), ValueError)
        assert kwargs.get("operation") == "product_details"


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

    async def test_order_details_merges_free_reward_into_paid_line(self, monkeypatch):
        """A free reward line for a purchased product is merged additively
        ('+1 free 🎁') into that product's line, not shown as a duplicate row."""
        handler = orders_module.OrderHandlers()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="order_7")
        context = make_context()

        order = {
            "id": 7, "order_number": "TG_TEST", "created_at": "2026-06-15T00:00:00",
            "total_amount": 36000, "status": "confirmed",
            "order_items": [
                {"product_id": 2, "product_name": "19 litrlik suv", "quantity": 2,
                 "unit_price": 18000, "total_price": 36000, "is_reward": False},
                {"product_id": 2, "product_name": "19 litrlik suv", "quantity": 1,
                 "unit_price": 0, "total_price": 0, "is_reward": True},
            ],
        }
        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(orders_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(orders_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(orders_module.OrderKeyboards, "order_details", lambda *_a, **_k: "kbd")
        monkeypatch.setattr(
            orders_module, "api_client",
            FakeAPIClientContext(get_order=_resp(success=True, data={"data": {"order": order, "delivery": None}})),
        )

        await handler.order_details(update, context)

        text = update.callback_query.edit_message_text.await_args.kwargs["text"]
        # Merged into a single product line (not two identical "19 litrlik suv" rows).
        assert text.count("19 litrlik suv") == 1
        # The free-bonus marker is present.
        assert "🎁" in text

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

    async def test_checkout_handler_no_addresses_arms_location_keyboard(self, monkeypatch):
        handler = orders_module.OrderHandlers()
        handler.user_repo = SimpleNamespace(update_user_state=AsyncMock())
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="checkout")
        context = make_context()

        monkeypatch.setattr(orders_module, "user_middleware", AsyncMock(return_value={"id": 1}))
        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(orders_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(orders_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(orders_module.ProfileKeyboards, "location_request", lambda _lang, **_kw: "loc-kbd")
        monkeypatch.setattr(
            orders_module,
            "api_client",
            FakeAPIClientContext(get_user_addresses=_resp(success=True, data={"data": {"addresses": []}})),
        )

        await handler.checkout_handler(update, context)

        # Zero-address checkout now arms the location keyboard directly: the inline
        # card's only real button led here anyway (spec §6). The orphan
        # awaiting_input='address_location' write is gone — its only consumer
        # was WaterBusinessBot._handle_location, which is registered nowhere.
        # Assert specifically on that write rather than on call-occurrence:
        # checkout may legitimately write user state for unrelated reasons.
        for call in handler.user_repo.update_user_state.await_args_list:
            _, written_state = call.args
            assert written_state.get("awaiting_input") != "address_location"
        sent_markup = update.callback_query.message.reply_text.await_args.kwargs["reply_markup"]
        assert sent_markup == "loc-kbd"
        assert context.user_data["address_flow_origin"] == "checkout"

    async def test_address_handler_stores_selected_address(self, monkeypatch):
        handler = orders_module.OrderHandlers()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="address_55")
        context = make_context()

        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(orders_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(orders_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(orders_module.OrderKeyboards, "payment_methods", lambda _methods, _lang: "pay-kbd")
        monkeypatch.setattr(
            orders_module,
            "api_client",
            FakeAPIClientContext(
                get_payment_methods=_resp(
                    success=True,
                    data={
                        "data": {
                            "available_methods": [
                                {"method": "cash", "is_active": True},
                                {"method": "payme", "is_active": True},
                            ],
                            "payment_restrictions": {
                                "cod_restricted": False,
                                "active_cod_debt_count": 0,
                            },
                        }
                    },
                ),
                # Cash is on offer, so `_show_payment_picker` quotes it — it
                # needs a `GET /cart` response to build that quote from. An
                # empty cart (this test doesn't care about pricing) makes the
                # quote step a no-op, matching the exact text asserted below.
                get_cart=_resp(success=True, data={"data": {"cart": {"cart_items": []}}}),
            ),
        )

        await handler.address_handler(update, context)

        assert context.user_data["selected_address_id"] == 55
        update.callback_query.edit_message_text.assert_awaited_once_with(
            text="telegram.orders.select_payment:en",
            reply_markup="pay-kbd",
        )
        update.callback_query.answer.assert_awaited_once()

    async def test_address_handler_shows_cod_restriction_notice(self, monkeypatch):
        handler = orders_module.OrderHandlers()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="address_56")
        context = make_context()

        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(orders_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(orders_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(orders_module.OrderKeyboards, "payment_methods", lambda _methods, _lang: "pay-kbd")
        monkeypatch.setattr(
            orders_module,
            "api_client",
            FakeAPIClientContext(
                get_payment_methods=_resp(
                    success=True,
                    data={
                        "data": {
                            "available_methods": [
                                # Cash is COD-restricted here; click (-> "card") is the
                                # one method still on offer. "payme" is never returned by
                                # the backend (excluded per shared/payment_methods.py SSOT)
                                # so it's not a realistic fixture value.
                                {"method": "click", "is_active": True},
                            ],
                            "payment_restrictions": {
                                "cod_restricted": True,
                                "active_cod_debt_count": 2,
                            },
                        }
                    },
                )
            ),
        )

        await handler.address_handler(update, context)

        call_kwargs = update.callback_query.edit_message_text.call_args.kwargs
        # Notice text now flows through i18n; the test mock returns
        # "<key>:<language>", so we assert on the key the bot used.
        assert "telegram.orders.cod_restricted_has_debts:en" in call_kwargs["text"]

    async def test_checkout_sends_the_selected_address_to_payment_methods(self, monkeypatch):
        """Plan E R3: the place arm of the COD cap can only fire when the address
        reaches the endpoint. Without this the coworker is silently offered Cash
        and then rejected at order creation with a generic error."""
        handler = orders_module.OrderHandlers()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="address_55")
        context = make_context()

        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(orders_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(orders_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(orders_module.OrderKeyboards, "payment_methods", lambda _methods, _lang: "pay-kbd")
        # FakeAPIClientContext.get_payment_methods swallows its arguments
        # (tests/telegram_bot/helpers.py:180-181), so it cannot observe the new
        # kwarg. Bind an AsyncMock for that one method instead — the assertion
        # is on the PAYLOAD, not on call-occurrence.
        methods = AsyncMock(return_value=_resp(success=True, data=_PAYMENT_METHODS_OK))
        fake = FakeAPIClientContext()
        fake.get_payment_methods = methods
        monkeypatch.setattr(orders_module, "api_client", fake)

        await handler.address_handler(update, context)

        assert context.user_data["selected_address_id"] == 55
        assert methods.await_args.kwargs["delivery_address_id"] == 55

    async def test_checkout_survives_a_stale_selected_address(self, monkeypatch):
        """🔴 THE UNGATED REGRESSION. Do not delete this test.

        GET /payments/methods returns 400 for a delivery_address_id that is
        foreign OR NO LONGER EXISTS (business_app/api/payments.py:158-172 ->
        order_service.py:547-553). Today the bot never sends the parameter, so
        this cannot happen; after this task it can, Task 6 is UNGATED (E11), and
        orders.py:742-744 aborts the WHOLE payment screen on any non-success.

        Reachable without malice: the address is deleted between selection and
        checkout, or Quick Order pre-fills selected_address_id from a prior
        order whose address is gone (_show_payment_picker docstring, :719-724).

        Degrade, never dead-end: retry once WITHOUT the address. The place arm
        does not apply for that request -- which is exactly today's behaviour --
        and the customer still gets a payment keyboard.
        """
        handler = orders_module.OrderHandlers()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="address_55")
        context = make_context()

        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(orders_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(orders_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(orders_module.OrderKeyboards, "payment_methods", lambda _methods, _lang: "pay-kbd")
        # First await = the 400 the endpoint returns for a stale/foreign address;
        # second await = the normal success payload the address-less retry gets.
        methods = AsyncMock(
            side_effect=[
                _resp(success=False, error="Address not found", status_code=400),
                _resp(success=True, data=_PAYMENT_METHODS_OK),
            ]
        )
        # `_PAYMENT_METHODS_OK` offers cash, so the retry's payment screen
        # quotes it — real `get_cart` + `estimate_cart` responses so that
        # quote step is exercised for real rather than left to fail.
        cart_data = served_cart(({"name": "Bottle", "current_price": 12000}, 2))
        fake = FakeAPIClientContext(
            get_cart=_resp(success=True, data=cart_data),
            estimate_cart=_resp(success=True, data=served_estimate(cart_data, payment_method="cash")),
        )
        fake.get_payment_methods = methods
        monkeypatch.setattr(orders_module, "api_client", fake)

        await handler.address_handler(update, context)

        assert methods.await_count == 2
        # The retry drops the address entirely — it does not send None.
        assert "delivery_address_id" not in methods.await_args_list[1].kwargs
        # And the customer reached the payment keyboard, not an error.
        update.callback_query.edit_message_text.assert_awaited()


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
        monkeypatch.setattr(eligibility, "is_loyalty_eligible", AsyncMock(return_value=True))
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
        monkeypatch.setattr(eligibility, "is_loyalty_eligible", AsyncMock(return_value=True))
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

    def test_loyalty_guide_url_is_localized(self, monkeypatch):
        monkeypatch.setenv("COMPANY_WEBSITE", "https://aqua-element.uz")
        handler = loyalty_module.LoyaltyHandlers()
        # Default language (uz) gets a clean URL; others carry an explicit ?lang=.
        assert handler._loyalty_guide_url("uz") == "https://aqua-element.uz/loyalty-guide"
        assert handler._loyalty_guide_url("ru") == "https://aqua-element.uz/loyalty-guide?lang=ru"
        assert handler._loyalty_guide_url("en") == "https://aqua-element.uz/loyalty-guide?lang=en"
        # Trailing slash on the base is normalized.
        monkeypatch.setenv("COMPANY_WEBSITE", "https://aqua-element.uz/")
        assert handler._loyalty_guide_url("uz") == "https://aqua-element.uz/loyalty-guide"

    async def test_loyalty_menu_includes_guide_url_button(self, monkeypatch):
        monkeypatch.setenv("COMPANY_WEBSITE", "https://aqua-element.uz")
        handler = loyalty_module.LoyaltyHandlers()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="menu_loyalty")
        context = make_context()

        monkeypatch.setattr(loyalty_module, "user_middleware", AsyncMock(return_value={"id": 1}))
        monkeypatch.setattr(loyalty_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(eligibility, "is_loyalty_eligible", AsyncMock(return_value=True))
        monkeypatch.setattr(loyalty_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(loyalty_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(
            loyalty_module,
            "api_client",
            FakeAPIClientContext(
                get_loyalty_points=_resp(success=True, data={"current_balance": 10, "lifetime_earned": 10}),
                get_loyalty_rewards=_resp(success=True, data={"rewards": []}),
            ),
        )

        await handler.loyalty_menu(update, context)

        markup = update.callback_query.edit_message_text.await_args.kwargs["reply_markup"]
        urls = [b.url for row in markup.inline_keyboard for b in row if b.url]
        assert "https://aqua-element.uz/loyalty-guide?lang=en" in urls

    async def test_loyalty_menu_supports_api_envelope_payloads(self, monkeypatch):
        handler = loyalty_module.LoyaltyHandlers()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="menu_loyalty")
        context = make_context()

        monkeypatch.setattr(loyalty_module, "user_middleware", AsyncMock(return_value={"id": 1}))
        monkeypatch.setattr(loyalty_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(eligibility, "is_loyalty_eligible", AsyncMock(return_value=True))
        monkeypatch.setattr(loyalty_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(loyalty_module, "get_auth_token", AsyncMock(return_value="jwt"))
        monkeypatch.setattr(
            loyalty_module,
            "api_client",
            FakeAPIClientContext(
                get_loyalty_points=_resp(
                    success=True,
                    data={"data": {"points_balance": 120, "qualifying_points": 999}},
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
        monkeypatch.setattr(eligibility, "is_loyalty_eligible", AsyncMock(return_value=True))
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
        monkeypatch.setattr(eligibility, "is_loyalty_eligible", AsyncMock(return_value=True))
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
        monkeypatch.setattr(eligibility, "is_loyalty_eligible", AsyncMock(return_value=True))
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

    async def test_redeem_reward_stores_selection_and_returns_to_menu(self, monkeypatch):
        """Phase 3 apply-at-checkout: tapping redeem_<id> just stores the reward
        id in conversation state and confirms it will be applied at checkout —
        no standalone redeem API call (that route was removed)."""
        handler = loyalty_module.LoyaltyHandlers()
        handler.loyalty_menu = AsyncMock()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="redeem_42")
        context = make_context()

        monkeypatch.setattr(loyalty_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(eligibility, "is_loyalty_eligible", AsyncMock(return_value=True))
        monkeypatch.setattr(loyalty_module.i18n, "get", _i18n_get)

        await handler.redeem_reward(update, context)

        assert context.user_data["selected_reward_id"] == 42
        update.callback_query.answer.assert_awaited_once_with(
            "telegram.loyalty.reward_selected:en", show_alert=True
        )
        handler.loyalty_menu.assert_awaited_once_with(update, context)


@pytest.mark.unit
class TestLoyaltyHistoryDisplay:
    """Pure helpers behind the AquaCoins history view.

    Color is by SIGN of the amount (credit = green, debit = red), mirroring the
    admin UI; the label is a localized category derived from action_type /
    transaction_type, never the raw English description.
    """

    # --- _signed_amount: color by sign ---
    def test_signed_amount_credit_is_green(self):
        assert loyalty_module.LoyaltyHandlers._signed_amount(540) == ("🟢", "+540")

    def test_signed_amount_debit_is_red(self):
        assert loyalty_module.LoyaltyHandlers._signed_amount(-4000) == ("🔴", "-4000")

    def test_refund_credit_is_green_not_yellow(self):
        # A refund is a positive ADJUSTMENT; it must read as a credit (green),
        # fixing the old behaviour where it showed yellow ("other").
        assert loyalty_module.LoyaltyHandlers._signed_amount(4000)[0] == "🟢"

    def test_signed_amount_handles_non_int(self):
        assert loyalty_module.LoyaltyHandlers._signed_amount(None) == ("🟢", "+0")

    # --- _transaction_category: stable category + format args ---
    def test_category_referral(self):
        cat, fmt = loyalty_module.LoyaltyHandlers._transaction_category(
            {"transaction_type": "bonus", "action_type": "referral",
             "description": "Referral bonus for user #75"}
        )
        assert cat == "referral"
        assert fmt == {}

    def test_category_welcome(self):
        cat, _ = loyalty_module.LoyaltyHandlers._transaction_category(
            {"transaction_type": "bonus", "action_type": "welcome_bonus"})
        assert cat == "welcome"

    def test_category_birthday(self):
        cat, _ = loyalty_module.LoyaltyHandlers._transaction_category(
            {"transaction_type": "bonus", "action_type": "birthday_bonus"})
        assert cat == "birthday"

    def test_category_streak(self):
        cat, _ = loyalty_module.LoyaltyHandlers._transaction_category(
            {"transaction_type": "earned", "action_type": "streak_bonus"})
        assert cat == "streak"

    def test_category_purchase_is_order_earn(self):
        cat, fmt = loyalty_module.LoyaltyHandlers._transaction_category(
            {"transaction_type": "earned", "action_type": "purchase",
             "description": "Order #TG_000066_26"})
        assert cat == "order_earn"
        assert fmt == {}

    def test_category_surprise_reward_is_not_order_earn(self):
        # type=earned but action=surprise_reward → its own label, not order earnings.
        cat, fmt = loyalty_module.LoyaltyHandlers._transaction_category(
            {"transaction_type": "earned", "action_type": "surprise_reward",
             "description": "Surprise Reward! Thanks for being loyal 💙"})
        assert cat == "surprise"
        assert fmt == {}

    def test_category_redeem_named_extracts_reward(self):
        cat, fmt = loyalty_module.LoyaltyHandlers._transaction_category(
            {"transaction_type": "redeemed",
             "description": "Redeemed reward: 19 litrlik suv"}
        )
        assert cat == "redeem_named"
        assert fmt == {"name": "19 litrlik suv"}

    def test_category_redeem_unknown_description_falls_back(self):
        cat, fmt = loyalty_module.LoyaltyHandlers._transaction_category(
            {"transaction_type": "redeemed", "description": "something else"})
        assert cat == "redeem"
        assert fmt == {}

    def test_category_refund_with_order(self):
        cat, fmt = loyalty_module.LoyaltyHandlers._transaction_category(
            {"transaction_type": "adjustment", "action_type": "reward_refund",
             "order_id": 141, "description": "Refund of redeemed reward (order #141)"}
        )
        assert cat == "refund_order"
        assert fmt == {"order_id": 141}

    def test_category_refund_without_order(self):
        cat, fmt = loyalty_module.LoyaltyHandlers._transaction_category(
            {"transaction_type": "adjustment", "action_type": "reward_refund"})
        assert cat == "refund"
        assert fmt == {}

    def test_category_order_edit_is_adjustment(self):
        cat, _ = loyalty_module.LoyaltyHandlers._transaction_category(
            {"transaction_type": "adjustment", "action_type": "order_edit_award"})
        assert cat == "adjustment"

    def test_category_unknown_falls_back_to_other(self):
        cat, fmt = loyalty_module.LoyaltyHandlers._transaction_category(
            {"transaction_type": "weird", "description": "x"})
        assert cat == "other"
        assert fmt == {}


@pytest.mark.unit
class TestLoyaltyHistoryPagination:
    """Page parsing + nav-button construction for the paginated history."""

    # --- _parse_history_page: page lives in the callback_data ---
    def test_base_callback_is_page_one(self):
        assert loyalty_module.LoyaltyHandlers._parse_history_page("loyalty_history") == 1

    def test_numbered_callback(self):
        assert loyalty_module.LoyaltyHandlers._parse_history_page("loyalty_history_page_4") == 4

    def test_zero_and_garbage_default_to_one(self):
        assert loyalty_module.LoyaltyHandlers._parse_history_page("loyalty_history_page_0") == 1
        assert loyalty_module.LoyaltyHandlers._parse_history_page("garbage") == 1
        assert loyalty_module.LoyaltyHandlers._parse_history_page(None) == 1

    # --- _history_nav_buttons: only the arrows that apply, + Back ---
    @staticmethod
    def _callbacks(rows):
        return [b.get("callback_data") for row in rows for b in row]

    def _nav(self, page, pages, monkeypatch):
        monkeypatch.setattr(loyalty_module.i18n, "get", _i18n_get)
        return loyalty_module.LoyaltyHandlers._history_nav_buttons(page, pages, "en")

    def test_first_page_has_next_only(self, monkeypatch):
        cbs = self._callbacks(self._nav(1, 3, monkeypatch))
        assert "loyalty_history_page_2" in cbs          # next
        assert not any(c == "loyalty_history_page_0" for c in cbs)  # no prev

    def test_middle_page_has_both(self, monkeypatch):
        cbs = self._callbacks(self._nav(2, 3, monkeypatch))
        assert "loyalty_history_page_1" in cbs          # prev
        assert "loyalty_history_page_3" in cbs          # next

    def test_last_page_has_prev_only(self, monkeypatch):
        cbs = self._callbacks(self._nav(3, 3, monkeypatch))
        assert "loyalty_history_page_2" in cbs          # prev
        assert "loyalty_history_page_4" not in cbs      # no next past the end

    def test_single_page_has_no_arrows_but_keeps_back(self, monkeypatch):
        cbs = self._callbacks(self._nav(1, 1, monkeypatch))
        assert all("loyalty_history_page_" not in (c or "") for c in cbs)
        assert "back_to_main" in cbs
