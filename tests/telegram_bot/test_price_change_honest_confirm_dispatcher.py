"""The confirm screen sends the total it showed, and redraws when the server says that total moved (Task 47).

Driven through the real dispatcher against the journey's `Shop`: the bot posts `expected_total` (the estimate's
`pricing.final_total`) on `POST /api/v1/orders`; a 409 `STATE_CHANGED` / `price_changed` carrying the new quote
redraws the confirm screen from that quote and places nothing. The bot compares nothing and computes nothing.
"""

from __future__ import annotations

import html

import pytest

import bot as bot_module  # noqa: F401  (resolves the bot's own modules first)

from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

from tests.telegram_bot.ptb_harness import backend_failure, build_bot_harness
from tests.telegram_bot.test_checkout_journey_dispatcher import (
    BOTTLE_19L,
    TRANSLATIONS,
    Shop,
    add_address,
    assert_no_swallowed_crash,
    backend_calls,
    deliberate_retap,
    fill_cart,
    order_payloads,
    reach_confirmation,
    reach_payment_picker,
    toasts,
    user_data,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

NOTE_KEY = "telegram.price_change.price_changed_note"
NOTE = BACKEND_TRANSLATIONS.get(NOTE_KEY, {}).get("uz", "NOTE-NOT-SEEDED")
ADDRESS = 900
STALE_REWARD = 4


@pytest.fixture
async def bot(monkeypatch):
    return await build_bot_harness(monkeypatch, translations={**TRANSLATIONS, NOTE_KEY: NOTE})


@pytest.fixture
def shop(bot, monkeypatch):
    return Shop(bot.backend, monkeypatch)


@pytest.fixture
def user(bot):
    return bot.updates()


def _new_quote(*, total=60000.0):
    """The estimate `data` the server answers with once the price is 20,000: 3 x 20,000."""
    return {
        "items": [{
            "product_id": BOTTLE_19L, "product_name": "Aqua Element 19L", "quantity": 3,
            "unit_price": 20000.0, "subtotal": total,
        }],
        "pricing": {
            "items_subtotal": total, "delivery_fee": 0.0, "promo_discount": 0.0, "discount_amount": 0.0,
            "loyalty_discount": 0.0, "tier_discount": 0.0, "tier_name": None, "tier_discount_percentage": 0.0,
            "cod_savings": 0.0, "payment_method": "cash", "total_discount": 0.0,
            "total_before_discount": total, "final_total": total,
        },
        "cod_prepayment": None,
    }


def price_changed(quote):
    """`POST /api/v1/orders/`'s 409 for a total drawn before the price moved (Task 24's customer envelope)."""
    message = "Bu o'zgardi."
    return backend_failure(message, 409, data={
        "success": False, "message": message,
        "data": {"reason_code": "STATE_CHANGED", "reason": "price_changed", "quote": quote},
    })


def estimate_calls(bot):
    return backend_calls(bot, "POST", "/api/v1/orders/cart/estimate")


async def test_confirm_posts_the_total_the_confirm_screen_showed(bot, shop, user):
    shop.estimate_pricing = {"final_total": 43650.0, "tier_discount": 1350.0}
    add_address(bot, ADDRESS, "Uy", "Chilonzor 15")
    await fill_cart(bot, user, BOTTLE_19L, quantity=3)
    await reach_confirmation(bot, user, ADDRESS, payment="cash")

    await bot.send(user.tap("confirm_order"))

    assert order_payloads(bot) == [{
        "delivery_address_id": ADDRESS,
        "payment_method": "cash",
        "source": "telegram",
        "items": [{"product_id": BOTTLE_19L, "quantity": 3}],
        "expected_total": 43650.0,
    }]
    assert_no_swallowed_crash(bot)


async def test_a_price_change_between_screen_and_tap_redraws_the_confirm_screen_and_places_nothing(bot, shop, user):
    add_address(bot, ADDRESS, "Uy", "Chilonzor 15")
    await fill_cart(bot, user, BOTTLE_19L, quantity=3)
    await reach_confirmation(bot, user, ADDRESS, payment="cash")
    assert "To pay: 45,000 so'm" in bot.telegram.last_shown().text
    estimates_before = len(estimate_calls(bot))

    shop.order_response = price_changed(_new_quote())
    await bot.send(user.tap("confirm_order"))

    assert shop.orders == []
    assert shop.cart == {BOTTLE_19L: 3}, "a refused order must not empty the cart"
    redraw = bot.telegram.last_shown()
    assert redraw.method == "editMessageText"
    lines = redraw.text.split("\n")
    assert lines[:4] == ["<b>Buyurtmani tasdiqlang</b>", "", html.escape(NOTE, quote=False), ""]
    assert "To pay: 60,000 so'm" in redraw.text
    assert "45,000" not in redraw.text
    assert "confirm_order" in redraw.callback_data()
    assert len(estimate_calls(bot)) == estimates_before, "the redraw reads the quote the 409 carried"
    assert not any(text.startswith("❌") for text in toasts(bot))
    assert user_data(bot)["selected_address_id"] == ADDRESS
    assert user_data(bot)["selected_payment_method"] == "cash"

    shop.order_response = None
    await deliberate_retap(bot, user.tap("confirm_order"))

    assert [payload["expected_total"] for payload in order_payloads(bot)] == [45000.0, 60000.0]
    assert len(shop.orders) == 1
    assert_no_swallowed_crash(bot)


async def test_the_redrawn_confirm_screen_keeps_the_chosen_reward(bot, shop, user):
    bot.backend.route(
        "GET", "/api/v1/loyalty/rewards",
        lambda call: {"data": {"rewards": [{
            "id": STALE_REWARD, "name": "Free 19L bottle", "can_redeem": True, "reward_type": "free_product",
            "free_product_quantity": 1,
        }]}},
    )
    add_address(bot, ADDRESS, "Uy", "Chilonzor 15")
    await fill_cart(bot, user, BOTTLE_19L, quantity=3)
    await reach_confirmation(bot, user, ADDRESS, payment="cash")
    await bot.send(user.tap(f"checkout_apply_reward_{STALE_REWARD}"))
    assert user_data(bot)["selected_reward_id"] == STALE_REWARD

    shop.order_response = price_changed(_new_quote())
    await bot.send(user.tap("confirm_order"))

    assert order_payloads(bot)[0]["reward_id"] == STALE_REWARD
    assert user_data(bot)["selected_reward_id"] == STALE_REWARD, "the refused tap must not drop the reward"
    assert "Free 19L bottle" in bot.telegram.last_shown().text

    shop.order_response = None
    await deliberate_retap(bot, user.tap("confirm_order"))

    assert order_payloads(bot)[1]["reward_id"] == STALE_REWARD
    assert order_payloads(bot)[1]["expected_total"] == 60000.0
    assert len(shop.orders) == 1


async def test_the_cash_rescue_without_a_stash_sends_no_expected_total(bot, shop, user):
    add_address(bot, ADDRESS, "Uy", "Chilonzor 15")
    await fill_cart(bot, user, BOTTLE_19L, quantity=3)
    await reach_confirmation(bot, user, ADDRESS, payment="card")
    assert user_data(bot)["confirm_expected_total"] == 45000.0

    await bot.send(user.tap("select_payment_cash"))

    assert order_payloads(bot) == [{
        "delivery_address_id": ADDRESS,
        "payment_method": "cash",
        "source": "telegram",
        "items": [{"product_id": BOTTLE_19L, "quantity": 3}],
    }]
    assert_no_swallowed_crash(bot)


async def test_a_state_changed_refusal_without_a_quote_keeps_the_error_toast(bot, shop, user):
    add_address(bot, ADDRESS, "Uy", "Chilonzor 15")
    await fill_cart(bot, user, BOTTLE_19L, quantity=3)
    await reach_confirmation(bot, user, ADDRESS, payment="cash")
    estimates_before = len(estimate_calls(bot))

    shop.order_response = backend_failure("Bu o'zgardi.", 409, data={
        "success": False, "message": "Bu o'zgardi.",
        "data": {"reason_code": "STATE_CHANGED", "reason": "price_changed"},
    })
    await bot.send(user.tap("confirm_order"))

    assert "❌ Bu o'zgardi." in toasts(bot)
    assert shop.orders == []
    assert len(estimate_calls(bot)) == estimates_before
    assert NOTE not in bot.telegram.last_shown().text


async def test_changing_the_payment_method_redraws_and_restashes(bot, shop, user):
    shop.estimate_pricing = {"final_total": 43650.0, "tier_discount": 1350.0}
    add_address(bot, ADDRESS, "Uy", "Chilonzor 15")
    await fill_cart(bot, user, BOTTLE_19L, quantity=3)
    await reach_confirmation(bot, user, ADDRESS, payment="cash")
    assert user_data(bot)["confirm_expected_total"] == 43650.0

    shop.estimate_pricing = {"final_total": 45000.0}
    await bot.send(user.tap("payment_card"))
    assert user_data(bot)["confirm_expected_total"] == 45000.0

    await bot.send(user.tap("confirm_order"))

    assert [payload["expected_total"] for payload in order_payloads(bot)] == [45000.0]
    assert order_payloads(bot)[0]["payment_method"] == "card"


async def test_cancelling_checkout_forgets_the_stash(bot, shop, user):
    add_address(bot, ADDRESS, "Uy", "Chilonzor 15")
    await fill_cart(bot, user, BOTTLE_19L, quantity=3)
    await reach_confirmation(bot, user, ADDRESS, payment="cash")
    assert user_data(bot)["confirm_expected_total"] == 45000.0

    await bot.send(user.tap("cancel_order"))

    assert "confirm_expected_total" not in user_data(bot)
