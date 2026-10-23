"""Product → quantity screen → Checkout, through the real dispatcher (owner 2026-10-22).

Tapping a product opens its quantity screen directly: there is no product-details card and no
"Add to cart" step. For a product the cart does not hold yet, the screen is a DRAFT: presets, ± and a
typed number change only the number on screen, ✅ Checkout puts it in the cart and opens checkout,
and Back leaves the cart as it was. For a product the cart already holds, every one of those changes
the cart line at once (owner 2026-10-23); Checkout then just confirms the number and opens checkout.

The copy is the real seeded copy (`scripts/seed_backend_translations.py`), so a missing or
misspelled key fails here rather than reaching a customer as a humanised key.
"""

from __future__ import annotations

import json

import pytest
from telegram import Update

import bot as bot_module  # noqa: F401

# Bare bot-module names: already in sys.modules after `import bot`.
import utils as utils_module
from menu_keyboard import MENU_KEYBOARD_STATE_KEY, wanted_menu_labels

# AFTER the bot import, for the sys.path reason test_agent_order_callbacks.py documents.
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS  # noqa: E402

from tests.telegram_bot.ptb_harness import backend_failure, build_bot_harness
from tests.telegram_bot.test_cart_and_quantity_journeys import deliberate_retap
from tests.telegram_bot.test_checkout_journey_dispatcher import (
    BOTTLE_19L,
    BOTTLE_5L,
    WATER,
    Shop,
    add_address,
    backend_calls,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

TRANSLATIONS = {
    (language, key): row[language]
    for key, row in BACKEND_TRANSLATIONS.items()
    if key.startswith("telegram.")
    for language in ("uz", "ru", "en")
    if row.get(language)
}

LIST_MESSAGE = 4500


def uz(key: str, **fmt) -> str:
    return BACKEND_TRANSLATIONS[key]["uz"].format(**fmt)


def body(call) -> str:
    """What the customer reads on a bubble: a photo's caption or a message's text."""
    return call.params.get("caption") or call.text


def cart_writes(bot) -> list:
    return [
        (call.method, call.endpoint, call.data)
        for call in bot.backend.calls
        if call.method in {"POST", "PUT", "DELETE"} and call.endpoint.startswith("/api/v1/cart")
    ]


def pick(bot, prefix: str, suffix: str = "") -> str:
    shown = bot.telegram.last_shown().callback_data()
    matches = [d for d in shown if d.startswith(prefix) and d.endswith(suffix)]
    assert matches, f"no {prefix!r}...{suffix!r} button on {shown}"
    return matches[0]


def photo_tap(user, callback_data: str, message_id: int) -> Update:
    """A tap on a PHOTO bubble (`UpdateFactory.tap` only builds text bubbles)."""
    update = user.tap(callback_data, message_id=message_id).to_dict()
    message = update["callback_query"]["message"]
    del message["text"]
    message["photo"] = [{"file_id": "list", "file_unique_id": "u-list", "width": 640, "height": 640}]
    message["caption"] = "category list"
    return Update.de_json(update, user.bot)


async def _always_allowed(*_args, **_kwargs):
    return True


@pytest.fixture
async def bot(monkeypatch):
    harness = await build_bot_harness(monkeypatch, translations=TRANSLATIONS)
    monkeypatch.setattr(utils_module.rate_limiter, "allow_request", _always_allowed)
    harness.database.user["bot_state"] = json.dumps({
        MENU_KEYBOARD_STATE_KEY: {
            "labels": wanted_menu_labels("uz", True), "language": "uz", "loyalty": True,
        },
    })
    return harness


@pytest.fixture
def shop(bot, monkeypatch):
    return Shop(bot.backend, monkeypatch)


@pytest.fixture
def user(bot):
    return bot.updates()


async def open_product(bot, user, product_id=BOTTLE_19L):
    """Order water → the water category → the product, as the customer taps it."""
    await bot.send(user.text(uz("telegram.menu.products")))
    # A person takes seconds per screen; a second visit's category tap is not a double-tap.
    await deliberate_retap(bot, user.tap(f"category_{WATER}"))
    await bot.send(user.tap(f"product_{product_id}", message_id=LIST_MESSAGE))
    return bot.telegram.last_shown()


async def test_tapping_a_product_opens_its_quantity_screen_and_writes_nothing(bot, shop, user):
    screen = await open_product(bot, user)

    assert body(screen) == (
        "🛒 Aqua Element 19L\n"
        "💰 Narxi: 15,000 UZS (1 dona)\n"
        "ℹ️ Minimal buyurtma: 2\n"
        "\n"
        "Soni: 2\n"
        "Jami: 2 × 15,000 = 30,000 UZS\n"
        "\n"
        "✏️ Yoki kerakli miqdorni yozing"
    )
    buttons = screen.callback_data()
    assert buttons[-2:] == [f"buy_{BOTTLE_19L}_2", f"category_{WATER}"]
    assert screen.button_labels()[-2:] == [uz("telegram.cart.checkout"), uz("telegram.back")]
    assert {f"qty_dec_{BOTTLE_19L}_2", f"qty_inc_{BOTTLE_19L}_2"} <= set(buttons)
    assert not [data for data in buttons if data.startswith("add_to_cart_")]
    assert cart_writes(bot) == []
    assert shop.cart == {}


async def test_presets_and_plus_minus_change_only_the_number_on_screen(bot, shop, user):
    await open_product(bot, user)

    await bot.send(user.tap(pick(bot, f"qty_set_{BOTTLE_19L}_", "_12")))
    assert "Soni: 12\nJami: 12 × 15,000 = 180,000 UZS" in body(bot.telegram.last_shown())

    await bot.send(user.tap(f"qty_inc_{BOTTLE_19L}_12"))
    assert "Soni: 13\nJami: 13 × 15,000 = 195,000 UZS" in body(bot.telegram.last_shown())

    await bot.send(user.tap(f"qty_dec_{BOTTLE_19L}_13"))
    screen = bot.telegram.last_shown()
    assert "Soni: 12\n" in body(screen)
    assert f"buy_{BOTTLE_19L}_12" in screen.callback_data()
    assert cart_writes(bot) == []
    assert shop.cart == {}


async def test_minus_stops_at_the_product_minimum(bot, shop, user):
    await open_product(bot, user)

    await bot.send(user.tap(f"qty_dec_{BOTTLE_19L}_2"))

    assert "Soni: 2\n" in body(bot.telegram.last_shown())
    assert cart_writes(bot) == []


async def test_checkout_adds_the_shown_quantity_and_opens_checkout(bot, shop, user):
    add_address(bot, 901, "Uy", "Chilonzor 9-kvartal, 15-uy")
    await open_product(bot, user)
    await bot.send(user.tap(pick(bot, f"qty_set_{BOTTLE_19L}_", "_12")))

    await bot.send(user.tap(f"buy_{BOTTLE_19L}_12"))

    assert cart_writes(bot) == [("POST", "/api/v1/cart/items", {"product_id": BOTTLE_19L, "quantity": 12})]
    assert shop.cart == {BOTTLE_19L: 12}
    assert bot.telegram.last_shown().text.startswith(uz("telegram.checkout.delivering_to"))


async def test_a_product_already_in_the_cart_is_changed_in_the_cart_as_the_customer_taps(bot, shop, user):
    shop.cart[BOTTLE_19L] = 10
    screen = await open_product(bot, user)
    assert "Soni: 10\nJami: 10 × 15,000 = 150,000 UZS" in body(screen)
    assert cart_writes(bot) == []

    await bot.send(user.tap(pick(bot, f"qty_set_{BOTTLE_19L}_", "_15")))
    assert shop.cart == {BOTTLE_19L: 15}
    await bot.send(user.tap(f"qty_inc_{BOTTLE_19L}_15"))
    assert shop.cart == {BOTTLE_19L: 16}
    await bot.send(user.text("7"))

    assert "Soni: 7\nJami: 7 × 15,000 = 105,000 UZS" in body(bot.telegram.last_shown())
    assert cart_writes(bot) == [
        ("PUT", f"/api/v1/cart/items/{BOTTLE_19L}", {"quantity": 15}),
        ("PUT", f"/api/v1/cart/items/{BOTTLE_19L}", {"quantity": 16}),
        ("PUT", f"/api/v1/cart/items/{BOTTLE_19L}", {"quantity": 7}),
    ]
    assert shop.cart == {BOTTLE_19L: 7}


async def test_back_keeps_a_change_to_a_product_already_in_the_cart(bot, shop, user):
    shop.cart[BOTTLE_19L] = 10
    await open_product(bot, user)
    await bot.send(user.tap(pick(bot, f"qty_set_{BOTTLE_19L}_", "_15")))

    # Seconds after the category tap that opened the list, so the double-tap guard lets it through.
    await deliberate_retap(bot, user.tap(f"category_{WATER}"))

    assert f"product_{BOTTLE_19L}" in bot.telegram.last_shown().callback_data()
    assert cart_writes(bot) == [("PUT", f"/api/v1/cart/items/{BOTTLE_19L}", {"quantity": 15})]
    assert shop.cart == {BOTTLE_19L: 15}


async def test_back_from_a_product_not_in_the_cart_leaves_the_cart_as_it_was(bot, shop, user):
    shop.cart[BOTTLE_5L] = 4
    await open_product(bot, user)
    await bot.send(user.tap(pick(bot, f"qty_set_{BOTTLE_19L}_", "_15")))

    await deliberate_retap(bot, user.tap(f"category_{WATER}"))

    assert f"product_{BOTTLE_19L}" in bot.telegram.last_shown().callback_data()
    assert cart_writes(bot) == []
    assert shop.cart == {BOTTLE_5L: 4}


async def test_checkout_on_a_product_already_in_the_cart_sets_the_shown_number_and_opens_checkout(
    bot, shop, user
):
    add_address(bot, 901, "Uy", "Chilonzor 9-kvartal, 15-uy")
    shop.cart[BOTTLE_19L] = 10
    await open_product(bot, user)

    await bot.send(user.tap(f"buy_{BOTTLE_19L}_10"))

    assert cart_writes(bot) == [("PUT", f"/api/v1/cart/items/{BOTTLE_19L}", {"quantity": 10})]
    assert shop.cart == {BOTTLE_19L: 10}
    assert bot.telegram.last_shown().text.startswith(uz("telegram.checkout.delivering_to"))


async def test_a_stale_plus_on_a_product_in_the_cart_steps_from_the_cart(bot, shop, user):
    """A '+1' further up the chat must not rewrite the number the customer chose since."""
    shop.cart[BOTTLE_19L] = 10
    await open_product(bot, user)
    stale_plus = f"qty_inc_{BOTTLE_19L}_10"
    assert stale_plus in bot.telegram.last_shown().callback_data()
    await bot.send(user.tap(pick(bot, f"qty_set_{BOTTLE_19L}_", "_15")))

    await bot.send(user.tap(stale_plus))

    assert shop.cart == {BOTTLE_19L: 16}
    assert f"buy_{BOTTLE_19L}_16" in bot.telegram.last_shown().callback_data()


async def test_a_refused_change_to_a_product_in_the_cart_says_why_and_changes_nothing(bot, shop, user):
    shop.cart[BOTTLE_19L] = 10
    await open_product(bot, user)
    bot.backend.route(
        "PUT", f"/api/v1/cart/items/{BOTTLE_19L}",
        lambda _call: backend_failure("Only 12 left in stock", status_code=400),
    )
    shown_before = len(bot.telegram.shown)

    await bot.send(user.tap(pick(bot, f"qty_set_{BOTTLE_19L}_", "_15")))

    answers = [call.params.get("text") or "" for call in bot.telegram.of("answerCallbackQuery")]
    assert any("Only 12 left in stock" in text for text in answers)
    assert len(bot.telegram.shown) == shown_before, "the screen must keep showing the 10 the cart holds"
    assert shop.cart == {BOTTLE_19L: 10}


async def test_a_quantity_tap_over_a_cart_it_cannot_read_changes_nothing(bot, shop, user):
    """Unreadable is not "not in the cart": a draft redraw would show a number the cart may not hold."""
    shop.cart[BOTTLE_19L] = 10
    await open_product(bot, user)
    bot.backend.route("GET", "/api/v1/cart", lambda _call: backend_failure("HTTP 502", status_code=502))
    shown_before = len(bot.telegram.shown)

    await bot.send(user.tap(f"qty_inc_{BOTTLE_19L}_10"))

    assert any("HTTP 502" in (call.params.get("text") or "") for call in bot.telegram.of("answerCallbackQuery"))
    assert len(bot.telegram.shown) == shown_before
    assert cart_writes(bot) == []
    assert shop.cart == {BOTTLE_19L: 10}


async def test_two_products_reach_the_cart_through_two_checkouts(bot, shop, user):
    add_address(bot, 901, "Uy", "Chilonzor 9-kvartal, 15-uy")
    await open_product(bot, user, BOTTLE_19L)
    await bot.send(user.tap(f"buy_{BOTTLE_19L}_2"))
    await open_product(bot, user, BOTTLE_5L)
    await bot.send(user.tap(pick(bot, f"qty_set_{BOTTLE_5L}_", "_7")))

    await bot.send(user.tap(f"buy_{BOTTLE_5L}_7"))

    assert shop.cart == {BOTTLE_19L: 2, BOTTLE_5L: 7}
    assert [(method, endpoint) for method, endpoint, _ in cart_writes(bot)] == [
        ("POST", "/api/v1/cart/items"), ("POST", "/api/v1/cart/items"),
    ]
    assert bot.telegram.last_shown().text.startswith(uz("telegram.checkout.delivering_to"))


async def test_a_typed_number_redraws_the_screen_and_checkout_adds_it(bot, shop, user):
    await open_product(bot, user)

    await bot.send(user.text("7"))

    screen = bot.telegram.last_shown()
    assert screen.method == "sendMessage"
    assert "Soni: 7\nJami: 7 × 15,000 = 105,000 UZS" in body(screen)
    assert cart_writes(bot) == []

    await bot.send(user.tap(f"buy_{BOTTLE_19L}_7"))

    assert shop.cart == {BOTTLE_19L: 7}


async def test_a_typed_number_below_the_minimum_is_refused_and_the_cart_is_untouched(bot, shop, user):
    await open_product(bot, user)

    await bot.send(user.text("1"))

    assert bot.telegram.last_shown().text == uz("telegram.products.typed_quantity_below_min", min_qty=2)
    assert cart_writes(bot) == []


async def test_a_sold_out_product_says_so_and_offers_only_back(bot, shop, user):
    shop.products[BOTTLE_5L]["inventory"]["stock_quantity"] = 0

    screen = await open_product(bot, user, BOTTLE_5L)

    assert body(screen) == (
        "🛒 Aqua Element 5L\n"
        "💰 Narxi: 6,000 UZS (1 dona)\n"
        "\n"
        "❌ Tugagan"
    )
    assert screen.callback_data() == [f"category_{WATER}"]
    assert cart_writes(bot) == []


async def test_the_contract_price_note_sits_under_the_unit_price(bot, shop, user):
    shop.products[BOTTLE_19L]["pricing"]["contract_price_at_checkout"] = True

    screen = await open_product(bot, user)

    assert body(screen).startswith(
        "🛒 Aqua Element 19L\n"
        "💰 Narxi: 15,000 UZS (1 dona)\n"
        f"{uz('telegram.products.contract_price_at_checkout')}\n"
        "ℹ️ Minimal buyurtma: 2\n"
    )


async def test_a_refused_checkout_says_why_and_never_opens_checkout(bot, shop, user):
    add_address(bot, 901, "Uy", "Chilonzor 9-kvartal, 15-uy")
    await open_product(bot, user)
    bot.backend.route(
        "POST", "/api/v1/cart/items",
        lambda _call: backend_failure("Only 3 left in stock", status_code=400),
    )

    await bot.send(user.tap(f"buy_{BOTTLE_19L}_12"))

    answers = [call.params.get("text") for call in bot.telegram.of("answerCallbackQuery")]
    assert any("Only 3 left in stock" in (text or "") for text in answers)
    assert shop.cart == {}
    assert backend_calls(bot, "GET", "/api/v1/auth/addresses") == []


async def test_checkout_over_a_cart_it_cannot_read_writes_nothing(bot, shop, user):
    shop.cart[BOTTLE_19L] = 10
    await open_product(bot, user)
    bot.backend.route("GET", "/api/v1/cart", lambda _call: backend_failure("HTTP 502", status_code=502))

    await bot.send(user.tap(f"buy_{BOTTLE_19L}_15"))

    assert cart_writes(bot) == []
    assert shop.cart == {BOTTLE_19L: 10}
    assert backend_calls(bot, "GET", "/api/v1/auth/addresses") == []


@pytest.mark.parametrize("legacy", ["add_to_cart_{id}", "back_to_product_{id}"])
async def test_buttons_on_bubbles_from_before_the_change_open_the_quantity_screen(bot, shop, user, legacy):
    """Product cards and quantity screens drawn before this release stay in customers' chats."""
    await bot.send(user.tap(legacy.format(id=BOTTLE_19L), message_id=LIST_MESSAGE))

    screen = bot.telegram.last_shown()
    assert "Soni: 2\n" in body(screen)
    assert f"buy_{BOTTLE_19L}_2" in screen.callback_data()
    assert cart_writes(bot) == []


async def test_a_product_tapped_on_a_category_photo_arrives_as_its_own_photo(bot, shop, user):
    """The list bubble shows the CATEGORY's picture; editing its caption would put the product's
    quantity screen under the wrong picture."""
    shop.products[BOTTLE_19L]["image_url"] = "https://cdn.example.com/19l.png"

    await bot.send(photo_tap(user, f"product_{BOTTLE_19L}", LIST_MESSAGE))

    assert bot.telegram.of("editMessageCaption") == []
    assert [int(call.params["message_id"]) for call in bot.telegram.of("deleteMessage")] == [LIST_MESSAGE]
    (photo,) = bot.telegram.of("sendPhoto")
    assert photo.params["photo"] == "https://cdn.example.com/19l.png"
    assert body(photo).startswith("🛒 Aqua Element 19L\n")


async def test_a_checkout_for_zero_writes_nothing(bot, shop, user):
    """No rendered button carries 0, and the backend reads a PUT of 0 as "remove the line"."""
    shop.cart[BOTTLE_19L] = 10

    await bot.send(user.tap(f"buy_{BOTTLE_19L}_0"))

    assert cart_writes(bot) == []
    assert shop.cart == {BOTTLE_19L: 10}
