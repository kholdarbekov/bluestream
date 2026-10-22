"""💧 Order water, 🛒 Order again and Products → Repeat last, through the real dispatcher.

Spec docs/superpowers/specs/2026-10-21-customer-bot-bottom-menu-and-reorder-design.md §3 (bot half),
R8, R9. The backend alone decides which order is "last", which lines a reorder copies and whether
an order is open, so the bot is driven against GET /api/v1/orders/reorder-info and
POST /api/v1/cart/fill-from-order/<id> in the plan's Shared Interfaces shapes, on top of the
checkout journey's in-memory `Shop` (cart, payment methods, estimate, orders).
"""

from __future__ import annotations

import json

import pytest

import bot as bot_module  # noqa: F401

# Bare bot-module names: already in sys.modules after `import bot`.
import utils as utils_module
from handlers.base import BaseHandler
from keyboards import DeliverySummaryKeyboards, ReorderReminderKeyboards
from menu_keyboard import MENU_KEYBOARD_STATE_KEY, wanted_menu_labels
from telegram import Update

# AFTER the bot import, for the sys.path reason test_agent_order_callbacks.py documents.
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS  # noqa: E402
from scripts.seed_bottle_ledger_translations import KEYS as LEDGER_COPY  # noqa: E402

from tests.telegram_bot.ptb_harness import backend_failure, build_bot_harness
from tests.telegram_bot.test_checkout_journey_dispatcher import (
    BOTTLE_19L,
    BOTTLE_5L,
    Shop,
    add_address,
    backend_calls,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

# Production copy: every seeded telegram.* row in all three languages. The customer row reads uz.
TRANSLATIONS = {
    (language, key): row[language]
    for key, row in BACKEND_TRANSLATIONS.items()
    if key.startswith("telegram.")
    for language in ("uz", "ru", "en")
    if row.get(language)
}
TRANSLATIONS.update({(language, key): row[language] for key, row in LEDGER_COPY.items() for language in ("uz", "ru", "en")})


def uz(key: str) -> str:
    return BACKEND_TRANSLATIONS[key]["uz"]


REORDER_INFO = "/api/v1/orders/reorder-info"
CANDIDATE_ORDER_ID = 9001
OPEN_ORDER_ID = 9100
HOME = (901, "Uy", "Chilonzor 9-kvartal, 15-uy")
WORK = (902, "Ish", "Yunusobod 4-kvartal, 2-uy")
DACHA = (903, "Dacha", "Qibray, Bobur ko'chasi 7")
PRODUCTS_MESSAGE = 4100
REMINDER_MESSAGE = 4300
CANDIDATE = {
    "order_id": CANDIDATE_ORDER_ID,
    "order_number": "TG_000412_26",
    "items": [{"product_id": BOTTLE_19L, "product_name": "Aqua Element 19L", "quantity": 3}],
    "address": {"id": HOME[0], "title": HOME[1], "full_address": HOME[2]},
}
OPEN_ORDER = {"id": OPEN_ORDER_ID, "order_number": "TG_000501_26", "status": "out_for_delivery"}


def save_addresses(bot, *addresses):
    for address_id, title, full_address in addresses:
        add_address(bot, address_id, title, full_address)


def fill_path(order_id: int) -> str:
    return f"/api/v1/cart/fill-from-order/{order_id}"


def fill_calls(bot) -> list:
    return [
        call for call in bot.backend.calls
        if call.method == "POST" and call.endpoint.startswith("/api/v1/cart/fill-from-order/")
    ]


def serve_reorder(bot, shop, *, candidate=CANDIDATE, open_order=None, added=None, dropped=(),
                  order_id=CANDIDATE_ORDER_ID):
    """The backend's two reorder routes, in the Shared Interfaces shapes. The fill REPLACES the
    Shop's cart with `added` (CartService.fill_from_order); `added` defaults to the candidate's lines."""
    bot.backend.route(
        "GET", REORDER_INFO,
        lambda _call: {"success": True, "data": {"candidate": candidate, "open_order": open_order}},
    )
    if added is None:
        added = [
            {"product_id": item["product_id"], "quantity": item["quantity"]}
            for item in (candidate or {}).get("items", [])
        ]

    def fill(_call):
        shop.cart.clear()
        shop.cart.update({line["product_id"]: line["quantity"] for line in added})
        return {"success": True, "data": {"added": list(added), "dropped": list(dropped)}}

    bot.backend.route("POST", fill_path(order_id), fill)


def touched(bot, message_id: int) -> list:
    """Every edit or delete Telegram received for one message."""
    return [
        call for call in bot.telegram.of(
            "editMessageText", "editMessageReplyMarkup", "editMessageCaption", "deleteMessage",
        )
        if int(call.params.get("message_id", 0)) == message_id
    ]


def order_again_on_a_reminder(order_id: int = CANDIDATE_ORDER_ID) -> str:
    """The reminder's own Order-again callback, from the real builder."""
    markup = ReorderReminderKeyboards.reminder(
        "uz", episode_id=77, reorder_order_id=order_id, show_later=True, snooze_days=14
    )
    return markup.inline_keyboard[0][0].callback_data


async def _always_allowed(*_args, **_kwargs):
    return True


@pytest.fixture
async def bot(monkeypatch):
    harness = await build_bot_harness(monkeypatch, translations=TRANSLATIONS)
    # Several texts per test from one customer: "allowed" is the normal production answer
    # (the same stub test_menu_label_routing.py uses).
    monkeypatch.setattr(utils_module.rate_limiter, "allow_request", _always_allowed)
    # A returning customer whose bottom keyboard is already on screen (Task 14's marker), so the
    # Task 17 lazy installer stays quiet and every screen read below is the one under test.
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


async def test_repeat_last_names_the_backends_candidate(bot, shop, user):
    serve_reorder(bot, shop)

    await bot.send(user.tap("menu_products"))

    screen = bot.telegram.last_shown()
    assert screen.button_labels()[0] == "🔁 Oxirgisini takrorlash: 3× Aqua Element 19L"
    assert screen.callback_data()[0] == "quick_repeat_last"
    assert backend_calls(bot, "GET", "/api/v1/orders/") == []


async def test_repeat_last_fills_the_cart_from_the_candidate_in_one_call(bot, shop, user):
    save_addresses(bot, HOME)
    serve_reorder(bot, shop)

    await bot.send(user.tap("quick_repeat_last", message_id=PRODUCTS_MESSAGE))

    assert [(c.endpoint, c.data, c.user_token) for c in fill_calls(bot)] == [
        (fill_path(CANDIDATE_ORDER_ID), None, "test-access-token")
    ]
    assert backend_calls(bot, "POST", "/api/v1/cart/items") == []
    assert backend_calls(bot, "POST", "/api/v1/cart/clear") == []
    assert backend_calls(bot, "GET", "/api/v1/orders/") == []
    assert shop.cart == {BOTTLE_19L: 3}
    picker = bot.telegram.last_shown()
    assert picker.method == "sendMessage"
    assert picker.text.startswith(uz("telegram.orders.select_payment"))
    assert "payment_cash" in picker.callback_data()
    assert touched(bot, PRODUCTS_MESSAGE) == []


async def test_order_again_on_a_reminder_never_edits_the_reminder(bot, shop, user):
    save_addresses(bot, HOME)
    serve_reorder(bot, shop)

    await bot.send(user.tap(order_again_on_a_reminder(), message_id=REMINDER_MESSAGE))

    assert touched(bot, REMINDER_MESSAGE) == []
    assert [c.endpoint for c in fill_calls(bot)] == [fill_path(CANDIDATE_ORDER_ID)]
    assert backend_calls(bot, "GET", f"/api/v1/orders/{CANDIDATE_ORDER_ID}") == []
    picker = bot.telegram.last_shown()
    assert picker.method == "sendMessage"
    assert picker.text.startswith(uz("telegram.orders.select_payment"))


async def test_some_lines_dropped_shows_the_notice_then_the_cart(bot, shop, user):
    save_addresses(bot, HOME)
    serve_reorder(
        bot, shop,
        added=[{"product_id": BOTTLE_19L, "quantity": 3}],
        dropped=[{"product_id": BOTTLE_5L, "quantity": 2, "reason": "inactive"}],
    )

    await bot.send(user.tap("quick_repeat_last"))

    notice, cart = bot.telegram.shown[-2:]
    assert notice.text == uz("telegram.quick_order.unavailable")
    assert cart.text.startswith(uz("telegram.cart_title") + ":")
    assert "Aqua Element 19L x 3" in cart.text
    assert backend_calls(bot, "GET", "/api/v1/payments/methods") == []


async def test_every_line_dropped_shows_the_notice_then_products(bot, shop, user):
    serve_reorder(
        bot, shop, added=[], dropped=[{"product_id": BOTTLE_19L, "quantity": 3, "reason": "unavailable"}],
    )

    await bot.send(user.tap("quick_repeat_last"))

    notice, products = bot.telegram.shown[-2:]
    assert notice.text == uz("telegram.quick_order.unavailable")
    assert products.text == uz("telegram.menu.products")
    assert any(data.startswith("category_") for data in products.callback_data())
    assert backend_calls(bot, "GET", "/api/v1/auth/addresses") == []


@pytest.mark.parametrize(
    "method, endpoint",
    [("GET", REORDER_INFO), ("POST", fill_path(CANDIDATE_ORDER_ID))],
    ids=["reorder-info", "fill"],
)
async def test_a_failed_reorder_call_says_so_in_a_new_message(bot, shop, user, method, endpoint):
    """A backend failure is not "no history", and it is shown as a message, not as a second
    answerCallbackQuery that Telegram never displays."""
    shop.cart[BOTTLE_5L] = 4
    save_addresses(bot, HOME)
    serve_reorder(bot, shop)
    bot.backend.route(method, endpoint, lambda _call: backend_failure("HTTP 502", status_code=502))

    await bot.send(user.tap("quick_repeat_last", message_id=PRODUCTS_MESSAGE))

    shown = bot.telegram.last_shown()
    assert (shown.method, shown.text) == ("sendMessage", uz("telegram.error.generic"))
    assert uz("telegram.quick_order.no_history") not in bot.telegram.texts()
    assert touched(bot, PRODUCTS_MESSAGE) == []
    assert shop.cart == {BOTTLE_5L: 4}


async def test_an_unregistered_tap_on_order_again_never_overwrites_the_reminder(bot, shop, user):
    bot.database.user = None
    serve_reorder(bot, shop)

    await bot.send(user.tap(order_again_on_a_reminder(), message_id=REMINDER_MESSAGE))

    assert touched(bot, REMINDER_MESSAGE) == []
    assert bot.telegram.last_shown().method == "sendMessage"
    assert fill_calls(bot) == []


async def test_a_tap_whose_message_telegram_did_not_include_is_left_as_it_is():
    """PTB 22.3: `effective_message` is None for an inline or inaccessible message. Wrapping it
    would hand checkout `message=None`. profile.py's old rule passed such an update through."""
    update = Update.de_json({
        "update_id": 1,
        "callback_query": {
            "id": "cb1", "from": {"id": 55501, "is_bot": False, "first_name": "Kamola"},
            "chat_instance": "ci", "inline_message_id": "im-1", "data": "reorder_9001",
        },
    }, None)

    assert BaseHandler.as_message_update(update) is update


OPEN_ORDER_DETAIL = {
    "id": OPEN_ORDER_ID, "order_number": "TG_000501_26", "status": "out_for_delivery",
    "created_at": "2026-10-21T08:00:00+00:00", "total_amount": 45000, "order_items": [],
    "can_customer_cancel": False, "payment_info": {"is_payable": False},
}


async def test_order_water_with_items_in_the_cart_shows_the_cart(bot, shop, user):
    shop.cart[BOTTLE_5L] = 4
    serve_reorder(bot, shop)

    await bot.send(user.text(uz("telegram.menu.products")))

    cart = bot.telegram.last_shown()
    assert cart.text.startswith(uz("telegram.cart_title") + ":")
    assert "Aqua Element 5L x 4" in cart.text
    assert fill_calls(bot) == []
    assert shop.cart == {BOTTLE_5L: 4}


async def test_order_water_with_items_in_the_cart_and_an_open_order_says_so_first(bot, shop, user):
    """R9: the notice comes first; continuing then shows the cart, as with no open order."""
    shop.cart[BOTTLE_5L] = 4
    serve_reorder(bot, shop, open_order=OPEN_ORDER)

    await bot.send(user.text(uz("telegram.menu.products")))
    assert bot.telegram.last_shown().callback_data() == [f"order_{OPEN_ORDER_ID}", "order_water_go"]

    await bot.send(user.tap("order_water_go"))

    cart = bot.telegram.last_shown()
    assert cart.text.startswith(uz("telegram.cart_title") + ":") and "Aqua Element 5L x 4" in cart.text
    assert fill_calls(bot) == [] and shop.cart == {BOTTLE_5L: 4}
    assert len(backend_calls(bot, "GET", REORDER_INFO)) == 1  # the tap on Order water, none after


async def test_order_water_never_fills_over_a_cart_it_could_not_read(bot, shop, user):
    """Spec §3: an order in progress is never wiped by this button, even when the cart read fails."""
    shop.cart[BOTTLE_5L] = 4
    serve_reorder(bot, shop)
    bot.backend.route("GET", "/api/v1/cart", lambda _call: backend_failure("HTTP 502", status_code=502))

    await bot.send(user.text(uz("telegram.menu.products")))

    assert bot.telegram.last_shown().text == uz("telegram.error.generic")
    assert backend_calls(bot, "GET", REORDER_INFO) == []
    assert fill_calls(bot) == []
    assert shop.cart == {BOTTLE_5L: 4}


async def test_a_cart_the_cart_screen_calls_empty_is_refilled(bot, shop, user):
    """One rule for "the cart holds something": `products.cart_is_empty`, which the cart screen
    already applies. A server-dropped line is listed at 0 and has nothing to check out."""
    shop.cart[BOTTLE_5L] = 2
    shop.line_total_overrides[BOTTLE_5L] = 0.0
    shop.subtotal_override = 0.0
    save_addresses(bot, HOME)
    serve_reorder(bot, shop)

    await bot.send(user.text(uz("telegram.menu.products")))

    assert [c.endpoint for c in fill_calls(bot)] == [fill_path(CANDIDATE_ORDER_ID)]
    assert shop.cart == {BOTTLE_19L: 3}
    assert bot.telegram.last_shown().text.startswith(uz("telegram.orders.select_payment"))


async def test_order_water_with_an_open_order_says_so_first(bot, shop, user):
    serve_reorder(bot, shop, open_order=OPEN_ORDER)

    await bot.send(user.text(uz("telegram.menu.products")))

    notice = bot.telegram.last_shown()
    assert notice.text == "📦 Sizda faol buyurtma bor: TG_000501_26 — Yetkazib berish yo'lida."
    assert notice.params.get("parse_mode") is None
    assert notice.callback_data() == [f"order_{OPEN_ORDER_ID}", "order_water_go"]
    assert notice.button_labels() == [uz("telegram.payment.view_order"), uz("telegram.reorder_reminder.btn_order")]
    assert fill_calls(bot) == []


async def test_view_order_on_the_notice_opens_that_order(bot, shop, user):
    serve_reorder(bot, shop, open_order=OPEN_ORDER)
    bot.backend.route(
        "GET", f"/api/v1/orders/{OPEN_ORDER_ID}",
        lambda _call: {"data": {"order": dict(OPEN_ORDER_DETAIL), "delivery": None}},
    )

    await bot.send(user.text(uz("telegram.menu.products")))
    notice = bot.telegram.last_shown()
    await bot.send(user.tap(notice.callback_data()[0]))

    assert len(backend_calls(bot, "GET", f"/api/v1/orders/{OPEN_ORDER_ID}")) == 1
    assert f"track_order_{OPEN_ORDER_ID}" in bot.telegram.last_shown().callback_data()


async def test_continuing_past_the_notice_opens_products_and_never_repeats_the_last_order(bot, shop, user):
    """Owner 2026-10-22: after 💧 Order water the notice's Order again starts a FRESH order."""
    save_addresses(bot, HOME)
    serve_reorder(bot, shop, open_order=OPEN_ORDER)

    await bot.send(user.text(uz("telegram.menu.products")))
    notice = bot.telegram.last_shown()
    assert notice.callback_data() == [f"order_{OPEN_ORDER_ID}", "order_water_go"]
    reads_before = len(backend_calls(bot, "GET", REORDER_INFO))
    await bot.send(user.tap("order_water_go"))

    products = bot.telegram.last_shown()
    assert products.text == uz("telegram.menu.products")
    assert any(data.startswith("category_") for data in products.callback_data())
    # Ruling: Products stays the normal catalog, including its "Repeat last" row, which sits above
    # the categories. Showing that row is not a repeat; only tapping it would be.
    assert products.callback_data()[0] == "quick_repeat_last"
    assert products.button_labels()[0] == "🔁 Oxirgisini takrorlash: 3× Aqua Element 19L"
    assert fill_calls(bot) == []
    # Same ruling: the ONE reorder-info read belongs to that row (build_quick_suggestions), not to
    # the notice handler; the cart-holds-something test above asserts the handler itself makes none.
    assert len(backend_calls(bot, "GET", REORDER_INFO)) == reads_before + 1
    assert not [t for t in bot.telegram.texts() if t.startswith(uz("telegram.orders.select_payment"))]
    assert backend_calls(bot, "POST", "/api/v1/orders") == []
    assert shop.cart == {}


async def test_continuing_past_the_notice_lets_the_customer_choose_product_quantity_and_address(bot, shop, user):
    """The real use case end to end: notice -> category -> product -> quantity -> checkout
    -> the customer picks the address; the last order is never copied. Every callback is read
    from the screen the customer is looking at."""
    save_addresses(bot, HOME, WORK)
    serve_reorder(bot, shop, open_order=OPEN_ORDER)

    def pick(prefix: str, suffix: str = "") -> str:
        shown = bot.telegram.last_shown().callback_data()
        matches = [d for d in shown if d.startswith(prefix) and d.endswith(suffix)]
        assert matches, f"no {prefix!r}...{suffix!r} button on {shown}"
        return matches[0]

    await bot.send(user.text(uz("telegram.menu.products")))
    assert bot.telegram.last_shown().callback_data() == [f"order_{OPEN_ORDER_ID}", "order_water_go"]
    await bot.send(user.tap("order_water_go"))

    await bot.send(user.tap(pick("category_")))
    await bot.send(user.tap(pick(f"product_{BOTTLE_5L}")))
    await bot.send(user.tap(pick("add_to_cart_")))
    await bot.send(user.tap(pick(f"qty_set_{BOTTLE_5L}_", "_7")))
    assert shop.cart == {BOTTLE_5L: 7}

    # The quantity screen's own Checkout button.
    assert "checkout" in bot.telegram.last_shown().callback_data()
    await bot.send(user.tap(pick("checkout")))

    address_step = bot.telegram.last_shown()
    assert address_step.text == uz("telegram.orders.select_address")
    assert {f"address_{HOME[0]}", f"address_{WORK[0]}"} <= set(address_step.callback_data())

    await bot.send(user.tap(pick(f"address_{WORK[0]}")))

    assert bot.telegram.last_shown().text.startswith(uz("telegram.orders.select_payment"))
    (methods_query,) = backend_calls(bot, "GET", "/api/v1/payments/methods")
    assert methods_query.params == {"context": "order", "delivery_address_id": WORK[0]}
    assert shop.cart == {BOTTLE_5L: 7}
    assert fill_calls(bot) == []
    assert backend_calls(bot, "POST", "/api/v1/orders") == []


async def test_continuing_past_the_notice_never_fills_over_a_cart_it_could_not_read(bot, shop, user):
    shop.cart[BOTTLE_5L] = 4
    serve_reorder(bot, shop, open_order=OPEN_ORDER)

    await bot.send(user.text(uz("telegram.menu.products")))
    notice = bot.telegram.last_shown()
    assert notice.callback_data() == [f"order_{OPEN_ORDER_ID}", "order_water_go"]
    reads_before = len(backend_calls(bot, "GET", REORDER_INFO))
    bot.backend.route("GET", "/api/v1/cart", lambda _call: backend_failure("HTTP 502", status_code=502))
    await bot.send(user.tap("order_water_go"))

    assert bot.telegram.last_shown().text == uz("telegram.error.generic")
    assert len(backend_calls(bot, "GET", REORDER_INFO)) == reads_before
    assert fill_calls(bot) == []
    assert shop.cart == {BOTTLE_5L: 4}


async def test_order_water_with_a_candidate_goes_straight_to_payment(bot, shop, user):
    save_addresses(bot, HOME)
    serve_reorder(bot, shop)

    await bot.send(user.text(uz("telegram.menu.products")))

    assert [c.endpoint for c in fill_calls(bot)] == [fill_path(CANDIDATE_ORDER_ID)]
    picker = bot.telegram.last_shown()
    assert picker.method == "sendMessage"
    assert picker.text.startswith(uz("telegram.orders.select_payment"))


async def test_order_water_without_a_candidate_opens_products(bot, shop, user):
    serve_reorder(bot, shop, candidate=None)

    await bot.send(user.text(uz("telegram.menu.products")))

    # Only order_water reads the cart and then the reorder answer BEFORE Products asks for categories.
    assert [c.endpoint for c in bot.backend.calls if c.method == "GET"][:2] == ["/api/v1/cart", REORDER_INFO]
    products = bot.telegram.last_shown()
    assert products.text == uz("telegram.menu.products")
    assert any(data.startswith("category_") for data in products.callback_data())
    assert fill_calls(bot) == []


async def test_order_again_with_an_open_order_says_so_and_continues_with_its_own_callback(bot, shop, user):
    save_addresses(bot, HOME)
    serve_reorder(bot, shop, open_order=OPEN_ORDER)

    await bot.send(user.tap(order_again_on_a_reminder(), message_id=REMINDER_MESSAGE))

    notice = bot.telegram.last_shown()
    assert notice.method == "sendMessage"
    assert notice.callback_data() == [f"order_{OPEN_ORDER_ID}", f"reorder_go_{CANDIDATE_ORDER_ID}"]
    assert fill_calls(bot) == []

    await bot.send(user.tap(notice.callback_data()[1]))

    assert [c.endpoint for c in fill_calls(bot)] == [fill_path(CANDIDATE_ORDER_ID)]
    assert bot.telegram.last_shown().text.startswith(uz("telegram.orders.select_payment"))
    assert touched(bot, REMINDER_MESSAGE) == []


async def test_two_order_water_taps_fill_once_and_never_place_an_order(bot, shop, user):
    """Review Focus 2 as production runs it: per-chat serial updates (bot.py:494-522), so the
    second tap reads the cart the first one filled, and shows it."""
    save_addresses(bot, HOME)
    serve_reorder(bot, shop)

    await bot.send(user.text(uz("telegram.menu.products")))
    await bot.send(user.text(uz("telegram.menu.products")))

    assert [c.endpoint for c in fill_calls(bot)] == [fill_path(CANDIDATE_ORDER_ID)]
    assert shop.cart == {BOTTLE_19L: 3}
    assert backend_calls(bot, "POST", "/api/v1/cart/items") == []
    assert backend_calls(bot, "POST", "/api/v1/orders") == []
    picker, cart = bot.telegram.shown[-2:]
    assert picker.text.startswith(uz("telegram.orders.select_payment"))
    assert cart.text.startswith(uz("telegram.cart_title") + ":")


async def test_two_order_water_taps_that_both_read_an_empty_cart_refill_the_same_order(bot, shop, user):
    """Review Focus 2, worst case: both taps read the cart before either fill lands. Two fills of
    the SAME order (each replaces the cart), never a doubled cart, two identical pickers, zero orders."""
    save_addresses(bot, HOME)
    serve_reorder(bot, shop)
    bot.backend.route("GET", "/api/v1/cart", lambda _call: {"data": {"cart": {"cart_items": [], "subtotal": 0}}})

    await bot.send(user.text(uz("telegram.menu.products")))
    await bot.send(user.text(uz("telegram.menu.products")))

    assert [c.endpoint for c in fill_calls(bot)] == [fill_path(CANDIDATE_ORDER_ID)] * 2
    assert shop.cart == {BOTTLE_19L: 3}
    assert backend_calls(bot, "POST", "/api/v1/cart/items") == []
    assert backend_calls(bot, "POST", "/api/v1/orders") == []
    pickers = [c for c in bot.telegram.shown if c.text.startswith(uz("telegram.orders.select_payment"))]
    assert len(pickers) == 2
    assert (pickers[0].text, pickers[0].callback_data()) == (pickers[1].text, pickers[1].callback_data())


async def test_order_water_with_two_addresses_skips_the_card_and_confirms_the_full_address(bot, shop, user):
    save_addresses(bot, HOME, WORK)
    serve_reorder(bot, shop)

    await bot.send(user.text(uz("telegram.menu.products")))

    picker = bot.telegram.last_shown()
    assert picker.method == "sendMessage"
    assert picker.text.startswith(uz("telegram.orders.select_payment"))
    assert not [t for t in bot.telegram.texts() if t.startswith(uz("telegram.checkout.delivering_to"))]
    assert uz("telegram.error_occurred") not in bot.telegram.texts()

    await bot.send(user.tap("payment_cash"))

    confirmation = bot.telegram.last_shown()
    assert confirmation.text.startswith(uz("telegram.orders.confirmation_title"))
    assert f"{uz('telegram.delivery_address')}: Uy\nChilonzor 9-kvartal, 15-uy\n\n" in confirmation.text
    assert confirmation.params.get("parse_mode") is None
    buttons = dict(zip(confirmation.button_labels(), confirmation.callback_data()))
    assert buttons[uz("telegram.checkout.change_address")] == "checkout_change_address"

    await bot.send(user.tap(buttons[uz("telegram.checkout.change_address")]))

    address_picker = bot.telegram.last_shown()
    assert address_picker.text == uz("telegram.orders.select_address")
    assert {f"address_{HOME[0]}", f"address_{WORK[0]}"} <= set(address_picker.callback_data())


@pytest.mark.parametrize(
    "saved, candidate_address, first_screen_key",
    [
        # The backend says the order's address is no longer saved: the normal checkout (guard).
        ((HOME,), None, "telegram.checkout.delivering_to"),
        # Saved when reorder-info was read, deleted before checkout: never another address in its place.
        ((WORK,), CANDIDATE["address"], "telegram.checkout.delivering_to"),
        ((WORK, DACHA), CANDIDATE["address"], "telegram.orders.select_address"),
    ],
)
async def test_a_candidate_without_a_saved_address_runs_the_normal_checkout(
    bot, shop, user, saved, candidate_address, first_screen_key
):
    save_addresses(bot, *saved)
    serve_reorder(bot, shop, candidate={**CANDIDATE, "address": candidate_address})

    await bot.send(user.text(uz("telegram.menu.products")))

    assert bot.telegram.last_shown().text.startswith(uz(first_screen_key))
    assert backend_calls(bot, "GET", "/api/v1/payments/methods") == []


async def test_your_usual_with_two_addresses_still_shows_the_delivering_to_card(bot, shop, user):
    """Pending owner question (Task 21): only reorders skip the card; ⭐ Your usual keeps it."""
    save_addresses(bot, HOME, WORK)
    bot.backend.route("GET", "/api/v1/orders/quick-reorder", lambda _call: {"data": {"quick_reorder_suggestions": [
        {"product_id": BOTTLE_5L, "product_name": "Aqua Element 5L", "suggested_quantity": 2, "in_stock": True},
    ]}})

    await bot.send(user.tap("quick_usual"))

    card = bot.telegram.last_shown()
    assert card.text.startswith(uz("telegram.checkout.delivering_to"))
    assert {"checkout_change_address", "menu_products"} <= set(card.callback_data())
    assert fill_calls(bot) == []


SUMMARY_MESSAGE = 4242


async def test_the_summary_keyboard_offers_order_again_above_report(bot):
    markup = DeliverySummaryKeyboards.summary("uz", 1234, 9001)

    assert [[(b.text, b.callback_data) for b in row] for row in markup.inline_keyboard] == [
        [(uz("telegram.reorder_reminder.btn_order"), "reorder_9001")],
        [(LEDGER_COPY["telegram.delivery_summary.report_button"]["uz"], "report_issue_1234")],
    ]
    only_report = DeliverySummaryKeyboards.summary("uz", 1234, None)
    assert [[b.callback_data for b in row] for row in only_report.inline_keyboard] == [["report_issue_1234"]]


def _order_again_on_the_summary() -> str:
    return DeliverySummaryKeyboards.summary("uz", 1234, CANDIDATE_ORDER_ID).inline_keyboard[0][0].callback_data


async def test_order_again_on_the_summary_leaves_the_summary_untouched(bot, shop, user):
    save_addresses(bot, HOME)
    serve_reorder(bot, shop)

    await bot.send(user.tap(_order_again_on_the_summary(), message_id=SUMMARY_MESSAGE))

    assert touched(bot, SUMMARY_MESSAGE) == []
    assert [c.endpoint for c in fill_calls(bot)] == [fill_path(CANDIDATE_ORDER_ID)]
    picker = bot.telegram.last_shown()
    assert picker.method == "sendMessage"
    assert picker.text.startswith(uz("telegram.orders.select_payment"))


async def test_order_again_on_an_order_whose_products_are_gone_offers_products(bot, shop, user):
    """Review Focus 5: no empty checkout, no crash."""
    serve_reorder(
        bot, shop, added=[], dropped=[{"product_id": BOTTLE_19L, "quantity": 3, "reason": "inactive"}],
    )

    await bot.send(user.tap(_order_again_on_the_summary(), message_id=SUMMARY_MESSAGE))

    notice, products = bot.telegram.shown[-2:]
    assert notice.text == uz("telegram.quick_order.unavailable")
    assert products.text == uz("telegram.menu.products")
    assert backend_calls(bot, "GET", "/api/v1/auth/addresses") == []
    assert backend_calls(bot, "GET", "/api/v1/payments/methods") == []
    assert touched(bot, SUMMARY_MESSAGE) == []
    assert uz("telegram.error_occurred") not in bot.telegram.texts()
