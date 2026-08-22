"""A refused ack must never cost the products flow its work.

THE CLASS (see tests/telegram_bot/test_callback_ack_guard_home.py for where the
guard lives): answering a callback query is COSMETIC. Telegram discards
callback queries after ~60s and refuses a late ``answerCallbackQuery`` with
"query is too old and response timeout expired or query id is invalid" —
routine after every redeploy, when the backlog of taps that piled up while the
bot was down is redelivered. A handler that acks and THEN works loses the work
to that rejection, and the global error handler cannot report it either: its
one user-facing action is answering the SAME dead query.

``ProductHandlers._clear_cart`` was the instance of it left in products.py: it
toasted "cart cleared" and re-rendered the cart afterwards. The backend clear
had already happened, so a refused ack left the customer looking at a screen
full of items that no longer exist — and the next thing they tap acts on a cart
the server emptied minutes ago.
"""

import pytest

from tests.telegram_bot.ptb_harness import build_bot_harness

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


STALE_ACK = (
    "Bad Request: query is too old and response timeout expired or query id is invalid"
)

TRANSLATIONS = {
    ("uz", "telegram.cart_title"): "Savat",
    ("uz", "telegram.cart_empty"): "Savatingiz bo'sh",
    ("uz", "telegram.cart_total"): "Jami",
    ("uz", "telegram.cart_ready_checkout"): "Buyurtmaga tayyor",
    ("uz", "telegram.products.cart_cleared"): "Savat tozalandi",
    ("uz", "telegram.back"): "Orqaga",
    ("uz", "telegram.error_occurred"): "Xatolik yuz berdi",
}

CART_ITEM = {
    "product": {
        "id": 7,
        "name": "Suv 19L",
        "current_price": 15000.0,
        "inventory": {"min_order_quantity": 1, "stock_quantity": 40},
        "specifications": {"volume": 19, "volume_unit": "l"},
    },
    "quantity": 2,
    "total_price": 30000.0,
}


@pytest.fixture
async def bot(monkeypatch):
    harness = await build_bot_harness(monkeypatch, translations=TRANSLATIONS)
    harness.cleared = []

    def read_cart(_call):
        items = [] if harness.cleared else [CART_ITEM]
        return {"data": {"cart": {"cart_items": items, "subtotal": 0 if items == [] else 30000}}}

    def clear_cart(call):
        harness.cleared.append(call)
        return {"data": {}}

    harness.backend.route("GET", "/api/v1/cart", read_cart)
    harness.backend.route("POST", "/api/v1/cart/clear", clear_cart)
    return harness


@pytest.fixture
def user(bot):
    return bot.updates()


async def test_a_stale_ack_still_leaves_the_customer_looking_at_the_emptied_cart(
    bot, user
):
    """Clear the cart on a tap Telegram has already expired.

    The clear itself is not in doubt — it happens before the ack. What must not
    be lost is the re-render: a cleared cart still showing two bottles is a lie
    the customer will act on.
    """
    bot.telegram.fail("answerCallbackQuery", STALE_ACK, status=400)

    await bot.send(user.tap("cart_clear"))

    assert len(bot.cleared) == 1, (
        "the tap never reached the backend clear at all"
    )
    assert bot.telegram.of("answerCallbackQuery"), (
        "the ack this test is about was never attempted, so it proved nothing"
    )
    screens = [call.text for call in bot.telegram.shown]
    assert screens, "the customer got no screen at all after their cart was cleared"
    assert TRANSLATIONS[("uz", "telegram.cart_empty")] in screens[-1], (
        "the cart was emptied on the server and the customer was left looking "
        f"at the old screen; screens={screens}"
    )
    assert TRANSLATIONS[("uz", "telegram.error_occurred")] not in screens[-1], (
        "a cosmetic ack rejection was reported to the customer as a failure"
    )
