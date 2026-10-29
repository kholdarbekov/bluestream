"""Back under the card-payment link, and the "paid" message sent as a new message.

Spec docs/superpowers/specs/2026-10-21-customer-bot-bottom-menu-and-reorder-design.md §6 (R7).
The link's Back sent `back_to_order_confirm`. By then `confirm_order` has already run
`context.user_data.clear()` (handlers/orders.py:1371), so every tap got the
`telegram.orders.missing_info` alert. Prod, 30 days: 6 episodes. In 3 of them the customer
then placed a new cash order and the card order was cancelled about 11 minutes later.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib
import importlib.util
import json
from pathlib import Path

import pytest
from aiohttp import test_utils

# Imported at module (collection) level so these resolve to the BOT's modules (tests/telegram_bot/conftest.py).
from i18n import i18n
from keyboards import PaymentKeyboards

from tests.telegram_bot.ptb_harness import DEFAULT_USER_ID
from tests.telegram_bot.test_checkout_journey_dispatcher import (  # noqa: F401
    BOTTLE_5L,
    BOTTLE_19L,
    add_address,
    assert_no_swallowed_crash,
    backend_calls,
    bot,
    fill_cart,
    reach_confirmation,
    shop,
    toasts,
    user,
    user_data,
)

# In a mixed xdist worker the bare `webhook_server` name belongs to the staff bot. Let `bot`
# claim the customer bot's bare deps first, then load the module by path under a unique name
# (the idiom of tests/telegram_bot/test_webhook_delivery_completed.py:19-41).
importlib.import_module("bot")
_WS_PATH = Path(__file__).resolve().parents[2] / "telegram_bot" / "webhook_server.py"
_spec = importlib.util.spec_from_file_location("telegram_webhook_server_payment_back_under_test", _WS_PATH)
ws_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ws_module)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

HOME = 900
SECRET = "test-webhook-secret"
MISSING_INFO = i18n.humanised_missing_key("telegram.orders.missing_info")


def alerts(bot):
    """Every popup alert (show_alert=True). The missing-info refusal used this channel."""
    return [
        call.params.get("text")
        for call in bot.telegram.of("answerCallbackQuery")
        if call.params.get("show_alert")
    ]


def serve_order(bot, order):
    bot.backend.route(
        "GET", f"/api/v1/orders/{order['id']}",
        lambda call: {"data": {"order": dict(order), "delivery": None}},
    )


async def place_card_order(bot, shop, user):
    """Cart, card checkout, Confirm: the order exists and its payment link is on screen."""
    add_address(bot, HOME, "Uy", "Chilonzor 15")
    await fill_cart(bot, user, BOTTLE_19L, quantity=3)
    await reach_confirmation(bot, user, HOME, payment="card")
    await bot.send(user.tap("confirm_order"))
    (order,) = shop.orders
    return order


async def test_back_under_the_payment_link_opens_that_order_and_pay_now_comes_back_to_it(bot, shop, user):
    order = await place_card_order(bot, shop, user)
    link = bot.telegram.last_shown()
    assert link.text == f"Pay order BS-{order['id']}: 45,000 UZS"
    assert link.callback_data() == [f"order_{order['id']}"], (
        "Back must name the order it was placed for: `back_to_order_confirm` needs the "
        "checkout state confirm_order has just cleared"
    )

    serve_order(bot, order)
    bot.telegram.reset()
    await bot.send(user.tap(link.callback_data()[0]))

    details = bot.telegram.last_shown()
    assert details.method == "editMessageText"
    assert details.params.get("parse_mode") == "MarkdownV2"
    assert f"BS\\-{order['id']}" in details.text
    # OrderKeyboards.order_details: Pay is the published one, `payment_retry_<id>_<amount>`.
    pay_action = order["customer_actions"]["pay"]
    (pay,) = [d for d in details.callback_data() if d.startswith("payment_retry_")]
    assert pay == f"payment_retry_{order['id']}_{pay_action['expected_amount']}"
    assert f"cancel_order_{order['id']}" in details.callback_data()
    assert alerts(bot) == []

    # Pay, from the button the customer can see: retry_payment draws the link `retry-payment`
    # answered through the same show_payment_link checkout uses, so the new link's Back is the same.
    shop.retry_payment_route(order)
    bot.telegram.reset()
    await bot.send(user.tap(pay))

    relink = bot.telegram.last_shown()
    assert relink.text == f"Pay order BS-{order['id']}: 45,000 UZS"
    assert relink.callback_data() == [f"order_{order['id']}"]
    (paid,) = backend_calls(bot, "POST", f"/api/v1/orders/{order['id']}/retry-payment")
    assert paid.data == {"channel": "bot", "expected_amount": pay_action["expected_amount"]}
    assert backend_calls(bot, "POST", "/api/v1/payments/create") == []
    assert shop.orders == [order]
    assert alerts(bot) == []
    assert_no_swallowed_crash(bot)


async def test_an_old_link_back_with_an_empty_cart_opens_my_orders(bot, shop, user):
    """A link sent before this release keeps sending `back_to_order_confirm` forever."""
    order = await place_card_order(bot, shop, user)
    # The backend empties the cart on EVERY order create (business_app/api/orders.py:452-457).
    # This fake keeps the cart for card orders, so empty it here as the server already has.
    shop.cart.clear()
    bot.backend.route("GET", "/api/v1/orders/", lambda call: {"data": {"orders": [dict(order)]}})
    assert "selected_address_id" not in user_data(bot)
    bot.telegram.reset()

    await bot.send(user.tap("back_to_order_confirm"))

    screen = bot.telegram.last_shown()
    assert screen.text.startswith(i18n.humanised_missing_key("telegram.orders.your_orders"))
    assert f"order_{order['id']}" in screen.callback_data()
    assert alerts(bot) == []
    assert MISSING_INFO not in toasts(bot)
    assert_no_swallowed_crash(bot)


async def test_an_old_link_back_with_a_new_basket_opens_the_cart(bot, shop, user):
    order = await place_card_order(bot, shop, user)
    shop.cart.clear()  # the server emptied it at create (see above)
    await fill_cart(bot, user, BOTTLE_5L, quantity=4)
    bot.telegram.reset()

    await bot.send(user.tap("back_to_order_confirm"))

    cart = bot.telegram.last_shown()
    assert "🛒 Aqua Element 5L x 4 = 24,000 UZS" in cart.text
    # Both normal-mode cart keyboards carry cart_clear (keyboards.py:636-653) and edit mode does
    # not, so this holds whatever MIN_ORDER_AMOUNT is.
    assert "cart_clear" in cart.callback_data()
    assert alerts(bot) == []
    assert MISSING_INFO not in toasts(bot)
    assert shop.orders == [order]
    assert_no_swallowed_crash(bot)


async def test_cart_edit_done_after_a_restart_opens_the_cart_not_an_alert(bot, shop, user):
    """Cart-edit Done sends `back_to_order_confirm`, and should (keyboards.py:621). After a
    restart (there is no persistence) the checkout state is gone, but the basket is still there."""
    add_address(bot, HOME, "Uy", "Chilonzor 15")
    await fill_cart(bot, user, BOTTLE_19L, quantity=3)
    await reach_confirmation(bot, user, HOME, payment="cash")
    await bot.send(user.tap("edit_order"))
    assert "back_to_order_confirm" in bot.telegram.last_shown().callback_data()

    user_data(bot).clear()
    bot.telegram.reset()
    await bot.send(user.tap("back_to_order_confirm"))

    cart = bot.telegram.last_shown()
    assert "🛒 Aqua Element 19L x 3 = 45,000 UZS" in cart.text
    assert "cart_clear" in cart.callback_data()
    assert f"cart_inc_{BOTTLE_19L}" not in cart.callback_data(), "edit mode ends with the checkout it served"
    assert alerts(bot) == []
    assert_no_swallowed_crash(bot)


def _sign(raw: bytes) -> str:
    return hmac.new(SECRET.encode("utf-8"), raw, hashlib.sha256).hexdigest()


async def test_a_paid_message_sent_as_a_new_message_carries_the_view_order_keyboard(bot, monkeypatch):
    """No stored payment-message id, so the bot sends a new message instead of editing one. Prod: 7 of 76 pushes in 30 days."""
    monkeypatch.setattr(ws_module.config.security, "webhook_secret", SECRET)
    server = ws_module.WebhookServer()
    server.set_application(bot.application)  # FakeTokenManager.redis is None: no message id to edit
    await server.setup()
    payload = {"user_id": 398, "telegram_id": DEFAULT_USER_ID, "order_id": 7350,
               "order_number": "BS-7350", "amount": 45000, "currency": "UZS"}
    raw = json.dumps(payload).encode("utf-8")

    async with test_utils.TestClient(test_utils.TestServer(server.app)) as client:
        response = await client.post(
            "/internal/payment-success",
            data=raw,
            headers={"Content-Type": "application/json",
                     "X-Bot-Webhook-Signature": _sign(raw),
                     "X-Request-ID": "pay-7350"},
        )
        assert response.status == 200

    assert bot.telegram.of("editMessageText") == []
    (sent,) = bot.telegram.of("sendMessage")
    assert sent.params["chat_id"] == DEFAULT_USER_ID
    # Exactly what the edit path attaches (webhook_server.py:615). It is read from the builder,
    # so Task 18 removing the Back-to-menu row does not break this test.
    edit_path = PaymentKeyboards.payment_success(7350, "uz")
    assert sent.callback_data() == [b.callback_data for row in edit_path.inline_keyboard for b in row]
    assert "order_7350" in sent.callback_data()
