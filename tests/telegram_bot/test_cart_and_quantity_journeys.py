"""The customer's cart, driven end to end through the REAL PTB dispatcher.

WHY THIS FILE EXISTS
--------------------
``test_handlers_add_to_cart_idempotent.py`` and ``test_cart_edit_mode.py`` both
call handler coroutines directly with a hand-rolled ``DummyUpdate``. That proves
each handler behaves when it is CALLED. It cannot prove any of the things the
cart actually breaks on:

* whether the ``callback_data`` a keyboard rendered is registered at all,
* what the SECOND tap of the same button does (the dedup middleware and the
  idempotent entry point are two different defences and only one of them is
  wired in production's ``initialize()``),
* what the customer is left looking at when a cart write fails,
* whether the money on the screen is the money the server will charge.

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
from tests.telegram_bot.ptb_harness import backend_failure, build_bot_harness

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


def build_catalogue() -> dict:
    """Fresh per test — several tests mutate stock mid-journey."""
    return {
        WATER_ID: {
            "id": WATER_ID,
            "name": WATER_NAME,
            "current_price": WATER_UNIT_PRICE,
            "description": "Tabiiy ichimlik suvi",
            "category": {"id": 1, "name": "Suv"},
            "inventory": {"min_order_quantity": WATER_MIN_QTY, "stock_quantity": 40},
            "specifications": {"volume": 18.9, "volume_unit": "l"},
        },
        SPORT_ID: {
            "id": SPORT_ID,
            "name": SPORT_NAME,
            "current_price": SPORT_UNIT_PRICE,
            "description": "Sport suvi",
            "category": {"id": 1, "name": "Suv"},
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


async def open_quantity_selector(bot, user, product_id=WATER_ID):
    """Tap 'Add to cart' on the product-details screen."""
    await bot.send(user.tap(f"add_to_cart_{product_id}"))
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


async def test_tapping_add_to_cart_twice_writes_one_line_at_the_product_minimum(
    bot, user, cart
):
    """A hesitant customer taps 'Add to cart', goes back, taps it again.

    ``POST /cart/items`` is an INCREMENT on the backend, so if the entry point
    stops being idempotent the line silently grows by ``min_order_quantity``
    per tap and the customer is charged for bottles they never asked for. That
    is prod triage 2026-06-27 (user 267) verbatim; this drives it through the
    dispatcher so the guard is exercised on the real update path, not on a
    hand-built one.
    """
    await open_quantity_selector(bot, user)
    # Back to the product card, then add again — the exact loop from the trace.
    await deliberate_retap(bot, user.tap(f"back_to_product_{WATER_ID}"))
    await open_quantity_selector(bot, user)

    adds = api_calls(bot, "POST", "/api/v1/cart/items")
    assert [call.data for call in adds] == [
        {"product_id": WATER_ID, "quantity": WATER_MIN_QTY}
    ], "the second tap must not post a second increment"
    assert cart.quantity_of(WATER_ID) == WATER_MIN_QTY

    # ...and the customer is looking at the quantity they actually have.
    shown = bot.telegram.last_shown()
    assert f"{t('telegram.quantity')}: {WATER_MIN_QTY}" in shown.text
    assert f"qty_inc_{WATER_ID}_{WATER_MIN_QTY}" in shown.callback_data()


async def test_a_double_tap_inside_the_dedup_window_never_reaches_the_cart(
    bot, user, cart
):
    """Telegram keeps the button spinner up for the whole handler, so people
    tap again. With ``PerChatSerialUpdateProcessor`` the second tap queues and
    runs against a message the first tap already deleted, producing the
    'Message to edit not found' / 'Message to delete not found' warning pair in
    production and a second quantity selector for the customer.

    The middleware must stop the duplicate before ANY handler runs, and must
    still answer it so the spinner clears. It must also be a debounce, not a
    ban: once the window passes, a deliberate re-tap has to work again.
    """

    await bot.send(user.tap(f"add_to_cart_{WATER_ID}"))
    assert api_calls(bot, "POST", "/api/v1/cart/items"), "first tap must go through"

    calls_after_first_tap = len(bot.backend.calls)
    bot.telegram.reset()

    await bot.send(user.tap(f"add_to_cart_{WATER_ID}"))

    assert len(bot.backend.calls) == calls_after_first_tap, (
        "the duplicate tap reached the backend — no handler may see it at all"
    )
    assert [call.method for call in bot.telegram.calls] == ["answerCallbackQuery"], (
        "a dropped duplicate must be acknowledged and nothing else"
    )
    assert "text" not in bot.telegram.calls[0].params, (
        "the drop is silent to the customer; a toast would look like an error"
    )

    # The window passes; the customer genuinely wants the screen again.
    expire_dedup_window()
    bot.telegram.reset()
    await bot.send(user.tap(f"add_to_cart_{WATER_ID}"))

    assert bot.telegram.shown, "a deliberate re-tap after the window must render"
    assert cart.quantity_of(WATER_ID) == WATER_MIN_QTY, (
        "and it still must not increment — dedup is the second line of defence, "
        "not the only one"
    )


# ---------------------------------------------------------------------------
# The quantity selector
# ---------------------------------------------------------------------------


async def test_a_preset_jump_writes_the_absolute_quantity_the_button_shows(
    bot, user, cart
):
    """The presets are labelled with absolute quantities ('8'), so tapping '8'
    must leave exactly 8 in the cart. If the write ever became relative the
    label and the cart would disagree and the customer would only find out on
    the confirmation screen.
    """
    selector = await open_quantity_selector(bot, user)

    target = WATER_MIN_QTY + ProductKeyboards.QUANTITY_PRESET_OFFSETS[1]
    assert f"qty_set_{WATER_ID}_{target}" in selector.callback_data(), (
        "the preset this test taps must be a button the customer can actually see"
    )

    bot.telegram.reset()
    await bot.send(user.tap(f"qty_set_{WATER_ID}_{target}"))

    writes = api_calls(bot, "PUT", f"/api/v1/cart/items/{WATER_ID}")
    assert [call.data for call in writes] == [{"quantity": target}]
    assert cart.quantity_of(WATER_ID) == target

    shown = bot.telegram.last_shown()
    assert f"{t('telegram.quantity')}: {target}" in shown.text
    # NOTE: this line is the selector's own `unit_price * quantity` preview, and
    # `unit_price` is the contract-blind `current_price` baked by
    # `Product.calculate_price`. It is pinned here as CURRENT behaviour, not
    # endorsed: it is a smaller instance of the same show-vs-charge split the
    # cart screen was fixed for. Nothing is posted from it.
    assert (
        f"{t('telegram.total')}: {format_price(WATER_UNIT_PRICE * target)} UZS"
        in shown.text
    )
    # The re-rendered fine-tune row must carry the NEW quantity, or the next
    # +1 would step from a stale number.
    assert f"qty_inc_{WATER_ID}_{target}" in shown.callback_data()
    assert f"qty_dec_{WATER_ID}_{target}" in shown.callback_data()


async def test_the_minus_button_never_takes_a_line_below_the_product_minimum(
    bot, user, cart
):
    """Water is sold in twos. Holding '−1' down must clamp at 2 — it must never
    write 1 (which the backend rejects, stranding the customer on a cart it
    refuses to check out) and never write 0 (which is not how a line is
    removed; only 'Remove' does that).
    """
    await open_quantity_selector(bot, user)
    assert cart.quantity_of(WATER_ID) == WATER_MIN_QTY

    for _ in range(3):
        selector = bot.telegram.last_shown()
        minus = f"qty_dec_{WATER_ID}_{WATER_MIN_QTY}"
        assert minus in selector.callback_data()
        await deliberate_retap(bot, user.tap(minus))

    written = [
        call.data["quantity"]
        for call in api_calls(bot, "PUT", f"/api/v1/cart/items/{WATER_ID}")
    ]
    assert written == [WATER_MIN_QTY] * 3, f"'−1' wrote below the floor: {written}"
    assert cart.quantity_of(WATER_ID) == WATER_MIN_QTY
    assert api_calls(bot, "DELETE", f"/api/v1/cart/items/{WATER_ID}") == [], (
        "decrementing must never remove the line behind the customer's back"
    )


async def test_a_stale_plus_button_steps_from_the_quantity_the_customer_chose(
    bot, user, cart
):
    """A '+1' scrolled back to in the chat must add one to what the cart HOLDS.

    ``quantity_handler`` used to take its base from the CALLBACK PAYLOAD
    (``qty_inc_{pid}_{qty}``), so a '+1' button from a message further up the
    chat wrote ``payload_qty + 1`` over whatever the cart really held: a
    customer who picked 8, scrolled up and tapped the old '+1' silently dropped
    to 3 and only found out at the confirmation screen.

    The base now comes from the SERVER cart, exactly as
    ``_handle_cart_item_action`` already did for the edit-mode ± buttons — one
    rule, one expression, both surfaces.
    """
    first_render = await open_quantity_selector(bot, user)
    stale_plus = f"qty_inc_{WATER_ID}_{WATER_MIN_QTY}"
    assert stale_plus in first_render.callback_data()

    target = WATER_MIN_QTY + ProductKeyboards.QUANTITY_PRESET_OFFSETS[1]
    await bot.send(user.tap(f"qty_set_{WATER_ID}_{target}"))
    assert cart.quantity_of(WATER_ID) == target

    await bot.send(user.tap(stale_plus))

    assert cart.quantity_of(WATER_ID) == target + 1, (
        "the stale button must step from the cart, not from its own payload"
    )
    written = [
        call.data["quantity"]
        for call in api_calls(bot, "PUT", f"/api/v1/cart/items/{WATER_ID}")
    ]
    assert written == [target, target + 1], (
        f"the stale tap wrote a quantity nobody chose: {written}"
    )


async def test_tapping_the_quantity_display_cell_writes_nothing(bot, user, cart):
    """The centre cell of the fine-tune row is a label, not an action, but it
    still routes through the broad ``^qty_`` pattern. If it ever stopped being
    a no-op, every customer who taps the number would move their own order.
    """
    await open_quantity_selector(bot, user)
    calls_before = len(bot.backend.calls)
    bot.telegram.reset()

    await bot.send(user.tap("qty_current"))

    assert len(bot.backend.calls) == calls_before
    assert cart.quantity_of(WATER_ID) == WATER_MIN_QTY
    assert [call.method for call in bot.telegram.calls] == ["answerCallbackQuery"], (
        "the spinner must be dismissed, and nothing else may happen"
    )


# ---------------------------------------------------------------------------
# The shelf moving under the customer
# ---------------------------------------------------------------------------


async def test_a_product_that_sells_out_before_the_tap_shows_the_servers_refusal(
    bot, user, cart, catalogue
):
    """The product card was rendered while stock was 40. By the time the
    customer taps 'Add to cart' the last bottle is gone.

    The bot has NO client-side stock gate on this path — ``add_to_cart`` never
    looks at ``stock_quantity`` — so the server's rejection is the only thing
    standing between the customer and a line that cannot be delivered. It must
    reach them as text, and nothing may be written.
    """
    catalogue[WATER_ID]["inventory"]["stock_quantity"] = 0
    bot.backend.route(
        "POST",
        "/api/v1/cart/items",
        lambda call: backend_failure("Mahsulot sotuvda yo'q", status_code=409),
    )

    await bot.send(user.tap(f"add_to_cart_{WATER_ID}"))

    assert api_calls(bot, "POST", "/api/v1/cart/items"), (
        "the bot has no stock gate of its own — it must still ask the server"
    )
    assert cart.quantity_of(WATER_ID) is None, "nothing may land in the cart"
    assert toasts(bot) == ["❌ Mahsulot sotuvda yo'q"]
    assert bot.telegram.shown == [], (
        "a refused add must not open the quantity selector — that screen would "
        "claim the customer owns bottles the server just refused them"
    )


async def test_a_sold_out_line_is_refused_by_the_selector_before_it_asks(
    bot, user, cart, catalogue
):
    """Same shelf, one screen later: the line is already in the cart when the
    product sells out, and the customer taps '+1'.

    ``quantity_handler`` used to clamp to stock only when
    ``stock_quantity > 0`` — so at ZERO stock the clamp switched itself off,
    ``upper`` fell back to MAX_QUANTITY (99) and the bot asked the backend for
    one more of something it could see there were none of. Zero is a real
    ceiling of zero: nothing is orderable, so nothing is written and the
    customer is told the product is out of stock.

    The screen must not be re-rendered either — re-rendering would show a
    quantity the cart does not hold.
    """
    await open_quantity_selector(bot, user)
    catalogue[WATER_ID]["inventory"]["stock_quantity"] = 0
    bot.telegram.reset()

    await bot.send(user.tap(f"qty_inc_{WATER_ID}_{WATER_MIN_QTY}"))

    assert api_calls(bot, "PUT", f"/api/v1/cart/items/{WATER_ID}") == [], (
        "the bot must not ask for stock it can see is not there"
    )
    assert cart.quantity_of(WATER_ID) == WATER_MIN_QTY, "the cart must not move"
    assert toasts(bot) == [t("telegram.products.out_of_stock")]
    assert bot.telegram.shown == [], (
        "no re-render: the screen must keep showing the quantity that is real"
    )


async def test_a_sold_out_product_offers_no_preset_the_customer_could_tap(
    bot, user, cart, catalogue
):
    """A screen may not offer water that does not exist.

    ``quantity_handler`` refuses every one of these taps (the test above), but a
    refusal the customer only meets AFTER tapping is not the same as not being
    offered. The keyboard used to decide the ceiling for itself —
    ``stock_quantity is not None and stock_quantity > 0`` — which is the same
    "zero disables the ceiling" bug the handler was fixed for, one layer up: at
    zero stock it fell back to MAX_QUANTITY and rendered '5', '8', '12', '15',
    '20' for a sold-out product.

    The bound now comes from the ONE place that resolves it, so a product with
    nothing orderable renders nothing to order.
    """
    await open_quantity_selector(bot, user)
    assert cart.quantity_of(WATER_ID) == WATER_MIN_QTY

    # The last bottles go while the customer is on the product card.
    catalogue[WATER_ID]["inventory"]["stock_quantity"] = 0

    await deliberate_retap(bot, user.tap(f"back_to_product_{WATER_ID}"))
    await deliberate_retap(bot, user.tap(f"add_to_cart_{WATER_ID}"))

    selector = bot.telegram.last_shown()
    presets = [
        data for data in selector.callback_data() if data.startswith("qty_set_")
    ]
    assert presets == [], (
        f"a sold-out product still offered preset quantities: {presets}"
    )


async def test_the_presets_stop_at_the_last_quantity_that_is_actually_there(
    bot, user, cart, catalogue
):
    """Partial stock is a real ceiling too, and it is the handler's ceiling.

    Six bottles left and a product minimum of 2: '+3' (5) is orderable, '+6' (8)
    is not. The keyboard must not offer 8 — and the one it does offer must be a
    quantity ``quantity_handler`` will accept, which is the whole point of the
    two agreeing.
    """
    catalogue[WATER_ID]["inventory"]["stock_quantity"] = 6

    selector = await open_quantity_selector(bot, user)
    presets = [
        int(data.rsplit("_", 1)[-1])
        for data in selector.callback_data()
        if data.startswith("qty_set_")
    ]
    assert presets == [WATER_MIN_QTY + ProductKeyboards.QUANTITY_PRESET_OFFSETS[0]], (
        f"presets were offered beyond the stock on hand: {presets}"
    )

    # …and the surviving preset is honoured, not clamped away.
    await bot.send(user.tap(f"qty_set_{WATER_ID}_{presets[0]}"))
    assert cart.quantity_of(WATER_ID) == presets[0]


async def test_a_stock_figure_that_lies_still_reports_the_servers_refusal_on_plus(
    bot, user, cart
):
    """The bot's stock figure is a snapshot: another customer can take the last
    bottle between the render and the tap, and the catalogue the bot just read
    will still say 40.

    So the local ceiling is a courtesy, never the authority. When the server
    refuses the write anyway, its reason has to reach the customer AND the
    screen must not be re-rendered — re-rendering would show a quantity the
    server never accepted.
    """
    await open_quantity_selector(bot, user)
    bot.backend.route(
        "PUT",
        f"/api/v1/cart/items/{WATER_ID}",
        lambda call: backend_failure("Omborda yetarli mahsulot yo'q", status_code=409),
    )
    bot.telegram.reset()

    await bot.send(user.tap(f"qty_inc_{WATER_ID}_{WATER_MIN_QTY}"))

    assert api_calls(bot, "PUT", f"/api/v1/cart/items/{WATER_ID}"), (
        "stock said 40 — the bot must still ask, and let the server decide"
    )
    assert cart.quantity_of(WATER_ID) == WATER_MIN_QTY, "the cart must not move"
    assert toasts(bot) == ["❌ Omborda yetarli mahsulot yo'q"]
    assert bot.telegram.shown == [], (
        "no re-render: the screen must keep showing the quantity that is real"
    )


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

    ``add_to_cart`` on the very same payload calls ``_handle_api_error`` and
    toasts the reason; this handler now does the same, and still writes nothing.
    """
    await open_quantity_selector(bot, user)
    bot.backend.route(
        "GET",
        f"/api/v1/products/{WATER_ID}",
        lambda call: backend_failure("Mahsulot topilmadi", status_code=404),
    )
    bot.telegram.reset()

    await bot.send(user.tap(f"qty_inc_{WATER_ID}_{WATER_MIN_QTY}"))

    assert api_calls(bot, "PUT", f"/api/v1/cart/items/{WATER_ID}") == []
    assert cart.quantity_of(WATER_ID) == WATER_MIN_QTY
    assert toasts(bot) == ["❌ Mahsulot topilmadi"]
    assert [call.method for call in bot.telegram.calls] == ["answerCallbackQuery"], (
        "the reason is a toast; re-rendering would claim a change that did not happen"
    )


async def test_a_failing_cart_read_stops_add_to_cart_instead_of_stacking_a_line(
    bot, user, cart
):
    """An unreadable cart is UNKNOWN, not empty.

    The idempotency guard was ``if existing_qty:``, and ``existing_qty`` was
    ``None`` both when the product was absent from the cart AND when
    ``GET /cart`` FAILED. A transient read error therefore re-armed the exact
    accumulation bug the guard was written for (prod 2026-06-27, user 267): the
    customer already has 2, the read 500s, the tap POSTs another 2 — and
    ``POST /cart/items`` is an INCREMENT, so they are billed for 4.

    The read failure is now surfaced instead, and nothing is written on a guess.
    """
    await open_quantity_selector(bot, user)
    assert cart.quantity_of(WATER_ID) == WATER_MIN_QTY

    bot.backend.route(
        "GET",
        "/api/v1/cart",
        lambda call: backend_failure("Savatni o'qib bo'lmadi", status_code=500),
    )

    await deliberate_retap(bot, user.tap(f"back_to_product_{WATER_ID}"))
    await deliberate_retap(bot, user.tap(f"add_to_cart_{WATER_ID}"))

    assert cart.quantity_of(WATER_ID) == WATER_MIN_QTY, (
        "a failed cart read was treated as an empty cart and doubled the line"
    )
    assert len(api_calls(bot, "POST", "/api/v1/cart/items")) == 1, (
        "only the first, readable open may write"
    )
    assert toasts(bot)[-1] == "❌ Savatni o'qib bo'lmadi", (
        "the customer must be told why the tap did nothing"
    )


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
    assert emptied.callback_data() == ["menu_products", "back_to_main"], (
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
    await deliberate_retap(bot, user.tap(f"add_to_cart_{WATER_ID}"))
    screens.append(("the quantity selector", bot.telegram.last_shown()))

    for where, screen in screens:
        assert screen.callback_data(), f"{where} rendered no buttons at all"
        for data in screen.callback_data():
            assert real_handlers_for(data), (
                f"the '{data}' button on {where} lands nowhere: no registered "
                f"handler claims it"
            )
