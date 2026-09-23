"""Typing the quantity on the cart quantity screen, driven through the REAL dispatcher.

WHY THIS FILE EXISTS
--------------------
The quantity screen (``ProductHandlers._render_quantity_step``) offered only
buttons. A customer who TYPED "7" instead was never answered: with no flow open
the group-0 catch-all (``WaterBusinessBot._handle_text_message``) silently filed
their "7" into the admin Support Inbox, and the cart stayed at the product
minimum. Nothing on screen changed, so they kept typing.

What a typed message on that screen now means:

* a whole number inside the product's bounds SETS the line to it (the same
  absolute write a preset button makes) and a fresh screen replaces the old;
* 0, below the minimum, above stock or the per-item cap: refused with the limit,
  nothing written;
* "2.5", "-3", "+5": a hint to type a whole number;
* anything else: the Support Inbox, exactly as before.

A number only counts while the quantity screen is the last thing the customer
touched: any other tap (including one on an OLDER quantity bubble), a command,
or any message that is not a quantity closes the window, and it expires after
30 minutes. The window lives in ``users.bot_state`` so a deploy cannot reopen
the old bug.

Every update goes in through ``Application.process_update`` against the three
harness seams (Telegram's transport, ``_make_request``, the bot's SQL). The
backend cart is ``ServerCart``: POST increments, PUT sets — the semantics the
2026-06-27 accumulation bug came from.
"""

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest
from telegram import Update

from shared.business_config import MAX_QUANTITY_PER_ITEM
from tests.telegram_bot.ptb_harness import backend_failure, build_bot_harness
from tests.telegram_bot.test_cart_and_quantity_journeys import (
    TRANSLATIONS as CART_TRANSLATIONS,
    SPORT_ID,
    WATER_ID,
    WATER_MIN_QTY,
    WATER_NAME,
    ServerCart,
    api_calls,
    build_catalogue,
    install_cart_backend,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


SUPPORT_ENDPOINT = "/api/v1/support/messages"
WATER_STOCK = 40  # build_catalogue()'s stock for WATER_ID: the ceiling here

ORDER_ID = 555
ORDER_NUMBER = "ORD-2026-0555"

PROFILE = "/api/v1/auth/profile"
SEND_OTP = "/api/v1/auth/send-otp"
VERIFY_OTP = "/api/v1/auth/verify-phone"

# The seeded Uzbek copy (scripts/seed_backend_translations.py). Assertions
# compare against these exact strings: "the customer was told something" is not
# the claim, "the customer was told the limit" is.
TRANSLATIONS = {
    **CART_TRANSLATIONS,
    ("uz", "telegram.products.type_quantity_hint"): "✏️ Yoki kerakli sonni yozib yuboring",
    ("uz", "telegram.products.typed_quantity_below_min"): (
        "Bu mahsulot uchun eng kam buyurtma — {min_qty} ta. "
        "{min_qty} yoki undan ko'p son yozing."
    ),
    ("uz", "telegram.products.typed_quantity_above_max"): (
        "Hozir bu mahsulotdan ko'pi bilan {max_qty} ta buyurtma qilish mumkin. "
        "Kichikroq son yozing."
    ),
    ("uz", "telegram.products.typed_quantity_not_whole"): (
        "Iltimos, {min_qty} dan {max_qty} gacha butun son yozing."
    ),
    ("uz", "telegram.support.describe_issue_prompt"): (
        "Iltimos, #{order_number}-buyurtma bo'yicha muammoni yozib yuboring."
    ),
    ("uz", "telegram.support.cancel_button"): "Bekor qilish",
    ("uz", "telegram.support.ack"): "✅ Rahmat! Xabaringiz qo'llab-quvvatlash guruhiga yuborildi.",
}


def t(key: str, **fmt) -> str:
    """The seeded Uzbek string, filled the way the handler fills it."""
    return TRANSLATIONS[("uz", key)].format(**fmt)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def catalogue():
    return build_catalogue()


@pytest.fixture
def cart(catalogue):
    return ServerCart(catalogue)


@pytest.fixture
async def bot(monkeypatch, cart):
    harness = await build_bot_harness(monkeypatch, translations=TRANSLATIONS)
    install_cart_backend(harness.backend, cart)
    harness.backend.route(
        "GET",
        f"/api/v1/orders/{ORDER_ID}",
        lambda _c: {"data": {"order": {"id": ORDER_ID, "order_number": ORDER_NUMBER}}},
    )
    harness.backend.route(
        "GET",
        PROFILE,
        lambda _c: {"data": {
            "id": 398, "phone": "+998978730111", "phone_verified": False,
            "first_name": "Kamola",
        }},
    )
    harness.backend.route("POST", SEND_OTP, lambda _c: {"data": {"sent": True}})
    harness.backend.route("POST", VERIFY_OTP, lambda _c: {"data": {"verified": True}})
    return harness


@pytest.fixture
def user(bot, request):
    """A customer id unique to THIS test.

    Typed text runs the REAL Redis-backed ``RateLimiter`` (30 messages / 60s /
    user). Sharing one id across the file would let a later test be told "rate
    limit exceeded" instead of the behaviour it asserts, and only when the
    whole file runs. Same derivation as test_support_and_text_routing.py.
    """
    digest = hashlib.sha256(request.node.nodeid.encode("utf-8")).hexdigest()[:8]
    telegram_id = 780_000_000 + int(digest, 16) % 1_000_000
    return bot.updates(user_id=telegram_id, chat_id=telegram_id)


# ---------------------------------------------------------------------------
# Readers and journeys
# ---------------------------------------------------------------------------

QUANTITY_SCREEN_MESSAGE_ID = 321  # the bubble the customer tapped "Add to cart" on


async def open_quantity_screen(bot, user, product_id=WATER_ID, message_id=QUANTITY_SCREEN_MESSAGE_ID):
    """Tap 'Add to cart' on the product card (message 321 unless told otherwise)."""
    await bot.send(user.tap(f"add_to_cart_{product_id}", message_id=message_id))
    screen = bot.telegram.last_shown()
    # Start every assertion from "after the screen opened": opening it already
    # POSTed the line at the product minimum, which is add_to_cart's business.
    bot.telegram.reset()
    bot.backend.calls.clear()
    return screen


def photo_tap(user, callback_data: str, message_id: int) -> Update:
    """A tap on a PHOTO bubble — the product card of every product with a
    picture, and therefore the usual way into the quantity screen.
    ``UpdateFactory.tap`` only builds text bubbles."""
    update = user.tap(callback_data, message_id=message_id).to_dict()
    message = update["callback_query"]["message"]
    del message["text"]
    message["photo"] = [{"file_id": "card", "file_unique_id": "u-card", "width": 640, "height": 640}]
    message["caption"] = "product card"
    return Update.de_json(update, user.bot)


def reply(user, body: str, to_message_id: int) -> Update:
    """A number swipe-replied to a particular bot bubble."""
    update = user.text(body).to_dict()
    update["message"]["reply_to_message"] = {
        "message_id": to_message_id,
        "date": 1_700_000_000,
        "chat": {"id": user.chat_id, "type": "private"},
        "from": {"id": 42, "is_bot": True, "first_name": "BlueStream"},
        "text": "a quantity screen",
    }
    return Update.de_json(update, user.bot)


def cart_writes(bot, product_id=WATER_ID) -> list:
    """Every write that could move the line, as (method, body)."""
    return [
        (call.method, call.data)
        for call in bot.backend.calls
        if (call.method == "PUT" and call.endpoint == f"/api/v1/cart/items/{product_id}")
        or (call.method == "POST" and call.endpoint == "/api/v1/cart/items")
        or (call.method == "DELETE" and call.endpoint == f"/api/v1/cart/items/{product_id}")
    ]


def support_posts(bot) -> list:
    return [
        call.data
        for call in bot.backend.calls
        if call.method == "POST" and call.endpoint == SUPPORT_ENDPOINT
    ]


def replies(bot) -> list[str]:
    return [call.text for call in bot.telegram.of("sendMessage")]


def deleted_message_ids(bot) -> list[int]:
    return [int(call.params["message_id"]) for call in bot.telegram.of("deleteMessage")]


def stored_state(bot) -> dict:
    return json.loads(bot.database.user.get("bot_state") or "{}")


def restart(bot, user):
    """A deploy: `user_data` and every conversation die; `users.bot_state` does not."""
    bot.application.user_data[user.user_id].clear()
    for group in bot.application.handlers.values():
        for handler in group:
            conversations = getattr(handler, "_conversations", None)
            if conversations is not None:
                conversations.clear()
    bot.telegram.reset()


# ---------------------------------------------------------------------------
# The number is written
# ---------------------------------------------------------------------------


async def test_a_typed_number_sets_the_line_to_exactly_that_number(bot, user, cart):
    """The whole feature. If the write became relative (a POST increment), the
    customer who typed 7 would get 2 + 7 = 9 bottles; if it went nowhere, their
    7 would sit in the Support Inbox while the cart held 2."""
    await open_quantity_screen(bot, user)
    assert cart.quantity_of(WATER_ID) == WATER_MIN_QTY

    await bot.send(user.text("7"))

    assert cart_writes(bot) == [("PUT", {"quantity": 7})]
    assert cart.quantity_of(WATER_ID) == 7
    assert support_posts(bot) == [], "a quantity is not a support ticket"

    screen = bot.telegram.last_shown()
    assert screen.method == "sendMessage", "the refreshed screen goes BELOW the customer's 7"
    assert f"{t('telegram.quantity')}: 7" in screen.text
    assert "Jami: 105,000 UZS" in screen.text  # 7 × 15,000, hand-computed
    # The ± row steps from the NEW quantity, or the next tap would undo the 7.
    assert f"qty_inc_{WATER_ID}_7" in screen.callback_data()
    assert f"qty_dec_{WATER_ID}_7" in screen.callback_data()
    # One live screen: the one they typed under replaces the one they typed at.
    assert deleted_message_ids(bot) == [QUANTITY_SCREEN_MESSAGE_ID]


@pytest.mark.parametrize(
    "typed",
    ["7 ta", "7 dona", "7 шт", "x7", "\u04457", "7\ufe0f\u20e3", "\uff17", "\u0667"],
)
async def test_the_ways_customers_write_seven_all_set_seven(bot, user, cart, typed):
    await open_quantity_screen(bot, user)

    await bot.send(user.text(typed))

    assert cart_writes(bot) == [("PUT", {"quantity": 7})]
    assert cart.quantity_of(WATER_ID) == 7


async def test_typing_again_replaces_the_screen_the_first_number_drew(bot, user, cart):
    """The window must follow the screen: after "7" drew a new bubble, "9" has to
    delete THAT bubble — not the long-gone first one — or the chat collects a
    live screen per number typed."""
    await open_quantity_screen(bot, user)
    await bot.send(user.text("7"))
    screen_drawn_by_the_seven = bot.telegram._next_message_id  # the last message sent
    bot.telegram.reset()

    await bot.send(user.text("9"))

    assert cart.quantity_of(WATER_ID) == 9
    assert deleted_message_ids(bot) == [screen_drawn_by_the_seven]


async def test_a_line_that_left_the_cart_is_added_back_at_the_typed_number(bot, user, cart):
    """The screen is still up but the line is gone (removed on the website, say).
    PUT on a missing line is a backend 404, so the only honest write is a POST —
    and it is safe only because the bot READ the cart first and saw nothing."""
    await open_quantity_screen(bot, user)
    cart.remove(WATER_ID)

    await bot.send(user.text("7"))

    assert cart_writes(bot) == [("POST", {"product_id": WATER_ID, "quantity": 7})]
    assert cart.quantity_of(WATER_ID) == 7


async def test_a_typed_number_still_lands_after_a_deploy(bot, user, cart):
    """The window is in `users.bot_state`, not `user_data`: a restart between the
    screen and the number must not send the 7 back to the Support Inbox."""
    await open_quantity_screen(bot, user)
    restart(bot, user)

    await bot.send(user.text("7"))

    assert cart_writes(bot) == [("PUT", {"quantity": 7})]
    assert support_posts(bot) == []


async def test_a_photo_screen_is_redrawn_as_a_photo(bot, user, cart, catalogue):
    """Most products have a picture, and the quantity screen shows it. Answering
    a typed number with a bare text screen would make the product vanish."""
    catalogue[WATER_ID]["image_url"] = "https://cdn.example.com/water.jpg"
    await bot.send(user.tap(f"add_to_cart_{WATER_ID}", message_id=QUANTITY_SCREEN_MESSAGE_ID))
    first_photo_screen = bot.telegram._next_message_id
    bot.telegram.reset()

    await bot.send(user.text("7"))

    photos = bot.telegram.of("sendPhoto")
    assert len(photos) == 1
    assert photos[0].params["photo"] == "https://cdn.example.com/water.jpg"
    assert f"{t('telegram.quantity')}: 7" in photos[0].params["caption"]
    assert deleted_message_ids(bot) == [first_photo_screen]


# ---------------------------------------------------------------------------
# Numbers that are refused
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("typed", ["1", "0"])
async def test_a_number_below_the_minimum_is_refused_with_the_minimum(bot, user, cart, typed):
    """Water is sold in twos. Writing 1 would strand the customer on a cart the
    backend refuses at checkout; writing 0 is a PUT the backend treats as
    REMOVE. Neither may reach it."""
    await open_quantity_screen(bot, user)

    await bot.send(user.text(typed))

    assert cart_writes(bot) == []
    assert cart.quantity_of(WATER_ID) == WATER_MIN_QTY
    assert replies(bot) == [t("telegram.products.typed_quantity_below_min", min_qty=2)]
    assert support_posts(bot) == []


async def test_after_a_refusal_the_customer_can_simply_type_again(bot, user, cart):
    await open_quantity_screen(bot, user)
    await bot.send(user.text("1"))

    await bot.send(user.text("3"))

    assert cart_writes(bot) == [("PUT", {"quantity": 3})]


async def test_a_number_above_the_stock_is_refused_with_what_is_left(bot, user, cart):
    await open_quantity_screen(bot, user)

    await bot.send(user.text("41"))

    assert cart_writes(bot) == []
    assert replies(bot) == [t("telegram.products.typed_quantity_above_max", max_qty=WATER_STOCK)]


async def test_the_top_of_the_range_is_accepted(bot, user, cart):
    """Off-by-one guard on the ceiling: exactly the stock is orderable."""
    await open_quantity_screen(bot, user)

    await bot.send(user.text("40"))

    assert cart_writes(bot) == [("PUT", {"quantity": 40})]


async def test_an_untracked_product_is_capped_at_the_per_item_maximum(bot, user, cart, catalogue):
    """No stock figure means the backend's per-item cap is the only bound — and
    the cart write path does not enforce it, so this refusal is the guard."""
    catalogue[WATER_ID]["inventory"]["stock_quantity"] = None
    await open_quantity_screen(bot, user)

    await bot.send(user.text(str(MAX_QUANTITY_PER_ITEM + 1)))

    assert cart_writes(bot) == []
    assert replies(bot) == [
        t("telegram.products.typed_quantity_above_max", max_qty=MAX_QUANTITY_PER_ITEM)
    ]


@pytest.mark.parametrize("typed", ["2.5", "-3", "+5"])
async def test_a_number_that_is_not_a_whole_count_gets_a_hint(bot, user, cart, typed):
    await open_quantity_screen(bot, user)

    await bot.send(user.text(typed))

    assert cart_writes(bot) == []
    assert replies(bot) == [
        t("telegram.products.typed_quantity_not_whole", min_qty=2, max_qty=WATER_STOCK)
    ]
    assert support_posts(bot) == []


async def test_a_product_that_sold_out_since_the_screen_opened_is_refused(
    bot, user, cart, catalogue
):
    await open_quantity_screen(bot, user)
    catalogue[WATER_ID]["inventory"]["stock_quantity"] = 1  # below the minimum of 2

    await bot.send(user.text("7"))
    await bot.send(user.text("7"))

    assert cart_writes(bot) == []
    assert replies(bot) == [t("telegram.products.out_of_stock")] * 2, (
        "every number typed at a sold-out screen is answered, never filed as support"
    )


async def test_a_backend_refusal_is_shown_and_the_cart_is_left_alone(bot, user, cart):
    """The bot's ceiling reads raw stock; the backend also subtracts other
    customers' reservations. Its refusal must reach the customer."""
    await open_quantity_screen(bot, user)
    refusal = f"Product {WATER_ID} ({WATER_NAME}): Only 5 available (reserved: 35), requested 7"
    bot.backend.route(
        "PUT",
        f"/api/v1/cart/items/{WATER_ID}",
        lambda _c: backend_failure(refusal, status_code=400),
    )

    await bot.send(user.text("7"))

    assert cart.quantity_of(WATER_ID) == WATER_MIN_QTY
    assert replies(bot) == [f"❌ {refusal}"]


async def test_an_unreadable_cart_writes_nothing(bot, user, cart):
    """The typed write is a PUT when the line exists and a POST (an INCREMENT)
    when it does not, so which one is right depends on reading the cart. An
    unreadable cart is UNKNOWN, never empty: treating a 500 as "not in the
    cart" would POST 7 onto the 2 already there — the 2026-06-27 accumulation
    bug, 9 bottles for a customer who typed 7."""
    await open_quantity_screen(bot, user)
    bot.backend.route(
        "GET", "/api/v1/cart", lambda _c: backend_failure("Cart unavailable", status_code=500),
    )

    await bot.send(user.text("7"))

    assert cart_writes(bot) == []
    assert cart.quantity_of(WATER_ID) == WATER_MIN_QTY
    assert replies(bot) == ["❌ Cart unavailable"]


# ---------------------------------------------------------------------------
# When a number is NOT a quantity
# ---------------------------------------------------------------------------


async def test_words_typed_on_the_screen_reach_the_inbox_and_end_the_typing_window(
    bot, user, cart
):
    """Once the customer is talking to a person, a bare number that follows is
    most likely an answer to that person ("which apartment?" — "12"), and an
    operator reply reaches the chat without any update the bot could see. So
    words close the window: the 12 goes to the inbox, not into the cart."""
    question = "Salom, suv bugun keladimi?"
    await open_quantity_screen(bot, user)

    await bot.send(user.text(question))
    await bot.send(user.text("12"))

    assert support_posts(bot) == [
        {"content": question, "message_type": "text"},
        {"content": "12", "message_type": "text"},
    ]
    assert bot.telegram.shown == [], "the inbox capture stays silent"
    assert cart_writes(bot) == []


async def test_a_phone_number_typed_on_the_screen_reaches_the_inbox(bot, user, cart):
    """Nine or twelve digits is a phone number, not a count; it is for the
    operator who asked for it."""
    await open_quantity_screen(bot, user)

    await bot.send(user.text("+998901234567"))

    assert support_posts(bot) == [{"content": "+998901234567", "message_type": "text"}]
    assert cart_writes(bot) == []


@pytest.mark.parametrize(
    "leave",
    [
        lambda user: user.tap(f"back_to_product_{WATER_ID}"),
        lambda user: user.tap("back_to_main"),
        lambda user: user.tap("cart_view"),
        lambda user: user.command("menu"),
        lambda user: user.photo(),
        # The two ways off the screen that sit ON the live screen itself.
        lambda user: user.tap("checkout", message_id=QUANTITY_SCREEN_MESSAGE_ID),
        lambda user: user.tap(f"back_to_product_{WATER_ID}", message_id=QUANTITY_SCREEN_MESSAGE_ID),
    ],
    ids=["back", "main-menu", "cart", "/menu", "photo", "checkout-on-screen", "back-on-screen"],
)
async def test_once_the_customer_leaves_the_screen_a_number_is_a_message_again(
    bot, user, cart, leave
):
    """A digit typed later, somewhere else, must never move a cart line the
    customer is no longer looking at."""
    await open_quantity_screen(bot, user)
    await bot.send(leave(user))
    bot.telegram.reset()
    bot.backend.calls.clear()  # a photo is itself filed to support; not the point

    await bot.send(user.text("7"))

    assert cart_writes(bot) == []
    assert support_posts(bot) == [{"content": "7", "message_type": "text"}]


async def test_the_screen_stops_listening_after_thirty_minutes(bot, user, cart):
    """Aged by editing the stored stamp, the same column production reads."""
    await open_quantity_screen(bot, user)
    state = stored_state(bot)
    state["quantity_screen"]["shown_at"] = (
        datetime.now(timezone.utc) - timedelta(minutes=31)
    ).isoformat()
    bot.database.user["bot_state"] = json.dumps(state)

    await bot.send(user.text("7"))

    assert cart_writes(bot) == []
    assert support_posts(bot) == [{"content": "7", "message_type": "text"}]


# ---------------------------------------------------------------------------
# Older quantity bubbles still in the chat
#
# A command (or a reply keyboard) leaves the previous quantity screen in the
# chat with working buttons. A tap on it must never leave the typing window
# aimed at a DIFFERENT product: only a tap on the live screen keeps it, and a
# tap that redraws another screen moves it there.
# ---------------------------------------------------------------------------

# Older than Water's 321: Telegram message ids only grow within a chat, and the
# reply rule relies on it.
SPORT_SCREEN_MESSAGE_ID = 300


async def two_quantity_screens(bot, user):
    """Sport's screen (300) left behind by /menu, then Water's screen (321) live."""
    await open_quantity_screen(bot, user, product_id=SPORT_ID, message_id=SPORT_SCREEN_MESSAGE_ID)
    await bot.send(user.command("menu"))
    await open_quantity_screen(bot, user, product_id=WATER_ID)


async def test_tapping_the_quantity_label_on_an_older_screen_ends_the_typing_window(
    bot, user, cart
):
    """The 'Miqdor: 1' label writes and redraws nothing. Typing after it means
    Sport — so it must not land on Water, the screen the window still named."""
    await two_quantity_screens(bot, user)

    await bot.send(user.tap("qty_current", message_id=SPORT_SCREEN_MESSAGE_ID))
    await bot.send(user.text("4"))

    assert cart_writes(bot, WATER_ID) == []
    assert cart_writes(bot, SPORT_ID) == []
    assert support_posts(bot) == [{"content": "4", "message_type": "text"}]


async def test_a_failed_add_to_cart_on_another_card_ends_the_typing_window(bot, user, cart):
    await open_quantity_screen(bot, user)
    bot.backend.route(
        "POST", "/api/v1/cart/items", lambda _c: backend_failure("Server error", status_code=500),
    )

    await bot.send(user.tap(f"add_to_cart_{SPORT_ID}", message_id=500))
    await bot.send(user.text("4"))

    assert api_calls(bot, "PUT", f"/api/v1/cart/items/{WATER_ID}") == []
    assert cart.quantity_of(WATER_ID) == WATER_MIN_QTY
    assert support_posts(bot) == [{"content": "4", "message_type": "text"}]


async def test_a_redrawn_older_screen_takes_the_typed_number(bot, user, cart):
    await two_quantity_screens(bot, user)

    await bot.send(user.tap(f"qty_inc_{SPORT_ID}_1", message_id=SPORT_SCREEN_MESSAGE_ID))
    await bot.send(user.text("4"))

    assert api_calls(bot, "PUT", f"/api/v1/cart/items/{WATER_ID}") == []
    assert [c.data for c in api_calls(bot, "PUT", f"/api/v1/cart/items/{SPORT_ID}")] == [
        {"quantity": 2}, {"quantity": 4},
    ]
    assert cart.quantity_of(SPORT_ID) == 4
    assert deleted_message_ids(bot) == [SPORT_SCREEN_MESSAGE_ID]


async def test_a_number_replied_to_an_older_screen_is_not_written_to_the_live_one(
    bot, user, cart
):
    """Swipe-replying "4" to Sport's old screen means Sport. The window only
    knows Water's, so the number must not land on Water."""
    await two_quantity_screens(bot, user)

    await bot.send(reply(user, "4", to_message_id=SPORT_SCREEN_MESSAGE_ID))

    assert cart_writes(bot, WATER_ID) == []
    assert support_posts(bot) == [{"content": "4", "message_type": "text"}]


async def test_a_number_replied_to_the_live_screen_is_its_quantity(bot, user, cart):
    await open_quantity_screen(bot, user)

    await bot.send(reply(user, "7", to_message_id=QUANTITY_SCREEN_MESSAGE_ID))

    assert cart_writes(bot) == [("PUT", {"quantity": 7})]


async def test_a_forwarded_number_is_not_a_quantity(bot, user, cart):
    """Somebody else's "5" is not the customer choosing five."""
    await open_quantity_screen(bot, user)

    await bot.send(user.forwarded_text("5"))

    assert cart_writes(bot) == []


async def test_tapping_the_live_screens_quantity_label_keeps_the_window(bot, user, cart):
    """Tapping the number before typing one is the natural gesture; it must not
    cost the customer the very typing it invites."""
    await open_quantity_screen(bot, user)

    await bot.send(user.tap("qty_current", message_id=QUANTITY_SCREEN_MESSAGE_ID))
    await bot.send(user.text("7"))

    assert cart_writes(bot) == [("PUT", {"quantity": 7})]
    assert support_posts(bot) == []


async def test_a_tap_on_a_photo_screen_keeps_the_window_on_that_bubble(bot, user, cart, catalogue):
    """The usual production path: the product card is a photo, 'Add to cart'
    edits its caption in place, and a later ± edits it again. The window must
    name that same bubble, so the typed number replaces it."""
    catalogue[WATER_ID]["image_url"] = "https://cdn.example.com/water.jpg"
    await bot.send(photo_tap(user, f"add_to_cart_{WATER_ID}", QUANTITY_SCREEN_MESSAGE_ID))
    await bot.send(photo_tap(user, f"qty_inc_{WATER_ID}_2", QUANTITY_SCREEN_MESSAGE_ID))
    assert [int(c.params["message_id"]) for c in bot.telegram.of("editMessageCaption")] == [
        QUANTITY_SCREEN_MESSAGE_ID, QUANTITY_SCREEN_MESSAGE_ID,
    ]
    bot.telegram.reset()

    await bot.send(user.text("7"))

    assert cart.quantity_of(WATER_ID) == 7
    assert deleted_message_ids(bot) == [QUANTITY_SCREEN_MESSAGE_ID]
    assert f"{t('telegram.quantity')}: 7" in bot.telegram.of("sendPhoto")[0].params["caption"]


# ---------------------------------------------------------------------------
# Living beside the other prompts
# ---------------------------------------------------------------------------


async def test_the_newest_screen_takes_the_number_and_an_older_issue_report_the_words(
    bot, user, cart
):
    """"Report an issue" armed first, THEN the quantity screen: the number is
    for the screen the customer is looking at, and the report is still armed
    for the words that follow (opening the screen must not disarm it)."""
    await bot.send(user.tap(f"report_issue_{ORDER_ID}"))
    await open_quantity_screen(bot, user)

    await bot.send(user.text("7"))
    await bot.send(user.text("Suv loyqa keldi"))

    assert cart_writes(bot) == [("PUT", {"quantity": 7})]
    assert support_posts(bot) == [
        {"content": f"[Order #{ORDER_NUMBER}] Suv loyqa keldi", "message_type": "text"}
    ]


async def test_an_issue_report_opened_after_the_screen_takes_the_number(bot, user, cart):
    await open_quantity_screen(bot, user)
    await bot.send(user.tap(f"report_issue_{ORDER_ID}"))

    await bot.send(user.text("7"))

    assert cart_writes(bot) == []
    assert support_posts(bot) == [
        {"content": f"[Order #{ORDER_NUMBER}] 7", "message_type": "text"}
    ]


async def test_a_verification_code_is_never_read_as_a_quantity(bot, user, cart):
    """An SMS code is six digits, and six digits never read as a quantity, so
    the code still reaches verification. Anything a count can be, typed at the
    quantity screen, is still a quantity — a pending verification must not
    answer every number with "invalid code format"."""
    await bot.send(user.tap("verify_phone_number"))
    await open_quantity_screen(bot, user)

    await bot.send(user.text("7"))
    await bot.send(user.text("123456"))

    assert cart_writes(bot) == [("PUT", {"quantity": 7})]
    verifications = api_calls(bot, "POST", VERIFY_OTP)
    assert len(verifications) == 1
    assert "123456" in json.dumps(verifications[0].data)


# ---------------------------------------------------------------------------
# The screen says so
# ---------------------------------------------------------------------------


async def test_the_quantity_screen_invites_typing(bot, user):
    screen = await open_quantity_screen(bot, user)

    assert t("telegram.products.type_quantity_hint") in screen.text


async def test_a_sold_out_screen_does_not_invite_typing(bot, user, catalogue):
    catalogue[WATER_ID]["inventory"]["stock_quantity"] = 1  # below the minimum of 2

    screen = await open_quantity_screen(bot, user)

    assert t("telegram.products.type_quantity_hint") not in screen.text
