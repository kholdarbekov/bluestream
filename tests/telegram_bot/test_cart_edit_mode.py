"""Deliverable B — full cart editing from the order-confirmation 'Edit' button."""

from unittest.mock import AsyncMock

import pytest

from api_client import APIResponse
from handlers import orders as orders_module
from handlers import products as products_module
from tests.telegram_bot.helpers import (
    DummyCallbackQuery,
    DummyUpdate,
    FakeAPIClientContext,
    make_context,
)


def _i18n_get(key, language, *args, **kwargs):
    return f"{key}:{language}"


def _cart_items_response():
    """A ``GET /cart`` payload shaped like ``CartService.get_cart_details``.

    The server publishes the per-line ``total_price`` and the cart ``subtotal``,
    and the cart screen READS them (sweep #7). A literal that omits them serves a
    cart the backend never serves — and, because ``cart_is_empty`` is decided by
    the subtotal, it would render this cart as empty and hide the very per-item
    controls this module exists to assert on.
    """
    return APIResponse(
        success=True,
        data={
            "data": {
                "cart": {
                    "cart_items": [
                        {
                            "product_id": 7,
                            "quantity": 3,
                            "total_price": 54000,
                            "product": {
                                "id": 7,
                                "name": "Aqua Element 18.9 l",
                                "current_price": 18000,
                                "inventory": {"min_order_quantity": 1, "stock_quantity": 100},
                            },
                        }
                    ],
                    "subtotal": 54000,
                }
            }
        },
    )


def _all_callbacks(markup):
    return [btn.callback_data for row in markup.inline_keyboard for btn in row]


@pytest.mark.unit
@pytest.mark.anyio
class TestEditCartEntry:
    async def test_edit_order_sets_return_flag_and_renders_edit_mode(self, monkeypatch):
        handler = orders_module.OrderHandlers()
        # show_cart is owned by ProductHandlers; edit_cart must delegate to the
        # module-level product_handlers singleton in edit mode.
        fake_show_cart = AsyncMock()
        monkeypatch.setattr(orders_module.product_handlers, "show_cart", fake_show_cart)

        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="edit_order")
        context = make_context()

        await handler.edit_cart(update, context)

        assert context.user_data.get("cart_edit_return") == "order_confirm"
        fake_show_cart.assert_awaited_once()
        # delegated with edit_mode=True
        assert fake_show_cart.await_args.kwargs.get("edit_mode") is True


@pytest.mark.unit
@pytest.mark.anyio
class TestEditModeRender:
    async def test_show_cart_edit_mode_renders_per_item_controls(self, monkeypatch):
        handler = products_module.ProductHandlers()
        handler._handle_api_error = AsyncMock()
        handler._handle_auth_error = AsyncMock()
        handler._edit_or_replace_callback_message = AsyncMock()

        fake = FakeAPIClientContext()
        fake.get_cart = AsyncMock(return_value=_cart_items_response())

        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="edit_order")
        context = make_context()
        context.user_data["cart_edit_return"] = "order_confirm"

        monkeypatch.setattr(products_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(products_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(products_module, "get_auth_token", AsyncMock(return_value="tok"))
        monkeypatch.setattr(products_module, "api_client", fake)

        await handler.show_cart(update, context, edit_mode=True)

        handler._edit_or_replace_callback_message.assert_awaited_once()
        markup = handler._edit_or_replace_callback_message.await_args.kwargs["reply_markup"]
        cbs = _all_callbacks(markup)
        assert "cart_dec_7" in cbs
        assert "cart_inc_7" in cbs
        assert "cart_rm_7" in cbs
        assert "menu_products" in cbs  # ➕ Add product
        # Done routes back to the confirmation screen because cart_edit_return is set
        assert "back_to_order_confirm" in cbs


def _product_response_for(product_id=7, min_order_qty=1, stock=100):
    return APIResponse(
        success=True,
        data={
            "data": {
                "product": {
                    "id": product_id,
                    "name": "Aqua Element 18.9 l",
                    "current_price": 18000,
                    "inventory": {"min_order_quantity": min_order_qty, "stock_quantity": stock},
                }
            }
        },
    )


async def _run_cart_action(monkeypatch, fake, callback_data):
    handler = products_module.ProductHandlers()
    handler._handle_error = AsyncMock()
    handler._handle_api_error = AsyncMock()
    handler._handle_auth_error = AsyncMock()
    handler.show_cart = AsyncMock()

    update = DummyUpdate()
    update.callback_query = DummyCallbackQuery(data=callback_data)
    context = make_context()
    context.user_data["cart_edit_return"] = "order_confirm"

    monkeypatch.setattr(products_module.i18n, "get_user_language", AsyncMock(return_value="en"))
    monkeypatch.setattr(products_module.i18n, "get", _i18n_get)
    monkeypatch.setattr(products_module, "get_auth_token", AsyncMock(return_value="tok"))
    monkeypatch.setattr(products_module, "api_client", fake)

    await handler.cart_handler(update, context)
    return handler


@pytest.mark.unit
@pytest.mark.anyio
class TestCartItemMutations:
    async def test_cart_inc_updates_with_clamped_qty_and_rerenders(self, monkeypatch):
        fake = FakeAPIClientContext()
        # product currently at qty 3 in cart, stock 100, min 1 -> inc to 4
        fake.get_product = AsyncMock(return_value=_product_response_for(7, 1, 100))
        fake.get_cart = AsyncMock(return_value=_cart_items_response())
        fake.update_cart_item = AsyncMock(return_value=APIResponse(success=True, data={}))

        handler = await _run_cart_action(monkeypatch, fake, "cart_inc_7")

        fake.update_cart_item.assert_awaited_once()
        assert fake.update_cart_item.await_args.kwargs.get("quantity") == 4
        handler.show_cart.assert_awaited_once()
        assert handler.show_cart.await_args.kwargs.get("edit_mode") is True

    async def test_cart_inc_clamps_to_stock(self, monkeypatch):
        fake = FakeAPIClientContext()
        # current qty 3, but stock is 3 -> inc must clamp at 3 (no-op increase)
        fake.get_product = AsyncMock(return_value=_product_response_for(7, 1, 3))
        fake.get_cart = AsyncMock(return_value=_cart_items_response())
        fake.update_cart_item = AsyncMock(return_value=APIResponse(success=True, data={}))

        await _run_cart_action(monkeypatch, fake, "cart_inc_7")

        assert fake.update_cart_item.await_args.kwargs.get("quantity") == 3

    async def test_cart_dec_clamps_to_min_order_qty(self, monkeypatch):
        fake = FakeAPIClientContext()
        # current qty 3, min_order_qty 3 -> dec must clamp at 3
        fake.get_product = AsyncMock(return_value=_product_response_for(7, 3, 100))
        fake.get_cart = AsyncMock(return_value=_cart_items_response())
        fake.update_cart_item = AsyncMock(return_value=APIResponse(success=True, data={}))

        await _run_cart_action(monkeypatch, fake, "cart_dec_7")

        assert fake.update_cart_item.await_args.kwargs.get("quantity") == 3

    async def test_cart_rm_removes_item_and_rerenders(self, monkeypatch):
        fake = FakeAPIClientContext()
        fake.get_cart = AsyncMock(return_value=_cart_items_response())
        fake.remove_cart_item = AsyncMock(return_value=APIResponse(success=True, data={}))

        handler = await _run_cart_action(monkeypatch, fake, "cart_rm_7")

        fake.remove_cart_item.assert_awaited_once()
        # product_id positional or kwarg — assert the id 7 reached the call
        called = fake.remove_cart_item.await_args
        assert 7 in called.args or called.kwargs.get("product_id") == 7
        handler.show_cart.assert_awaited_once()
        assert handler.show_cart.await_args.kwargs.get("edit_mode") is True


@pytest.mark.unit
@pytest.mark.anyio
class TestCartViewEditModeAwareness:
    async def test_cart_view_rerenders_in_edit_mode_when_cart_edit_return_is_set(self, monkeypatch):
        """cart_view must stay in edit mode when user is editing (cart_edit_return set)."""
        fake = FakeAPIClientContext()
        fake.get_cart = AsyncMock(return_value=_cart_items_response())

        handler = products_module.ProductHandlers()
        handler._handle_error = AsyncMock()
        handler._handle_api_error = AsyncMock()
        handler._handle_auth_error = AsyncMock()
        handler.show_cart = AsyncMock()

        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="cart_view")
        context = make_context()
        context.user_data["cart_edit_return"] = "order_confirm"

        monkeypatch.setattr(products_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(products_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(products_module, "get_auth_token", AsyncMock(return_value="tok"))
        monkeypatch.setattr(products_module, "api_client", fake)

        await handler.cart_handler(update, context)

        handler.show_cart.assert_awaited_once()
        assert handler.show_cart.await_args.kwargs.get("edit_mode") is True

    async def test_cart_view_renders_normal_mode_when_cart_edit_return_not_set(self, monkeypatch):
        """cart_view must render the normal (non-edit) cart when not in edit flow."""
        fake = FakeAPIClientContext()
        fake.get_cart = AsyncMock(return_value=_cart_items_response())

        handler = products_module.ProductHandlers()
        handler._handle_error = AsyncMock()
        handler._handle_api_error = AsyncMock()
        handler._handle_auth_error = AsyncMock()
        handler.show_cart = AsyncMock()

        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="cart_view")
        context = make_context()
        # No cart_edit_return key set

        monkeypatch.setattr(products_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(products_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(products_module, "get_auth_token", AsyncMock(return_value="tok"))
        monkeypatch.setattr(products_module, "api_client", fake)

        await handler.cart_handler(update, context)

        handler.show_cart.assert_awaited_once()
        # edit_mode should be False or absent (default)
        call_kwargs = handler.show_cart.await_args.kwargs
        assert call_kwargs.get("edit_mode", False) is False


def _low_cart_response(amount_qty=1, price=1000, min_order_qty=1):
    """Cart whose subtotal is below MIN_ORDER_AMOUNT (price*qty intentionally tiny)."""
    return APIResponse(
        success=True,
        data={
            "data": {
                "cart": {
                    "cart_items": [
                        {
                            "product_id": 7,
                            "quantity": amount_qty,
                            "product": {
                                "id": 7,
                                "name": "Aqua Element 18.9 l",
                                "current_price": price,
                                "inventory": {"min_order_quantity": min_order_qty, "stock_quantity": 100},
                            },
                        }
                    ],
                    "cod_prepayment": {},
                }
            }
        },
    )


@pytest.mark.unit
@pytest.mark.anyio
class TestDoneAndBelowMinimum:
    async def test_done_routes_to_confirmation_reflecting_edited_cart(self, monkeypatch):
        handler = orders_module.OrderHandlers()
        handler._handle_auth_error = AsyncMock()
        handler._handle_api_error = AsyncMock()
        handler._handle_error = AsyncMock()

        # Live cart at Done time (edited): single item, qty 3.
        fake = FakeAPIClientContext()
        fake.get_cart = AsyncMock(return_value=_cart_items_response())  # qty 3, price 18000 -> meets min
        fake.get_loyalty_rewards = AsyncMock(return_value=APIResponse(success=True, data={"data": {"rewards": []}}))

        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="back_to_order_confirm")
        context = make_context()
        # selected address/payment survive editing
        context.user_data["selected_address_id"] = 1
        context.user_data["selected_payment_method"] = "cash"

        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(orders_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(orders_module, "get_auth_token", AsyncMock(return_value="tok"))
        monkeypatch.setattr(orders_module, "api_client", fake)
        import eligibility
        monkeypatch.setattr(eligibility, "is_loyalty_eligible", AsyncMock(return_value=False))

        await handler.back_to_order_confirm(update, context)

        # confirmation screen was rendered from the freshly fetched cart
        fake.get_cart.assert_awaited()
        update.callback_query.edit_message_text.assert_awaited_once()
        text = update.callback_query.edit_message_text.await_args.kwargs["text"]
        assert "Aqua Element 18.9 l" in text
        assert "x3" in text  # reflects edited quantity from live cart

    async def test_below_minimum_after_edit_disables_confirm(self, monkeypatch):
        handler = orders_module.OrderHandlers()
        handler._handle_auth_error = AsyncMock()
        handler._handle_api_error = AsyncMock()
        handler._handle_error = AsyncMock()

        fake = FakeAPIClientContext()
        # Subtotal = 1 * 1000 = 1000 UZS, far below MIN_ORDER_AMOUNT.
        fake.get_cart = AsyncMock(return_value=_low_cart_response(amount_qty=1, price=1000))
        fake.get_loyalty_rewards = AsyncMock(return_value=APIResponse(success=True, data={"data": {"rewards": []}}))

        captured = {}

        def _fake_order_confirmation(language, meets_minimum=True, has_reward=False, show_reward=True):
            captured["meets_minimum"] = meets_minimum
            from keyboards import KeyboardBuilder
            return KeyboardBuilder.build_inline_keyboard(
                [[{"text": "x", "callback_data": "noop"}]]
            )

        monkeypatch.setattr(orders_module.OrderKeyboards, "order_confirmation", _fake_order_confirmation)

        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="back_to_order_confirm")
        context = make_context()

        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(orders_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(orders_module, "get_auth_token", AsyncMock(return_value="tok"))
        monkeypatch.setattr(orders_module, "api_client", fake)
        import eligibility
        monkeypatch.setattr(eligibility, "is_loyalty_eligible", AsyncMock(return_value=False))

        await handler.back_to_order_confirm(update, context)

        # meets_minimum=False is what disables the Confirm button in
        # OrderKeyboards.order_confirmation (existing behaviour).
        assert captured.get("meets_minimum") is False


@pytest.mark.unit
@pytest.mark.anyio
class TestAddProductBreadcrumb:
    async def test_products_menu_shows_back_to_cart_when_editing(self, monkeypatch):
        handler = products_module.ProductHandlers()
        handler._handle_api_error = AsyncMock()
        handler._handle_auth_error = AsyncMock()

        fake = FakeAPIClientContext()
        fake.get_product_categories = AsyncMock(
            return_value=APIResponse(
                success=True,
                data={"data": {"categories": [{"id": 1, "name": "Water"}, {"id": 2, "name": "Juice"}]}},
            )
        )

        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="menu_products")
        context = make_context()
        context.user_data["cart_edit_return"] = "order_confirm"

        monkeypatch.setattr(products_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(products_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(products_module, "get_auth_token", AsyncMock(return_value="tok"))
        monkeypatch.setattr(products_module, "api_client", fake)
        # Suppress quick-order suggestions to keep the test self-contained
        monkeypatch.setattr(
            products_module.quick_order_handlers,
            "build_quick_suggestions",
            AsyncMock(return_value=[]),
        )

        await handler.products_menu(update, context)

        # products_menu renders via edit_message_text (not _edit_or_replace_callback_message)
        update.callback_query.edit_message_text.assert_awaited_once()
        markup = update.callback_query.edit_message_text.await_args.kwargs["reply_markup"]
        cbs = [btn.callback_data for row in markup.inline_keyboard for btn in row]
        assert "edit_order" in cbs  # 🛒 Back to cart breadcrumb re-enters edit mode

    async def test_products_menu_no_breadcrumb_when_not_editing(self, monkeypatch):
        """Normal product browsing must NOT show the Back to cart breadcrumb."""
        handler = products_module.ProductHandlers()
        handler._handle_api_error = AsyncMock()
        handler._handle_auth_error = AsyncMock()

        fake = FakeAPIClientContext()
        fake.get_product_categories = AsyncMock(
            return_value=APIResponse(
                success=True,
                data={"data": {"categories": [{"id": 1, "name": "Water"}, {"id": 2, "name": "Juice"}]}},
            )
        )

        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="menu_products")
        context = make_context()
        # No cart_edit_return set → normal browsing

        monkeypatch.setattr(products_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(products_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(products_module, "get_auth_token", AsyncMock(return_value="tok"))
        monkeypatch.setattr(products_module, "api_client", fake)
        monkeypatch.setattr(
            products_module.quick_order_handlers,
            "build_quick_suggestions",
            AsyncMock(return_value=[]),
        )

        await handler.products_menu(update, context)

        update.callback_query.edit_message_text.assert_awaited_once()
        markup = update.callback_query.edit_message_text.await_args.kwargs["reply_markup"]
        cbs = [btn.callback_data for row in markup.inline_keyboard for btn in row]
        assert "edit_order" not in cbs  # breadcrumb must NOT appear


@pytest.mark.unit
@pytest.mark.anyio
class TestCartEditReturnFlagClearing:
    """cart_edit_return must be cleared whenever the user leaves the edit/checkout flow."""

    async def test_cancel_checkout_clears_cart_edit_return(self, monkeypatch):
        """cancel_checkout must pop cart_edit_return so a later cart tap is normal."""
        handler = orders_module.OrderHandlers()

        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="cancel_checkout")
        context = make_context()
        context.user_data["cart_edit_return"] = "order_confirm"
        context.user_data["selected_address_id"] = 1
        context.user_data["selected_payment_method"] = "cash"

        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(orders_module.i18n, "get", _i18n_get)
        # main_menu_for is called inside cancel_checkout
        monkeypatch.setattr(orders_module, "main_menu_for", AsyncMock(return_value=None))

        await handler.cancel_checkout(update, context)

        assert context.user_data.get("cart_edit_return") is None

    async def test_back_to_order_confirm_clears_cart_edit_return(self, monkeypatch):
        """back_to_order_confirm (Done) must clear cart_edit_return before re-rendering."""
        handler = orders_module.OrderHandlers()
        handler._handle_auth_error = AsyncMock()
        handler._handle_api_error = AsyncMock()
        handler._handle_error = AsyncMock()

        fake = FakeAPIClientContext()
        fake.get_cart = AsyncMock(return_value=_cart_items_response())
        fake.get_loyalty_rewards = AsyncMock(return_value=APIResponse(success=True, data={"data": {"rewards": []}}))

        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="back_to_order_confirm")
        context = make_context()
        context.user_data["cart_edit_return"] = "order_confirm"
        context.user_data["selected_address_id"] = 1
        context.user_data["selected_payment_method"] = "cash"

        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(orders_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(orders_module, "get_auth_token", AsyncMock(return_value="tok"))
        monkeypatch.setattr(orders_module, "api_client", fake)
        import eligibility
        monkeypatch.setattr(eligibility, "is_loyalty_eligible", AsyncMock(return_value=False))

        await handler.back_to_order_confirm(update, context)

        assert context.user_data.get("cart_edit_return") is None

    async def test_confirm_order_card_payment_clears_cart_edit_return(self, monkeypatch):
        """confirm_order with card payment must clear cart_edit_return before returning."""
        handler = orders_module.OrderHandlers()
        handler._handle_auth_error = AsyncMock()
        handler._handle_api_error = AsyncMock()
        handler._handle_error = AsyncMock()

        cart_response = APIResponse(
            success=True,
            data={
                "data": {
                    "cart": {
                        "cart_items": [
                            {
                                "product_id": 7,
                                "quantity": 2,
                                "product": {"id": 7, "name": "Aqua Element 18.9 l", "current_price": 18000},
                            }
                        ]
                    }
                }
            },
        )
        order_response = APIResponse(
            success=True,
            data={
                "data": {
                    "order": {"id": 42, "order_number": "ORD-042", "total_amount": 36000, "order_items": []},
                    "payment_ready_at": None,
                }
            },
        )

        fake = FakeAPIClientContext()
        fake.get_cart = AsyncMock(return_value=cart_response)
        fake.create_order = AsyncMock(return_value=order_response)

        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="confirm_order")
        context = make_context()
        context.user_data["cart_edit_return"] = "order_confirm"
        context.user_data["selected_address_id"] = 5
        context.user_data["selected_payment_method"] = "card"

        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(orders_module.i18n, "get", _i18n_get)
        monkeypatch.setattr(orders_module, "get_auth_token", AsyncMock(return_value="tok"))
        monkeypatch.setattr(orders_module, "api_client", fake)

        # Stub out payment_handlers.send_payment_link to avoid import side-effects
        from unittest.mock import MagicMock, patch
        fake_payment_handlers = MagicMock()
        fake_payment_handlers.send_payment_link = AsyncMock(return_value=True)

        with patch("handlers.payments.payment_handlers", fake_payment_handlers):
            # Also patch the import inside confirm_order's local scope
            import handlers.payments as payments_module
            monkeypatch.setattr(payments_module, "payment_handlers", fake_payment_handlers)
            await handler.confirm_order(update, context)

        assert context.user_data.get("cart_edit_return") is None
