"""The customer's cart, driven end to end through the REAL PTB dispatcher.

WHY THIS FILE EXISTS
--------------------
``test_cart_edit_mode.py`` calls handler coroutines directly with a hand-rolled
``DummyUpdate``. That proves each handler behaves when it is CALLED. It cannot
prove any of the things the cart actually breaks on:

* whether the ``callback_data`` a keyboard rendered is registered at all,
* what the SECOND tap of the same button does (the dedup middleware and
  Checkout reading the cart before it writes are two different defences, and
  only the second one is still standing once the debounce window has passed),
* what the customer is left looking at when a cart write fails,
* whether the money on the screen is the money the server will charge.

THE QUANTITY SCREEN IS A DRAFT (owner 2026-10-22)
-------------------------------------------------
There is no product card and no "Add to cart" step any more. A product tap
opens its quantity screen at the quantity the cart already holds (else the
product minimum) and writes NOTHING; presets and ± only redraw the screen. The
one cart write is ✅ Checkout (``buy_<id>_<qty>``): it reads the cart, PUTs the
line if the product is already there, POSTs it otherwise, and hands over to
checkout. So every "what reached the cart" assertion below is made at Checkout.

Every update below goes in through ``Application.process_update``. The only
fakes are the three harness seams: Telegram's transport, ``_make_request``, and
the bot's own SQL. ``ServerCart`` below is the backend's cart, and it prices
itself — the bot is never allowed to price it.

READING THE ASSERTIONS
----------------------
Assertions are on what reached the BACKEND (exact method, endpoint and body) or
on what the customer SAW (exact rendered text, exact ``callback_data``). "A mock
was called" is never enough here: the 2026-06-27 accumulation bug was a mock
being called one time too many with a payload nobody looked at.
"""

import time

import pytest
from telegram import Update
from telegram.ext import TypeHandler

from handlers import callback_dedup
from handlers.callback_dedup import callback_dedup_middleware
from handlers.products import min_order_shortfall
from keyboards import ProductKeyboards
from utils import format_price

from shared.business_config import MIN_ORDER_AMOUNT
from tests.telegram_bot.ptb_harness import backend_failure, build_bot_harness, menu_already_installed

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


# ---------------------------------------------------------------------------
# The catalogue
#
# Prices are derived from MIN_ORDER_AMOUNT rather than hard-coded so the
# scenarios keep their MEANING if the business floor ever moves: one bottle of
# water cannot clear the floor on its own, one crate of sport water clears it
# comfortably.
# ---------------------------------------------------------------------------

WATER_ID = 5
SPORT_ID = 6

WATER_UNIT_PRICE = float(MIN_ORDER_AMOUNT) * 0.75  # 15 000 — one is not enough
SPORT_UNIT_PRICE = float(MIN_ORDER_AMOUNT) * 1.50  # 30 000

WATER_NAME = "Aqua Element 18.9 l"
SPORT_NAME = "Aqua Sport 0.5 l"

WATER_MIN_QTY = 2  # deliberately > 1: proves "the product's minimum", not "one"

WATER_CATEGORY = 1  # the quantity screen's Back goes to `category_<id>`


def build_catalogue() -> dict:
    """Fresh per test — several tests mutate stock mid-journey."""
    return {
        WATER_ID: {
            "id": WATER_ID,
            "name": WATER_NAME,
            "current_price": WATER_UNIT_PRICE,
            "description": "Tabiiy ichimlik suvi",
            "category": {"id": WATER_CATEGORY, "name": "Suv"},
            "inventory": {"min_order_quantity": WATER_MIN_QTY, "stock_quantity": 40},
            "specifications": {"volume": 18.9, "volume_unit": "l"},
        },
        SPORT_ID: {
            "id": SPORT_ID,
            "name": SPORT_NAME,
            "current_price": SPORT_UNIT_PRICE,
            "description": "Sport suvi",
            "category": {"id": WATER_CATEGORY, "name": "Suv"},
            "inventory": {"min_order_quantity": 1, "stock_quantity": 12},
            "specifications": {"volume": 0.5, "volume_unit": "l"},
        },
    }


# ---------------------------------------------------------------------------
# The backend's cart
# ---------------------------------------------------------------------------


class ServerCart:
    """The cart as ``CartService`` owns it — including the pricing.

    Two knobs exist because both correspond to real backend behaviour the bot
    has already been burned by:

    ``server_unit_price``
        The figure ``get_cart_summary`` composes ``total_price`` / ``subtotal``
        from. It is deliberately SEPARATE from the product's ``current_price``,
        which ``CartItem.to_dict()`` bakes through ``Product.calculate_price``
        — a function that ignores its ``user`` argument and is therefore blind
        to contract pricing. When the two differ, only one of them may reach
        the screen.

    ``dropped``
        Products ``get_cart_summary`` SKIPS (inactive products). Their line
        survives in ``cart_items`` with no ``total_price`` and contributes
        nothing to ``subtotal`` — exactly what the order will contain.
    """

    def __init__(self, catalogue: dict):
        self.catalogue = catalogue
        self.lines: dict[int, int] = {}
        self.server_unit_price = {
            pid: float(product["current_price"]) for pid, product in catalogue.items()
        }
        self.dropped: set[int] = set()

    # -- the write endpoints, with the backend's own semantics ---------------

    def add(self, product_id: int, quantity: int):
        """POST /cart/items is an INCREMENT on the backend, not a set."""
        self.lines[product_id] = self.lines.get(product_id, 0) + int(quantity)
        return self.payload()

    def set(self, product_id: int, quantity: int):
        self.lines[product_id] = int(quantity)
        return self.payload()

    def remove(self, product_id: int):
        self.lines.pop(product_id, None)
        return self.payload()

    def clear(self):
        self.lines.clear()
        return self.payload()

    # -- reads ---------------------------------------------------------------

    def quantity_of(self, product_id: int):
        return self.lines.get(product_id)

    def subtotal(self) -> float:
        return sum(
            self.server_unit_price[pid] * qty
            for pid, qty in self.lines.items()
            if pid not in self.dropped
        )

    def payload(self) -> dict:
        items = []
        for pid, qty in self.lines.items():
            item = {
                "product_id": pid,
                "quantity": qty,
                "product": self.catalogue[pid],
            }
            if pid not in self.dropped:
                item["total_price"] = self.server_unit_price[pid] * qty
            items.append(item)
        return {"data": {"cart": {"cart_items": items, "subtotal": self.subtotal()}}}


def install_cart_backend(backend, cart: ServerCart) -> None:
    """Wire the real api_client endpoint paths onto ``cart``.

    The endpoints below are the ones ``telegram_bot/api_client.py`` builds; a
    typo here shows up as a route miss (the harness default ``{"data": {}}``),
    not as a silently passing test.
    """
    backend.route("GET", "/api/v1/cart", lambda call: cart.payload())
    backend.route(
        "POST",
        "/api/v1/cart/items",
        lambda call: cart.add(call.data["product_id"], call.data["quantity"]),
    )
    backend.route("POST", "/api/v1/cart/clear", lambda call: cart.clear())

    for pid in cart.catalogue:
        backend.route(
            "GET",
            f"/api/v1/products/{pid}",
            lambda call, pid=pid: {"data": {"product": cart.catalogue[pid]}},
        )
        backend.route(
            "PUT",
            f"/api/v1/cart/items/{pid}",
            lambda call, pid=pid: cart.set(pid, call.data["quantity"]),
        )
        backend.route(
            "DELETE",
            f"/api/v1/cart/items/{pid}",
            lambda call, pid=pid: cart.remove(pid),
        )


# ---------------------------------------------------------------------------
# Translations
#
# Real, distinct copy per language. Unseeded keys render as
# `humanised_missing_key`, so a test asserting on a string it forgot to seed
# would be asserting on "Cart total" — an accident that reads like a pass.
# ---------------------------------------------------------------------------

TRANSLATIONS = {
    ("uz", "telegram.cart_title"): "Savatingiz",
    ("ru", "telegram.cart_title"): "Ваша корзина",
    ("uz", "telegram.cart_empty"): "Savat bo'sh",
    ("ru", "telegram.cart_empty"): "Корзина пуста",
    ("uz", "telegram.cart_total"): "Savat jami",
    ("ru", "telegram.cart_total"): "Итого по корзине",
    ("uz", "telegram.cart_ready_checkout"): "Buyurtmaga tayyor",
    ("ru", "telegram.cart_ready_checkout"): "Готово к заказу",
    ("uz", "telegram.cart_min_order_warning"): (
        "Eng kam buyurtma {min_amount} UZS. Yana {remaining} UZS qo'shing"
    ),
    ("ru", "telegram.cart_min_order_warning"): (
        "Минимальный заказ {min_amount} UZS. Добавьте ещё {remaining} UZS"
    ),
    ("uz", "telegram.cart_min_qty_warning"): (
        "{product_name}: eng kami {min_qty} dona, yana {remaining} dona"
    ),
    ("uz", "telegram.cart.checkout"): "Buyurtma berish",
    ("ru", "telegram.cart.checkout"): "Оформить заказ",
    ("uz", "telegram.cart.clear"): "Savatni tozalash",
    ("ru", "telegram.cart.clear"): "Очистить корзину",
    ("uz", "telegram.cart.continue_shopping"): "Xaridni davom ettirish",
    ("ru", "telegram.cart.continue_shopping"): "Продолжить покупки",
    ("uz", "telegram.cart.add_more"): "Yana mahsulot qo'shing",
    ("uz", "telegram.cart.remove"): "O'chirish",
    ("ru", "telegram.cart.remove"): "Удалить",
    ("uz", "telegram.cart.add_product"): "Mahsulot qo'shish",
    ("uz", "telegram.cart.done"): "Tayyor",
    ("uz", "telegram.back"): "Orqaga",
    ("ru", "telegram.back"): "Назад",
    ("uz", "telegram.quantity"): "Miqdor",
    ("ru", "telegram.quantity"): "Количество",
    ("uz", "telegram.total"): "Jami",
    ("ru", "telegram.total"): "Итого",
    ("uz", "telegram.products.min_order_quantity_label"): "Eng kam miqdor: {min_qty}",
    ("uz", "telegram.products.unit_price_label"): "Narxi: {price} UZS (1 dona)",
    ("uz", "telegram.products.type_quantity_hint"): "✏️ Yoki kerakli miqdorni yozing",
    ("uz", "telegram.products.cart_cleared"): "Savat tozalandi",
    ("uz", "telegram.products.out_of_stock"): "Tugagan",
    ("uz", "telegram.products.invalid_action"): "Noto'g'ri amal",
}


def t(key: str, language: str = "uz", **fmt) -> str:
    """The seeded string, formatted the way the handler formats it."""
    return TRANSLATIONS[(language, key)].format(**fmt)


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
    await menu_already_installed(harness)
    return harness


@pytest.fixture
def user(bot):
    return bot.updates()


# ---------------------------------------------------------------------------
# Small readers over what reached the backend / what the customer saw
# ---------------------------------------------------------------------------


def api_calls(bot, method: str, endpoint: str) -> list:
    return [
        call
        for call in bot.backend.calls
        if call.method == method and call.endpoint == endpoint
    ]


def cart_writes(bot) -> list[tuple]:
    """Every write that reached a cart line, as ``(method, endpoint, body)``, in order."""
    return [
        (call.method, call.endpoint, call.data)
        for call in bot.backend.calls
        if call.method in {"POST", "PUT", "DELETE"}
        and call.endpoint.startswith("/api/v1/cart/items")
    ]


def checkout_reads(bot) -> list:
    """The address read that opens checkout — ``checkout_handler``'s first backend call."""
    return api_calls(bot, "GET", "/api/v1/auth/addresses")


def toasts(bot) -> list[str]:
    """Every ``answerCallbackQuery`` that carried visible text."""
    return [
        call.params["text"]
        for call in bot.telegram.of("answerCallbackQuery")
        if "text" in call.params
    ]


def expire_dedup_window() -> None:
    """Age every in-memory dedup lock past its TTL.

    The window is 2 real seconds; sleeping for it would put a two-second stall
    in the suite for every debounce assertion. Ageing the module's own lock
    table is the same thing the clock would do, without the wall time.
    """
    stale = time.monotonic() - 1
    for key in list(callback_dedup._in_memory_locks):
        callback_dedup._in_memory_locks[key] = stale


async def deliberate_retap(bot, update):
    """Send a tap the customer made DELIBERATELY, seconds after the previous one.

    The dedup guard debounces (user_id, callback_data) for 2 real seconds, and
    since 2026-08-21 the harness carries that guard because production
    registers it in ``_setup_handlers()``. So firing the same button twice in
    the same microsecond models an IMPATIENT DOUBLE-TAP, not a customer
    stepping the quantity down three times — the guard correctly eats the
    repeats. Ageing the lock table first is what the wall clock would do,
    without putting two real seconds into the suite.

    The tests ABOUT the debounce deliberately do NOT use this.
    """
    expire_dedup_window()
    return await bot.send(update)


async def open_quantity_screen(bot, user, product_id=WATER_ID, message_id=None):
    """Tap the product in a product list: its quantity screen opens, nothing is written."""
    await bot.send(user.tap(f"product_{product_id}", message_id=message_id))
    return bot.telegram.last_shown()


async def open_cart_in_edit_mode(bot, user):
    """The customer taps 'Edit' on the order-confirmation card.

    That is the only entry into edit mode in production, and it is what sets
    ``cart_edit_return`` — the flag every later cart tap reads.
    """
    await bot.send(user.tap("edit_order"))
    return bot.telegram.last_shown()


# ---------------------------------------------------------------------------
# Adding the same product twice
# ---------------------------------------------------------------------------


async def test_checking_out_the_same_product_twice_never_stacks_two_lots(
    bot, user, cart
):
    """A hesitant customer checks a product out, goes back to the shop, opens
    the same product again and checks out again.

    ``POST /cart/items`` is an INCREMENT on the backend, so if the write ever
    stopped reading the cart first, the line would silently grow by another
    lot per Checkout and the customer would be charged for bottles they never
    asked for. That is prod triage 2026-06-27 (user 267) verbatim — then it was
    the 'Add to cart' tap, now Checkout is the screen's only write, so the
    guard lives there. Driven through the dispatcher so it is exercised on the
    real update path, not on a hand-built one.
    """
    first = await open_quantity_screen(bot, user)
    checkout = f"buy_{WATER_ID}_{WATER_MIN_QTY}"
    assert checkout in first.callback_data()
    assert cart_writes(bot) == [], "opening the screen must write nothing"

    await bot.send(user.tap(checkout))

    # Back in the shop, the same product again: the screen opens at what the
    # cart now holds, and opening it still writes nothing.
    await deliberate_retap(bot, user.tap(f"product_{WATER_ID}"))
    reopened = bot.telegram.last_shown()
    assert f"{t('telegram.quantity')}: {WATER_MIN_QTY}\n" in reopened.text
    assert checkout in reopened.callback_data()
    assert len(cart_writes(bot)) == 1

    await deliberate_retap(bot, user.tap(checkout))

    assert cart_writes(bot) == [
        ("POST", "/api/v1/cart/items", {"product_id": WATER_ID, "quantity": WATER_MIN_QTY}),
        ("PUT", f"/api/v1/cart/items/{WATER_ID}", {"quantity": WATER_MIN_QTY}),
    ], "the second Checkout must SET the line, not post a second increment"
    assert cart.quantity_of(WATER_ID) == WATER_MIN_QTY
    assert len(checkout_reads(bot)) == 2, "both Checkouts must go on to checkout"


async def test_a_double_tap_inside_the_dedup_window_never_reaches_the_cart(
    bot, user, cart
):
    """Telegram keeps the button spinner up for the whole handler, so people
    tap again. With ``PerChatSerialUpdateProcessor`` the second tap queues and
    runs after the first one has finished — on Checkout, that is a second cart
    write and a second checkout screen for one intended tap.

    The middleware must stop the duplicate before ANY handler runs, and must
    still answer it so the spinner clears. It must also be a debounce, not a
    ban: once the window passes, a deliberate re-tap has to work again.
    """
    await open_quantity_screen(bot, user)
    checkout = f"buy_{WATER_ID}_{WATER_MIN_QTY}"

    await bot.send(user.tap(checkout))
    assert cart_writes(bot) == [
        ("POST", "/api/v1/cart/items", {"product_id": WATER_ID, "quantity": WATER_MIN_QTY}),
    ], "first tap must go through"

    calls_after_first_tap = len(bot.backend.calls)
    bot.telegram.reset()

    await bot.send(user.tap(checkout))

    assert len(bot.backend.calls) == calls_after_first_tap, (
        "the duplicate tap reached the backend — no handler may see it at all"
    )
    assert [call.method for call in bot.telegram.calls] == ["answerCallbackQuery"], (
        "a dropped duplicate must be acknowledged and nothing else"
    )
    assert "text" not in bot.telegram.calls[0].params, (
        "the drop is silent to the customer; a toast would look like an error"
    )

    # The window passes; the customer genuinely taps Checkout again.
    expire_dedup_window()
    bot.telegram.reset()
    await bot.send(user.tap(checkout))

    assert bot.telegram.shown, "a deliberate re-tap after the window must reach checkout"
    assert cart_writes(bot)[1:] == [
        ("PUT", f"/api/v1/cart/items/{WATER_ID}", {"quantity": WATER_MIN_QTY}),
    ]
    assert cart.quantity_of(WATER_ID) == WATER_MIN_QTY, (
        "and it still must not stack — dedup is the second line of defence, "
        "not the only one"
    )


# ---------------------------------------------------------------------------
# The quantity screen (a draft until Checkout)
# ---------------------------------------------------------------------------


async def test_a_preset_redraws_the_screen_at_its_number_and_checkout_writes_exactly_that(
    bot, user, cart
):
    """The presets are labelled with absolute quantities ('8'), so tapping '8'
    must put the screen on exactly 8 — and Checkout from there must leave
    exactly 8 in the cart. If the preset ever became relative, or the Checkout
    button kept an older number, the label and the cart would disagree and
    the customer would only find out on the confirmation screen.
    """
    screen = await open_quantity_screen(bot, user)

    target = WATER_MIN_QTY + ProductKeyboards.QUANTITY_PRESET_OFFSETS[1]
    preset = f"qty_set_{WATER_ID}_{target}"
    assert preset in screen.callback_data(), (
        "the preset this test taps must be a button the customer can actually see"
    )

    bot.telegram.reset()
    await bot.send(user.tap(preset))

    assert cart_writes(bot) == [], "a preset moves the screen, never the cart"
    assert cart.lines == {}

    shown = bot.telegram.last_shown()
    # NOTE: the total line is the screen's own `unit × quantity` preview from
    # the product's catalogue price. Nothing is posted from it — Checkout
    # sends a QUANTITY — and a contract customer is told their price applies
    # at checkout (`contract_price_at_checkout`), which this product is not.
    assert shown.text == "\n".join([
        f"🛒 {WATER_NAME}",
        f"💰 {t('telegram.products.unit_price_label', price=format_price(WATER_UNIT_PRICE))}",
        f"ℹ️ {t('telegram.products.min_order_quantity_label', min_qty=WATER_MIN_QTY)}",
        "",
        f"{t('telegram.quantity')}: {target}",
        f"{t('telegram.total')}: {target} × {format_price(WATER_UNIT_PRICE)}"
        f" = {format_price(WATER_UNIT_PRICE * target)} UZS",
        "",
        t("telegram.products.type_quantity_hint"),
    ])
    # The redrawn fine-tune row AND the Checkout button must carry the NEW
    # quantity, or the next +1 would step from a stale number and Checkout
    # would write one.
    assert shown.callback_data() == [
        *(
            f"qty_set_{WATER_ID}_{WATER_MIN_QTY + offset}"
            for offset in ProductKeyboards.QUANTITY_PRESET_OFFSETS
        ),
        f"qty_dec_{WATER_ID}_{target}",
        "qty_current",
        f"qty_inc_{WATER_ID}_{target}",
        f"buy_{WATER_ID}_{target}",
        f"category_{WATER_CATEGORY}",
    ]
    labels = dict(zip(shown.callback_data(), shown.button_labels()))
    assert labels[f"buy_{WATER_ID}_{target}"] == t("telegram.cart.checkout")

    await bot.send(user.tap(f"buy_{WATER_ID}_{target}"))

    assert cart_writes(bot) == [
        ("POST", "/api/v1/cart/items", {"product_id": WATER_ID, "quantity": target}),
    ]
    assert cart.quantity_of(WATER_ID) == target


async def test_the_minus_button_never_takes_the_screen_below_the_product_minimum(
    bot, user, cart
):
    """Water is sold in twos. Holding '−1' down must clamp at 2 — the screen
    must never offer 1 (which the backend rejects, stranding the customer on a
    cart it refuses to check out), and Checkout from the clamped screen must
    write 2. '−1' is never a way to remove anything: it writes nothing at all.
    """
    await open_quantity_screen(bot, user)

    minus = f"qty_dec_{WATER_ID}_{WATER_MIN_QTY}"
    for _ in range(3):
        screen = bot.telegram.last_shown()
        assert minus in screen.callback_data(), "'−1' stepped below the floor"
        await deliberate_retap(bot, user.tap(minus))

    clamped = bot.telegram.last_shown()
    assert f"{t('telegram.quantity')}: {WATER_MIN_QTY}\n" in clamped.text
    assert f"buy_{WATER_ID}_{WATER_MIN_QTY}" in clamped.callback_data()
    assert cart_writes(bot) == [], "'−1' must never write"

    await bot.send(user.tap(f"buy_{WATER_ID}_{WATER_MIN_QTY}"))

    assert cart_writes(bot) == [
        ("POST", "/api/v1/cart/items", {"product_id": WATER_ID, "quantity": WATER_MIN_QTY}),
    ]
    assert cart.quantity_of(WATER_ID) == WATER_MIN_QTY


async def test_a_stale_bubbles_plus_steps_from_its_own_number_and_takes_nothing_from_the_cart(
    bot, user, cart
):
    """Each quantity bubble in the chat is its own draft.

    This test used to pin "a stale '+1' steps from what the CART holds": when
    ± wrote the cart, a '+1' scrolled back to from an older bubble overwrote a
    customer's 8 with 3 and they only found out at the confirmation screen.
    Now ± writes nothing, so the old bubble's '+1' steps from the number THAT
    bubble shows, visibly, on that bubble — and the cart only ever receives
    the number on the bubble whose Checkout the customer tapped.
    """
    old_bubble, new_bubble = 301, 302

    first_render = await open_quantity_screen(bot, user, message_id=old_bubble)
    stale_plus = f"qty_inc_{WATER_ID}_{WATER_MIN_QTY}"
    assert stale_plus in first_render.callback_data()

    # Further down the chat the customer opens the product again and picks 8.
    await deliberate_retap(bot, user.tap(f"product_{WATER_ID}", message_id=new_bubble))
    target = WATER_MIN_QTY + ProductKeyboards.QUANTITY_PRESET_OFFSETS[1]
    await bot.send(user.tap(f"qty_set_{WATER_ID}_{target}", message_id=new_bubble))
    assert f"buy_{WATER_ID}_{target}" in bot.telegram.last_shown().callback_data()

    # Then scrolls up and taps the OLD bubble's '+1'.
    bot.telegram.reset()
    await bot.send(user.tap(stale_plus, message_id=old_bubble))

    redrawn = bot.telegram.last_shown()
    assert int(redrawn.params["message_id"]) == old_bubble, (
        "the stale tap must redraw its own bubble, not the one showing 8"
    )
    assert f"{t('telegram.quantity')}: {WATER_MIN_QTY + 1}\n" in redrawn.text
    assert f"buy_{WATER_ID}_{WATER_MIN_QTY + 1}" in redrawn.callback_data()
    assert cart_writes(bot) == [] and cart.lines == {}, (
        "a ± tap is a draft: the stale tap must not reach the cart"
    )

    # Checkout on the bubble showing 8 writes 8: the stale tap took nothing from it.
    await bot.send(user.tap(f"buy_{WATER_ID}_{target}", message_id=new_bubble))

    assert cart_writes(bot) == [
        ("POST", "/api/v1/cart/items", {"product_id": WATER_ID, "quantity": target}),
    ]
    assert cart.quantity_of(WATER_ID) == target


async def test_tapping_the_quantity_display_cell_writes_nothing(bot, user, cart):
    """The centre cell of the fine-tune row is a label, not an action, but it
    still routes through the broad ``^qty_`` pattern. If it ever stopped being
    a no-op, every customer who taps the number would move their own order.
    """
    await open_quantity_screen(bot, user)
    calls_before = len(bot.backend.calls)
    bot.telegram.reset()

    await bot.send(user.tap("qty_current"))

    assert len(bot.backend.calls) == calls_before
    assert cart.lines == {}
    assert [call.method for call in bot.telegram.calls] == ["answerCallbackQuery"], (
        "the spinner must be dismissed, and nothing else may happen"
    )


# ---------------------------------------------------------------------------
# The shelf moving under the customer
# ---------------------------------------------------------------------------


async def test_a_product_that_sells_out_before_checkout_shows_the_servers_refusal(
    bot, user, cart, catalogue
):
    """The quantity screen was drawn while stock was 40. By the time the
    customer taps ✅ Checkout the last bottle is gone.

    Checkout has NO client-side stock gate — ``checkout_product`` reads the
    cart, never the product — so the server's rejection is the only thing
    standing between the customer and a line that cannot be delivered. It must
    reach them as text, nothing may be written, and checkout must not open.
    """
    await open_quantity_screen(bot, user)
    catalogue[WATER_ID]["inventory"]["stock_quantity"] = 0
    bot.backend.route(
        "POST",
        "/api/v1/cart/items",
        lambda call: backend_failure("Mahsulot sotuvda yo'q", status_code=409),
    )
    bot.telegram.reset()

    await bot.send(user.tap(f"buy_{WATER_ID}_{WATER_MIN_QTY}"))

    assert cart_writes(bot) == [
        ("POST", "/api/v1/cart/items", {"product_id": WATER_ID, "quantity": WATER_MIN_QTY}),
    ], "Checkout has no stock gate of its own — it must still ask the server"
    assert cart.quantity_of(WATER_ID) is None, "nothing may land in the cart"
    assert toasts(bot) == ["❌ Mahsulot sotuvda yo'q"]
    assert bot.telegram.shown == [], (
        "a refused Checkout must not move on — the next screen would be "
        "checking out bottles the server just refused"
    )
    assert checkout_reads(bot) == []


async def test_a_sold_out_product_is_refused_on_plus_and_the_screen_is_not_redrawn(
    bot, user, cart, catalogue
):
    """The line is already in the cart, its screen is open, the product sells
    out, and the customer taps '+1'.

    ``quantity_handler`` used to clamp to stock only when
    ``stock_quantity > 0`` — so at ZERO stock the clamp switched itself off,
    ``upper`` fell back to MAX_QUANTITY (99) and the bot offered one more of
    something it could see there were none of. Zero is a real ceiling of zero:
    nothing is orderable, so the customer is told the product is out of stock,
    and neither the cart nor the screen moves.
    """
    cart.set(WATER_ID, WATER_MIN_QTY)
    await open_quantity_screen(bot, user)
    catalogue[WATER_ID]["inventory"]["stock_quantity"] = 0
    calls_before = len(bot.backend.calls)
    bot.telegram.reset()

    await bot.send(user.tap(f"qty_inc_{WATER_ID}_{WATER_MIN_QTY}"))

    assert [(call.method, call.endpoint) for call in bot.backend.calls[calls_before:]] == [
        ("GET", f"/api/v1/products/{WATER_ID}"),
    ], "a ± tap reads the product and nothing else: no cart read, no cart write"
    assert cart.quantity_of(WATER_ID) == WATER_MIN_QTY, "the cart must not move"
    assert toasts(bot) == [t("telegram.products.out_of_stock")]
    assert bot.telegram.shown == [], (
        "no re-render: the screen must not step to a quantity that does not exist"
    )


async def test_a_sold_out_product_offers_nothing_the_customer_could_tap(
    bot, user, cart, catalogue
):
    """A screen may not offer water that does not exist.

    ``quantity_handler`` refuses ± on a sold-out product (the test above), but
    a refusal the customer only meets AFTER tapping is not the same as not
    being offered. The keyboard used to decide the ceiling for itself —
    ``stock_quantity is not None and stock_quantity > 0`` — which is the same
    "zero disables the ceiling" bug the handler was fixed for, one layer up: at
    zero stock it fell back to MAX_QUANTITY and rendered '5', '8', '12', '15',
    '20' for a sold-out product.

    The bound now comes from the ONE place that resolves it, so a product with
    nothing orderable renders nothing to order: no presets, no ±, no Checkout.
    """
    screen = await open_quantity_screen(bot, user)
    assert any(data.startswith("qty_set_") for data in screen.callback_data())

    # The last bottles go while the customer is looking at it.
    catalogue[WATER_ID]["inventory"]["stock_quantity"] = 0

    await deliberate_retap(bot, user.tap(f"product_{WATER_ID}"))

    sold_out = bot.telegram.last_shown()
    assert sold_out.callback_data() == [f"category_{WATER_CATEGORY}"], (
        f"a sold-out product still offered something to order: {sold_out.callback_data()}"
    )
    assert sold_out.text == "\n".join([
        f"🛒 {WATER_NAME}",
        f"💰 {t('telegram.products.unit_price_label', price=format_price(WATER_UNIT_PRICE))}",
        "",
        f"❌ {t('telegram.products.out_of_stock')}",
    ])
    assert cart_writes(bot) == []


async def test_the_presets_stop_at_the_last_quantity_that_is_actually_there(
    bot, user, cart, catalogue
):
    """Partial stock is a real ceiling too, and it is the handler's ceiling.

    Six bottles left and a product minimum of 2: '+3' (5) is orderable, '+6' (8)
    is not. The keyboard must not offer 8 — and the one it does offer must be a
    quantity ``quantity_handler`` will accept and Checkout will write, which is
    the whole point of the two agreeing.
    """
    catalogue[WATER_ID]["inventory"]["stock_quantity"] = 6

    screen = await open_quantity_screen(bot, user)
    presets = [
        int(data.rsplit("_", 1)[-1])
        for data in screen.callback_data()
        if data.startswith("qty_set_")
    ]
    assert presets == [WATER_MIN_QTY + ProductKeyboards.QUANTITY_PRESET_OFFSETS[0]], (
        f"presets were offered beyond the stock on hand: {presets}"
    )

    # …and the surviving preset is honoured, not clamped away.
    await bot.send(user.tap(f"qty_set_{WATER_ID}_{presets[0]}"))
    checkout = f"buy_{WATER_ID}_{presets[0]}"
    assert checkout in bot.telegram.last_shown().callback_data()

    await bot.send(user.tap(checkout))
    assert cart_writes(bot) == [
        ("POST", "/api/v1/cart/items", {"product_id": WATER_ID, "quantity": presets[0]}),
    ]
    assert cart.quantity_of(WATER_ID) == presets[0]


async def test_a_stock_figure_that_lies_still_reports_the_servers_refusal_on_checkout(
    bot, user, cart
):
    """The bot's stock figure is a snapshot: another customer can take the last
    bottles between the render and the tap, and the catalogue the bot just read
    will still say 40.

    So the local ceiling is a courtesy, never the authority. The screen lets
    the customer step a product their cart does not hold yet up to 3; when the
    server refuses that write at Checkout, its reason has to reach the
    customer, the cart must stay as it was, and checkout must not open on a
    quantity the server never accepted. (A product the cart already holds is
    written on the tap itself; test_quantity_screen_journeys.py covers that
    refusal.)
    """
    await open_quantity_screen(bot, user)
    await bot.send(user.tap(f"qty_inc_{WATER_ID}_{WATER_MIN_QTY}"))
    more = WATER_MIN_QTY + 1
    checkout = f"buy_{WATER_ID}_{more}"
    assert checkout in bot.telegram.last_shown().callback_data(), (
        "stock said 40 — the screen offers one more"
    )

    bot.backend.route(
        "POST",
        "/api/v1/cart/items",
        lambda call: backend_failure("Omborda yetarli mahsulot yo'q", status_code=409),
    )
    bot.telegram.reset()

    await bot.send(user.tap(checkout))

    assert cart_writes(bot) == [
        ("POST", "/api/v1/cart/items", {"product_id": WATER_ID, "quantity": more}),
    ], "stock said 40 — the bot must still ask, and let the server decide"
    assert cart.lines == {}, "the cart must not move"
    assert toasts(bot) == ["❌ Omborda yetarli mahsulot yo'q"]
    assert bot.telegram.shown == [], (
        "nothing may be drawn: not checkout, and not a screen claiming the write landed"
    )
    assert checkout_reads(bot) == []


async def test_a_failing_product_lookup_tells_the_customer_why_plus_did_nothing(
    bot, user, cart
):
    """A '+1' that cannot be honoured must say so, not fake a shrug.

    ``quantity_handler`` used to wrap its whole body in ``if response.success:``
    with no ``else``. When ``GET /products/{id}`` failed — the product was
    deactivated, or the API was having a moment — the handler fell straight
    through to a bare ``query.answer()``: the spinner stopped, no text, no
    change. The customer tapped '+1' repeatedly and the bot pretended nothing
    had happened.

    ``open_quantity_screen`` on the very same product read calls
    ``_handle_api_error`` and toasts the reason; this handler does the same,
    and still touches nothing.
    """
    await open_quantity_screen(bot, user)
    bot.backend.route(
        "GET",
        f"/api/v1/products/{WATER_ID}",
        lambda call: backend_failure("Mahsulot topilmadi", status_code=404),
    )
    calls_before = len(bot.backend.calls)
    bot.telegram.reset()

    await bot.send(user.tap(f"qty_inc_{WATER_ID}_{WATER_MIN_QTY}"))

    assert [(call.method, call.endpoint) for call in bot.backend.calls[calls_before:]] == [
        ("GET", f"/api/v1/products/{WATER_ID}"),
    ]
    assert cart.lines == {}
    assert toasts(bot) == ["❌ Mahsulot topilmadi"]
    assert [call.method for call in bot.telegram.calls] == ["answerCallbackQuery"], (
        "the reason is a toast; re-rendering would claim a change that did not happen"
    )


async def test_an_unreadable_cart_opens_no_screen_and_lets_checkout_write_nothing(
    bot, user, cart
):
    """An unreadable cart is UNKNOWN, not empty.

    The old idempotency guard was ``if existing_qty:``, and ``existing_qty``
    was ``None`` both when the product was absent from the cart AND when
    ``GET /cart`` FAILED. A transient read error therefore re-armed the exact
    accumulation bug the guard was written for (prod 2026-06-27, user 267): the
    customer already has 2, the read 500s, the write POSTs another 2 — and
    ``POST /cart/items`` is an INCREMENT, so they are billed for 4.

    Both places that read the cart now surface the failure instead of
    guessing: Checkout writes nothing (it cannot choose between PUT and POST),
    and opening the screen shows no screen (its starting number would be a
    guess the customer might confirm).
    """
    cart.set(WATER_ID, WATER_MIN_QTY)
    screen = await open_quantity_screen(bot, user)
    checkout = f"buy_{WATER_ID}_{WATER_MIN_QTY}"
    assert checkout in screen.callback_data()

    bot.backend.route(
        "GET",
        "/api/v1/cart",
        lambda call: backend_failure("Savatni o'qib bo'lmadi", status_code=500),
    )
    bot.telegram.reset()

    # Checkout from the screen already on display.
    await bot.send(user.tap(checkout))

    assert cart_writes(bot) == [], (
        "a failed cart read was treated as an empty cart and written on a guess"
    )
    assert cart.quantity_of(WATER_ID) == WATER_MIN_QTY
    assert toasts(bot) == ["❌ Savatni o'qib bo'lmadi"], (
        "the customer must be told why the tap did nothing"
    )
    assert bot.telegram.shown == [] and checkout_reads(bot) == []

    # Opening the product again: no screen at a guessed number.
    await deliberate_retap(bot, user.tap(f"product_{WATER_ID}"))

    assert toasts(bot) == ["❌ Savatni o'qib bo'lmadi"] * 2
    assert bot.telegram.shown == []
    assert cart_writes(bot) == []
    assert cart.quantity_of(WATER_ID) == WATER_MIN_QTY


# ---------------------------------------------------------------------------
# Cart edit mode
# ---------------------------------------------------------------------------


async def test_the_cart_minus_button_stops_at_one_so_only_remove_empties_a_line(
    bot, user, cart
):
    """Sport water has no per-product minimum, so '−' walks it down to 1 and
    then stops. There is no quantity-zero path: a customer who wants the line
    gone must use 'Remove'. If '−' ever wrote 0 the backend would hold a
    zero-quantity line the confirmation screen prices at nothing.
    """
    cart.set(SPORT_ID, 2)
    await open_cart_in_edit_mode(bot, user)

    for _ in range(3):
        screen = bot.telegram.last_shown()
        assert f"cart_dec_{SPORT_ID}" in screen.callback_data()
        await deliberate_retap(bot, user.tap(f"cart_dec_{SPORT_ID}"))

    written = [
        call.data["quantity"]
        for call in api_calls(bot, "PUT", f"/api/v1/cart/items/{SPORT_ID}")
    ]
    assert written == [1, 1, 1], f"'−' walked past one: {written}"
    assert cart.quantity_of(SPORT_ID) == 1
    assert api_calls(bot, "DELETE", f"/api/v1/cart/items/{SPORT_ID}") == []

    # 'Remove' is the only way out, and it takes the line with it.
    await deliberate_retap(bot, user.tap(f"cart_rm_{SPORT_ID}"))
    assert len(api_calls(bot, "DELETE", f"/api/v1/cart/items/{SPORT_ID}")) == 1
    assert cart.quantity_of(SPORT_ID) is None


async def test_removing_the_last_line_leaves_an_empty_cart_screen_with_a_way_out(
    bot, user, cart
):
    """Edit mode renders per-item ± / Remove rows from ``cart_items``. Remove
    the last one and those rows have nothing to describe — if the screen still
    offered them, every button on it would point at a product the cart no
    longer has. The customer must be told the cart is empty and be given a
    route back to the shop.
    """
    cart.set(WATER_ID, 2)
    cart.set(SPORT_ID, 1)
    await open_cart_in_edit_mode(bot, user)

    await bot.send(user.tap(f"cart_rm_{SPORT_ID}"))
    still_editing = bot.telegram.last_shown()
    assert f"cart_rm_{WATER_ID}" in still_editing.callback_data()
    assert f"cart_rm_{SPORT_ID}" not in still_editing.callback_data()
    assert SPORT_NAME not in still_editing.text

    await bot.send(user.tap(f"cart_rm_{WATER_ID}"))

    emptied = bot.telegram.last_shown()
    assert emptied.text == t("telegram.cart_empty")
    assert emptied.callback_data() == ["menu_products"], (
        "an empty cart must offer exactly a way back to the shop and to the menu"
    )
    assert cart.lines == {}


async def test_a_five_hundred_on_a_cart_write_toasts_the_error_and_changes_nothing(
    bot, user, cart
):
    """The customer taps '+' in edit mode and the write fails.

    Two things have to hold together: the failure must be visible (a silent
    failure reads as a frozen bot and gets tapped again), and the screen must
    NOT be re-rendered — a re-render after a failed write is how a screen ends
    up showing a quantity the server never accepted.
    """
    cart.set(SPORT_ID, 2)
    await open_cart_in_edit_mode(bot, user)

    bot.backend.route(
        "PUT",
        f"/api/v1/cart/items/{SPORT_ID}",
        lambda call: backend_failure("Savatni yangilab bo'lmadi", status_code=500),
    )
    bot.telegram.reset()

    await bot.send(user.tap(f"cart_inc_{SPORT_ID}"))

    assert cart.quantity_of(SPORT_ID) == 2
    assert toasts(bot) == ["❌ Savatni yangilab bo'lmadi"]
    assert bot.telegram.shown == []


async def test_clearing_the_cart_empties_it_and_confirms_once(bot, user, cart):
    """'Clear cart' is destructive and unconfirmed, so the customer's only
    feedback is the toast plus the empty screen that follows. Both must happen,
    and the clear must be posted exactly once.
    """
    cart.set(WATER_ID, 2)
    await bot.send(user.tap("cart_view"))

    assert "cart_clear" in bot.telegram.last_shown().callback_data()
    bot.telegram.reset()

    await bot.send(user.tap("cart_clear"))

    assert len(api_calls(bot, "POST", "/api/v1/cart/clear")) == 1
    assert cart.lines == {}
    assert t("telegram.products.cart_cleared") in toasts(bot)
    assert bot.telegram.last_shown().text == t("telegram.cart_empty")


async def test_clearing_a_cart_that_fails_never_says_the_cart_was_cleared(
    bot, user, cart
):
    """If the clear fails and the bot still toasts 'Cart cleared' and renders an
    empty cart, the customer walks away believing an order's worth of bottles
    is gone while the server still holds them — and finds them again at
    checkout.
    """
    cart.set(WATER_ID, 2)
    await bot.send(user.tap("cart_view"))

    bot.backend.route(
        "POST",
        "/api/v1/cart/clear",
        lambda call: backend_failure("Savatni tozalab bo'lmadi", status_code=500),
    )
    bot.telegram.reset()

    await bot.send(user.tap("cart_clear"))

    assert cart.quantity_of(WATER_ID) == 2
    assert toasts(bot) == ["❌ Savatni tozalab bo'lmadi"]
    assert t("telegram.products.cart_cleared") not in toasts(bot)
    assert bot.telegram.shown == [], "the cart must not be redrawn as empty"


# ---------------------------------------------------------------------------
# The money on the screen
# ---------------------------------------------------------------------------


async def test_the_cart_total_is_the_servers_subtotal_not_a_client_side_sum(
    bot, user, cart
):
    """The figure SHOWN and the figure CHARGED are one decision, and it is the
    server's — see
    ``tests/integration/test_cart_screen_total_is_server_authoritative.py``.

    Here the server has dropped the sport line (inactive product): it survives
    in ``cart_items`` with no ``total_price`` and contributes nothing to
    ``subtotal``, which is exactly what the ORDER will contain. Its
    ``current_price`` is still baked into the payload by ``CartItem.to_dict``,
    so any re-multiplication in the bot would sum a line the order will never
    have — and that inflated total also decides whether the checkout button
    exists at all.
    """
    cart.server_unit_price[WATER_ID] = float(MIN_ORDER_AMOUNT) * 0.375  # 7 500
    cart.set(WATER_ID, 2)
    cart.set(SPORT_ID, 2)
    cart.dropped.add(SPORT_ID)

    server_subtotal = float(MIN_ORDER_AMOUNT) * 0.75  # 15 000
    assert cart.subtotal() == server_subtotal
    client_side_sum = WATER_UNIT_PRICE * 2 + SPORT_UNIT_PRICE * 2  # 90 000

    await bot.send(user.tap("cart_view"))
    text = bot.telegram.last_shown().text

    assert f"🛒 {WATER_NAME} x 2 = {format_price(server_subtotal)} UZS" in text
    assert f"🛒 {SPORT_NAME} x 2 = {format_price(0)} UZS" in text, (
        "a server-dropped line contributes 0, which is what the order gets"
    )
    assert (
        f"💰 {t('telegram.cart_total')}: {format_price(server_subtotal)} UZS" in text
    )
    for forbidden in (
        format_price(client_side_sum),  # 90 000 — the re-multiplied total
        format_price(SPORT_UNIT_PRICE * 2),  # 60 000 — the dropped line's raw price
        format_price(WATER_UNIT_PRICE * 2),  # 30 000 — the contract-blind water price
    ):
        assert forbidden not in text, f"{forbidden} can only come from client arithmetic"

    # The gate rides on the same server figure.
    shortfall = min_order_shortfall(server_subtotal)
    assert shortfall > 0
    assert "cart_checkout" not in bot.telegram.last_shown().callback_data(), (
        "checkout must stay locked while the SERVER subtotal is under the floor"
    )
    assert (
        t(
            "telegram.cart_min_order_warning",
            min_amount=format_price(MIN_ORDER_AMOUNT),
            remaining=format_price(shortfall),
        )
        in text
    )


async def test_the_checkout_button_follows_the_server_subtotal_across_the_floor(
    bot, user, cart
):
    """Same cart, same client-visible prices, only the SERVER's pricing moves —
    a contract re-price the bot cannot see. The checkout button and the
    'add N more' copy must both follow the server across the minimum-order
    floor, because ``min_order_shortfall`` is the single expression behind
    both. If either half ever read the client price again, a customer could be
    walked into a checkout the backend then rejects.
    """
    cart.set(WATER_ID, 2)
    assert cart.subtotal() >= MIN_ORDER_AMOUNT

    await deliberate_retap(bot, user.tap("cart_view"))
    above = bot.telegram.last_shown()
    assert "cart_checkout" in above.callback_data()
    assert t("telegram.cart_ready_checkout") in above.text

    # The server re-prices this customer's contract downwards. Nothing about
    # the product payload the bot holds changes.
    cart.server_unit_price[WATER_ID] = float(MIN_ORDER_AMOUNT) * 0.25  # 5 000
    bot.telegram.reset()
    await deliberate_retap(bot, user.tap("cart_view"))

    below = bot.telegram.last_shown()
    assert "cart_checkout" not in below.callback_data(), (
        "the gate must close on the server's new figure"
    )
    assert t("telegram.cart_ready_checkout") not in below.text
    assert (
        t(
            "telegram.cart_min_order_warning",
            min_amount=format_price(MIN_ORDER_AMOUNT),
            remaining=format_price(min_order_shortfall(cart.subtotal())),
        )
        in below.text
    )
    assert format_price(WATER_UNIT_PRICE * 2) not in below.text


async def test_a_line_under_its_product_minimum_blocks_checkout_and_says_why(
    bot, user, cart
):
    """A cart can clear the money floor and still be un-orderable: water is
    sold in twos and the backend enforces that. The customer must be told which
    product and how many more, not just handed a cart with no checkout button.
    """
    cart.set(WATER_ID, 1)
    cart.server_unit_price[WATER_ID] = float(MIN_ORDER_AMOUNT) * 3  # money floor cleared

    await bot.send(user.tap("cart_view"))
    screen = bot.telegram.last_shown()

    assert min_order_shortfall(cart.subtotal()) == 0, "the money floor is not the issue"
    assert "cart_checkout" not in screen.callback_data()
    assert (
        t(
            "telegram.cart_min_qty_warning",
            product_name=WATER_NAME,
            min_qty=WATER_MIN_QTY,
            remaining=WATER_MIN_QTY - 1,
        )
        in screen.text
    )
    assert t("telegram.cart_ready_checkout") not in screen.text


# ---------------------------------------------------------------------------
# Telegram and the customer misbehaving
# ---------------------------------------------------------------------------


async def test_a_telegram_edit_failure_still_shows_the_customer_the_edited_cart(
    bot, user, cart
):
    """'Message to edit not found' is in this project's production logs — it is
    what a deleted/duplicated bubble looks like. The write has already landed
    by then, so the customer must still be shown the resulting cart, in a fresh
    message, rather than left staring at the pre-edit screen believing their
    tap did nothing.
    """
    cart.set(SPORT_ID, 2)
    await open_cart_in_edit_mode(bot, user)

    bot.telegram.fail("editMessageText", "Bad Request: message to edit not found")
    bot.telegram.reset()

    await bot.send(user.tap(f"cart_inc_{SPORT_ID}"))

    assert cart.quantity_of(SPORT_ID) == 3, "the write must not be rolled back"
    replacement = bot.telegram.last_shown()
    assert replacement.method == "sendMessage", (
        "a failed edit must be replaced by a new message, not swallowed"
    )
    assert f"🛒 {SPORT_NAME} x 3 = {format_price(SPORT_UNIT_PRICE * 3)} UZS" in (
        replacement.text
    )
    assert f"cart_inc_{SPORT_ID}" in replacement.callback_data(), (
        "the replacement message must be usable, not a dead end"
    )


async def test_switching_language_mid_edit_relabels_the_cart_on_the_next_tap(
    bot, user, cart
):
    """Language is read per update from the user row, not cached into the flow.
    A customer who switches to Russian while their cart is open must get a
    Russian cart on the next tap — a half-translated screen is the exact shape
    of the English-leak class this project has shipped before.
    """
    cart.set(SPORT_ID, 2)
    await deliberate_retap(bot, user.tap("cart_view"))
    assert t("telegram.cart_title") in bot.telegram.last_shown().text

    bot.database.user["preferred_language"] = "ru"
    bot.telegram.reset()
    await deliberate_retap(bot, user.tap("cart_view"))

    screen = bot.telegram.last_shown()
    assert t("telegram.cart_title", "ru") in screen.text
    assert t("telegram.cart_total", "ru") in screen.text
    assert t("telegram.cart_title", "uz") not in screen.text
    assert t("telegram.cart_total", "uz") not in screen.text
    assert t("telegram.cart.checkout", "ru") in screen.button_labels()


async def test_every_button_the_cart_and_quantity_screens_render_is_claimed(
    bot, user, cart
):
    """A tap with no matching handler shows a spinner and then nothing — the
    commonest way a Telegram flow dies silently. Walk the cart surfaces and
    check every rendered ``callback_data`` is claimed by a REAL handler.

    The filter matters: ``_setup_handlers`` registers three handlers that claim
    every update and process none of them — the debug logger and callback-dedup
    ``TypeHandler``s, and the pattern-less
    ``CallbackQueryHandler(debug_callback_handler)`` at group -1. Counting any
    of them would make this assertion pass for literally any string, so
    ``handlers_matching`` drops them; the control below proves that is doing
    real work.
    """

    def real_handlers_for(data: str):
        return bot.handlers_matching(user.tap(data))

    # Control: without the exclusion this test would be vacuous.
    assert bot.handlers_matching(user.tap("zzz_not_a_real_button"), include_catch_alls=True), (
        "the catch-alls are expected to match everything"
    )
    assert real_handlers_for("zzz_not_a_real_button") == []

    cart.set(WATER_ID, 2)
    cart.set(SPORT_ID, 1)

    screens = []
    await deliberate_retap(bot, user.tap("cart_view"))
    screens.append(("the cart summary", bot.telegram.last_shown()))
    await deliberate_retap(bot, user.tap("edit_order"))
    screens.append(("the cart in edit mode", bot.telegram.last_shown()))
    await deliberate_retap(bot, user.tap(f"product_{WATER_ID}"))
    screens.append(("the quantity screen", bot.telegram.last_shown()))

    for where, screen in screens:
        assert screen.callback_data(), f"{where} rendered no buttons at all"
        for data in screen.callback_data():
            assert real_handlers_for(data), (
                f"the '{data}' button on {where} lands nowhere: no registered "
                f"handler claims it"
            )
