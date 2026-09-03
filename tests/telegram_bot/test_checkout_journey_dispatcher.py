"""The customer bot's shop: browse -> cart -> checkout -> order, through the
REAL PTB dispatcher.

WHY THIS FILE EXISTS
--------------------
The checkout journey is the only path in this product that takes money, and it
is stitched together from THREE handler modules that hand off to each other
through `context.user_data` and raw `callback_data` strings:

    products.cart_handler ──'checkout'──▶ orders.checkout_handler
    orders.address_handler ─────────────▶ orders._show_payment_picker
    orders.payment_handler ─────────────▶ orders._show_order_confirmation
    orders.confirm_order   ─────────────▶ POST /api/v1/orders

Nothing in the suite drove that chain end to end. Every seam in it is a
string: the cart renders `cart_checkout`, the quantity selector renders a bare
`checkout`, the payment keyboard renders `payment_card` while the backend calls
the same rail `click`, and the order payload is assembled from `user_data` keys
written three screens earlier. A single-handler test cannot see any of that —
it calls the handler directly and therefore proves only that the handler works
when something calls it.

So every update below goes in through `Application.process_update`. The
keyboards are the real ones, the `callback_data` is whatever production
rendered, the handler-group ordering is real, and the assertions are about what
the customer SAW (`bot.telegram`) and what reached the backend
(`bot.backend.calls`) — never about a mock having been called.
"""

from __future__ import annotations

import copy

import time

import pytest

# Imported at module (collection) level so `i18n`, `keyboards` and `config`
# resolve to the BOT's modules; see tests/telegram_bot/conftest.py.
from handlers.products import min_order_shortfall
from shared.business_config import MIN_ORDER_AMOUNT

from tests.telegram_bot.ptb_harness import (
    DEFAULT_USER_ID,
    BackendCall,
    backend_failure,
    build_bot_harness,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


def expire_dedup_window() -> None:
    """Age every in-memory callback-dedup lock past its 2-second TTL.

    Since 2026-08-21 the harness carries the real dedup guard, because
    production registers it in `_setup_handlers()`. That is the right default:
    an impatient double-tap IS debounced in production. But a customer who
    re-taps Checkout after walking a whole address flow, or retries Confirm
    after reading an error, is acting seconds later — so these tests age the
    lock table rather than putting real seconds into the suite.
    """
    from handlers import callback_dedup

    stale = time.monotonic() - 1
    for key in list(callback_dedup._in_memory_locks):
        callback_dedup._in_memory_locks[key] = stale


async def deliberate_retap(bot, update):
    """A tap the customer made deliberately, seconds after an identical one."""
    expire_dedup_window()
    return await bot.send(update)




# ---------------------------------------------------------------------------
# The shop the customer is browsing
# ---------------------------------------------------------------------------

WATER = 1
HARDWARE = 2

BOTTLE_19L = 101
BOTTLE_5L = 102
PUMP = 201


def _product(product_id, name, price, *, category_id, category_name,
             min_order_quantity=1, stock=50, volume=19, volume_unit="L"):
    """One product exactly as `GET /api/v1/products/{id}` returns it.

    Deliberately carries BOTH `pricing.current_price` and the `inventory` /
    `specifications` sub-dicts the renderers index into directly — a payload
    missing either is a `KeyError` inside `_format_products_list`, which is one
    of the shapes this journey has to survive.
    """
    return {
        "id": product_id,
        "name": name,
        "description": f"{name} description",
        "pricing": {"current_price": price, "base_price": price},
        "inventory": {"stock_quantity": stock, "min_order_quantity": min_order_quantity},
        "specifications": {"volume": volume, "volume_unit": volume_unit},
        "category": {"id": category_id, "name": category_name},
    }


CATEGORIES = [
    {"id": WATER, "name": "Suv"},
    {"id": HARDWARE, "name": "Jihozlar"},
]

PRODUCTS = {
    BOTTLE_19L: _product(
        BOTTLE_19L, "Aqua Element 19L", 15000,
        category_id=WATER, category_name="Suv", min_order_quantity=2, stock=40,
    ),
    BOTTLE_5L: _product(
        BOTTLE_5L, "Aqua Element 5L", 6000,
        category_id=WATER, category_name="Suv", volume=5,
    ),
    PUMP: _product(
        PUMP, "Qopqoq nasosi", 30000,
        category_id=HARDWARE, category_name="Jihozlar", volume=1, volume_unit="dona",
    ),
}


# Only the keys whose INTERPOLATED value is load-bearing are seeded. Everything
# else falls through to `i18n.humanised_missing_key`, which is what an unseeded
# key renders as in production too — so no test here can lean on a translation
# that does not exist.
TRANSLATIONS = {
    "telegram.orders.grand_total": "Grand total: {amount} UZS",
    "telegram.orders.estimate_tier_line": "{tier_name} discount -{percentage}%: -{amount} UZS",
    # `{icon}` replaced `{method}` (owner screenshot review): the rail's name
    # is already stated on the confirmation screen's "Payment method" line
    # above, so the payable line carries only the icon that ties to it.
    "telegram.orders.estimate_payable": "{icon} To pay: {amount} UZS",
    # The payment-method PICKER's neutral total — basket plus the plain,
    # undiscounted figure, shown before any rail (and its discount) is chosen.
    "telegram.orders.estimate_neutral_total": "Total: {amount} UZS",
    "telegram.orders.estimate_cod_savings": "Pay cash and save {amount} UZS",
    "telegram.orders.delivery_fee": "Delivery: {amount} UZS",
    "telegram.cart_min_order_warning": "Minimum {min_amount} UZS. Add {remaining} UZS more",
    "telegram.cart_min_qty_warning": "{product_name}: minimum {min_qty}, add {remaining}",
    "telegram.payment.pay_message": "Pay order {order_number}: {amount} UZS",
    # The card rail's "the order exists but the link does not" screen. Seeded
    # because the ORDER NUMBER in it is the whole point: a screen that cannot
    # name the order it is talking about tells the customer nothing they can
    # act on.
    "telegram.orders.payment_link_failed_message":
        "Order {order_number} is placed. We could not create the payment link.",
    # N-2: the retry path's OTHER screen -- rendered instead of the generic
    # payment_link_failed_message above when the backend refuses a cash-order
    # rail flip for a short marking-code pool. Distinguishable text on
    # purpose, so a test can prove WHICH of the two screens rendered.
    "telegram.payment.marking_codes_unavailable":
        "Your order stays on Cash on Delivery.",
    "telegram.order.number": "Order {0}",
    "telegram.order.total": "Total {0} UZS",
    # Seeded on purpose while `telegram.orders.cod_restricted_place` is left
    # UNSEEDED — that is the shape production has in any environment where the
    # newer key's seed script has not run.
    "telegram.orders.cod_restricted_has_debts":
        "Cash is unavailable: {active_debt_count} unpaid orders",
    # A language the customer can switch INTO mid-checkout. The uz rows are the
    # ones every other test reads, so a leak in either direction is visible.
    ("uz", "telegram.orders.select_payment"): "To'lov usulini tanlang",
    ("ru", "telegram.orders.select_payment"): "Выберите способ оплаты",
    # Real production copy carries its own icon now (the keyboard no longer
    # prepends a second one — the double-icon bug this task fixed).
    ("uz", "telegram.payment_cash"): "💰 Naqd pul",
    ("ru", "telegram.payment_cash"): "💰 Наличные",
    ("uz", "telegram.payment_card"): "💳 Karta",
    ("ru", "telegram.payment_card"): "💳 Карта",
    ("uz", "telegram.orders.confirmation_title"): "Buyurtmani tasdiqlang",
    ("ru", "telegram.orders.confirmation_title"): "Подтвердите заказ",
}


class Shop:
    """An in-memory catalogue + cart + order book behind the real api_client.

    Installed onto :class:`~tests.telegram_bot.ptb_harness.FakeBackend` with
    `route(...)`, so the bot's real `get_products` / `add_to_cart` /
    `create_order` wrappers run and their real endpoint paths and payload
    shapes are part of what is being asserted.
    """

    def __init__(self, backend, monkeypatch=None, *, products=PRODUCTS,
                 categories=CATEGORIES):
        self.backend = backend
        self.products = copy.deepcopy(products)
        self.categories = copy.deepcopy(categories)

        self.cart: dict[int, int] = {}
        self.orders: list[dict] = []
        self.next_order_id = 7100

        # Overrides that let a test make the SERVER disagree with naive
        # client-side arithmetic — the exact condition the "never re-multiply"
        # comments in products.py / orders.py exist for.
        self.line_total_overrides: dict[int, float] = {}
        self.subtotal_override: float | None = None

        # The server's quote. Overridable so a test can put a customer in a
        # discounting tier without standing up the whole loyalty schema — the
        # SHAPE is what the bot is being driven against here.
        self.estimate_pricing: dict = {}

        self.available_methods = [
            {"method": "cash", "is_active": True},
            {"method": "click", "is_active": True},
            {"method": "business_account", "is_active": True},
        ]
        self.payment_restrictions: dict = {}
        self.order_response = None  # None -> succeed

        # (METHOD, endpoint) -> a whole APIResponse, returned untouched.
        # `FakeBackend.route` can only express success-with-body or
        # failure-without-body, but the real client sets `data` to the FULL
        # error body on a 4xx/5xx (api_client.py:368) — which is how the Asl
        # belgisi 503 hands back `cancelled_order_id`. A test that hand-stuffs
        # that id into user_data would be re-implementing the very step it is
        # supposed to be proving.
        self.raw_responses: dict[tuple[str, str], object] = {}

        self._install()
        if monkeypatch is not None:
            self._install_raw_channel(monkeypatch)

    # -- wiring ---------------------------------------------------------------

    def _install_raw_channel(self, monkeypatch):
        import api_client as api_client_module

        inner = self.backend.handle

        async def dispatch(method, endpoint, data=None, params=None, **kwargs):
            raw = self.raw_responses.get((method.upper(), endpoint))
            if raw is not None:
                self.backend.calls.append(
                    BackendCall(method.upper(), endpoint, data, params)
                )
                return raw
            return await inner(method, endpoint, data=data, params=params, **kwargs)

        monkeypatch.setattr(api_client_module.api_client, "_make_request", dispatch)

    def _install(self):
        route = self.backend.route
        route("GET", "/api/v1/products/categories",
              lambda call: {"data": {"categories": self.categories}})
        route("GET", "/api/v1/products", self._products_page)
        route("GET", "/api/v1/cart", lambda call: self.cart_envelope())
        route("POST", "/api/v1/cart/items", self._add_item)
        route("POST", "/api/v1/cart/clear", self._clear)
        route("GET", "/api/v1/payments/methods", self._payment_methods)
        route("POST", "/api/v1/orders", self._create_order)
        route("POST", "/api/v1/orders/cart/estimate", self._estimate)
        route("POST", "/api/v1/payments/create", self._create_payment)

        for category in self.categories:
            route("GET", f"/api/v1/products/categories/{category['id']}",
                  lambda call, c=category: {"data": {"category": dict(c)}})
        for product_id in self.products:
            route("GET", f"/api/v1/products/{product_id}",
                  lambda call, pid=product_id: {"data": {"product": self.products[pid]}})
            route("PUT", f"/api/v1/cart/items/{product_id}",
                  lambda call, pid=product_id: self._set_quantity(pid, call))
            route("DELETE", f"/api/v1/cart/items/{product_id}",
                  lambda call, pid=product_id: self._remove(pid))

    # -- catalogue ------------------------------------------------------------

    def _products_page(self, call):
        category_id = (call.params or {}).get("category_id")
        items = [
            product for product in self.products.values()
            if category_id is None or str(product["category"]["id"]) == str(category_id)
        ]
        return {"data": {"items": items}, "meta": {"pages": 1}}

    # -- cart -----------------------------------------------------------------

    def cart_envelope(self, call=None):
        items = []
        naive_subtotal = 0.0
        for product_id, quantity in self.cart.items():
            product = self.products[product_id]
            unit = float(product["pricing"]["current_price"])
            line_total = self.line_total_overrides.get(product_id, unit * quantity)
            naive_subtotal += line_total
            items.append({
                "id": 5000 + product_id,
                "product_id": product_id,
                "quantity": quantity,
                "total_price": line_total,
                "product": product,
            })
        cart = {
            "cart_items": items,
            "subtotal": (
                self.subtotal_override
                if self.subtotal_override is not None
                else naive_subtotal
            ),
        }
        return {"data": {"cart": cart}}

    def _add_item(self, call):
        data = call.data or {}
        product_id = int(data["product_id"])
        # POST /cart/items is an INCREMENT on the real backend. Mirroring that
        # is the whole point: the bot's add-to-cart is only idempotent because
        # it reads the cart first.
        self.cart[product_id] = self.cart.get(product_id, 0) + int(data["quantity"])
        return self.cart_envelope()

    def _set_quantity(self, product_id, call):
        self.cart[product_id] = int((call.data or {})["quantity"])
        return self.cart_envelope()

    def _remove(self, product_id):
        self.cart.pop(product_id, None)
        return self.cart_envelope()

    def _clear(self, call):
        self.cart.clear()
        return self.cart_envelope()

    # -- payments / orders ----------------------------------------------------

    def _payment_methods(self, call):
        return {
            "data": {
                "available_methods": self.available_methods,
                "payment_restrictions": self.payment_restrictions,
            }
        }

    def _estimate(self, call):
        """`CartService.calculate_cart_estimate`'s payload shape, priced by the
        SERVER — the bot may only read these numbers, never compute them."""
        cart = self.cart_envelope()["data"]["cart"]
        subtotal = float(cart["subtotal"])
        pricing = {
            "items_subtotal": subtotal,
            "delivery_fee": 0.0,
            "promo_discount": 0.0,
            "discount_amount": 0.0,
            "loyalty_discount": 0.0,
            "tier_discount": 0.0,
            "tier_name": None,
            "tier_discount_percentage": 0.0,
            "cod_savings": 0.0,
            "payment_method": (call.data or {}).get("payment_method"),
            "total_discount": 0.0,
            "total_before_discount": subtotal,
            "final_total": subtotal,
        }
        pricing.update(self.estimate_pricing)
        return {
            "data": {
                "items": [
                    {
                        "product_id": item["product_id"],
                        "product_name": self.products[item["product_id"]]["name"],
                        "quantity": item["quantity"],
                        "unit_price": float(self.products[item["product_id"]]["pricing"]["current_price"]),
                        "subtotal": float(item["total_price"]),
                    }
                    for item in cart["cart_items"]
                ],
                "pricing": pricing,
            }
        }

    def _create_order(self, call):
        if self.order_response is not None:
            return self.order_response
        self.next_order_id += 1
        order = {
            "id": self.next_order_id,
            "order_number": f"BS-{self.next_order_id}",
            "total_amount": float(self.cart_envelope()["data"]["cart"]["subtotal"]),
            "status": "pending",
            "created_at": "2026-08-21T09:15:00+00:00",
            "order_items": [
                {"product_id": item["product_id"], "quantity": item["quantity"]}
                for item in self.cart_envelope()["data"]["cart"]["cart_items"]
            ],
        }
        self.orders.append(order)
        return {"data": {"order": order}}

    def _create_payment(self, call):
        order_id = (call.data or {}).get("order_id")
        return {
            "data": {
                "payment_link": {"payment_url": f"https://click.test/pay/{order_id}"}
            }
        }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def bot(monkeypatch):
    return await build_bot_harness(monkeypatch, translations=TRANSLATIONS)


@pytest.fixture
def shop(bot, monkeypatch):
    return Shop(bot.backend, monkeypatch)


def api_failure_with_body(error, status_code, body):
    """The APIResponse the REAL client builds for a 4xx/5xx that has a JSON
    body — `success=False` AND `data` set to the whole envelope."""
    from api_client import APIResponse

    return APIResponse(success=False, error=error, status_code=status_code, data=body)


@pytest.fixture
def user(bot):
    return bot.updates()


def user_data(bot):
    """The live `context.user_data` for our customer — the checkout flow's own
    memory between screens."""
    return bot.application.user_data[DEFAULT_USER_ID]


def add_address(bot, address_id, title, full_address, *, is_default=False):
    bot.backend.addresses[address_id] = {
        "id": address_id,
        "title": title,
        "full_address": full_address,
        "is_default": is_default,
        "latitude": 41.2876,
        "longitude": 69.2224,
    }


def backend_calls(bot, method, endpoint):
    return [
        call for call in bot.backend.calls
        if call.method == method and call.endpoint == endpoint
    ]


def order_payloads(bot):
    return [call.data for call in backend_calls(bot, "POST", "/api/v1/orders")]


def toasts(bot):
    """Every `answerCallbackQuery` that carried text — the bot's error channel.

    `BaseHandler._handle_api_error` shows backend failures as a TOAST, not a
    message, so a test that only reads `telegram.shown` cannot tell a rejected
    checkout from a silent one.
    """
    return [
        call.params["text"]
        for call in bot.telegram.of("answerCallbackQuery")
        if call.params.get("text")
    ]


def handlers_that_would_act(bot, update):
    """The handlers that would actually DO something with this update.

    `BotHarness.handlers_matching` cannot answer this question on its own:
    `bot.py` registers a PATTERN-LESS `CallbackQueryHandler(debug_callback_handler)`
    at group -1 that only logs and returns, so EVERY callback query matches at
    least one handler. Asserting on the raw list therefore passes no matter what
    the keyboards render — including for a `callback_data` nobody registered.
    Filtering the logger out is what lets "this button lands nowhere" be
    observed at all; `test_..._is_claimed_by_a_handler` below proves the filter
    can still say no.
    """
    return [
        (group, handler)
        for group, handler in bot.handlers_matching(update)
        if getattr(getattr(handler, "callback", None), "__name__", "")
        != "debug_callback_handler"
    ]


def assert_no_swallowed_crash(bot):
    """`BaseHandler._handle_error` catches EVERYTHING and answers a generic
    toast, so a handler that blew up mid-render still leaves a plausible-looking
    screen behind. Any happy-path journey must end with that toast absent."""
    assert "Error occurred" not in toasts(bot), (
        f"a handler raised and was swallowed; toasts so far: {toasts(bot)}"
    )


async def fill_cart(bot, user, product_id=BOTTLE_19L, quantity=3):
    """Get the customer to a cart holding `quantity` of `product_id`.

    Drives the real add-to-cart + quantity path rather than poking `shop.cart`,
    so anything downstream is reacting to a cart the BOT built.
    """
    await bot.send(user.tap(f"add_to_cart_{product_id}"))
    minimum = PRODUCTS[product_id]["inventory"]["min_order_quantity"]
    if quantity != minimum:
        await bot.send(user.tap(f"qty_set_{product_id}_{quantity}"))


async def reach_payment_picker(bot, user, address_id):
    """Cart -> checkout -> address chosen -> payment picker on screen."""
    await bot.send(user.tap("cart_view"))
    await bot.send(user.tap("cart_checkout"))
    await bot.send(user.tap(f"address_{address_id}"))


async def reach_confirmation(bot, user, address_id, payment="cash"):
    await reach_payment_picker(bot, user, address_id)
    await bot.send(user.tap(f"payment_{payment}"))


# ---------------------------------------------------------------------------
# Browsing
# ---------------------------------------------------------------------------


async def test_browsing_a_category_and_opening_a_product_shows_its_price_and_an_add_button(
    bot, shop, user
):
    """The three-tap path every first order starts with. If the category tap
    stops fetching that category — or the product card stops rendering the
    add button — the shop is unusable and no green single-handler test would
    say so, because each handler still "works" in isolation."""
    await bot.send(user.tap("menu_products"))

    categories_screen = bot.telegram.last_shown()
    assert categories_screen.callback_data() == [
        f"category_{WATER}", f"category_{HARDWARE}", "back_to_main",
    ]

    await bot.send(user.tap(f"category_{WATER}"))
    listing = bot.telegram.last_shown()

    # Only the water category, and the products it really contains.
    assert listing.callback_data() == [
        f"product_{BOTTLE_19L}", f"product_{BOTTLE_5L}", "back_to_categories",
    ]
    assert "Aqua Element 19L" in listing.text
    assert "Qopqoq nasosi" not in listing.text, "hardware leaked into the water category"

    (products_query,) = backend_calls(bot, "GET", "/api/v1/products")
    assert products_query.params == {"page": 1, "per_page": 6, "category_id": str(WATER)}

    await bot.send(user.tap(f"product_{BOTTLE_19L}"))
    card = bot.telegram.last_shown()
    assert card.callback_data() == [f"add_to_cart_{BOTTLE_19L}", f"category_{WATER}"]
    assert "15,000" in card.text, "the customer must see the price before adding"
    assert_no_swallowed_crash(bot)


async def test_a_one_category_shop_skips_the_picker_and_backs_out_to_the_main_menu(
    bot, user
):
    """With a single category the picker would be a one-button dead end, so
    products.py short-circuits past it. The Back button then HAS to point at
    the main menu: pointing it at the (skipped) category list strands the
    customer on an empty screen."""
    Shop(bot.backend, categories=[{"id": WATER, "name": "Suv"}])

    await bot.send(user.tap("menu_products"))

    listing = bot.telegram.last_shown()
    assert listing.callback_data() == [
        f"product_{BOTTLE_19L}", f"product_{BOTTLE_5L}", "back_to_main",
    ]
    assert not any(data.startswith("category_") for data in listing.callback_data())


# ---------------------------------------------------------------------------
# Cart
# ---------------------------------------------------------------------------


async def test_tapping_add_to_cart_twice_does_not_stack_two_minimum_quantities(
    bot, shop, user
):
    """`POST /cart/items` INCREMENTS server-side. A customer who taps Add,
    goes Back to the product and taps Add again used to silently pay for two
    minimum lots. The bot's guard is "read the cart first" — which lives in the
    seam between get_cart and add_to_cart, not inside either."""
    await bot.send(user.tap(f"add_to_cart_{BOTTLE_19L}"))
    await bot.send(user.tap(f"back_to_product_{BOTTLE_19L}"))
    await bot.send(user.tap(f"add_to_cart_{BOTTLE_19L}"))

    posted = [call.data for call in backend_calls(bot, "POST", "/api/v1/cart/items")]
    assert posted == [{"product_id": BOTTLE_19L, "quantity": 2}], (
        "the second Add must not post again; min_order_quantity is 2"
    )
    assert shop.cart == {BOTTLE_19L: 2}


async def test_the_quantity_buttons_set_the_line_and_a_double_tap_lands_on_the_same_number(
    bot, shop, user
):
    """Two things at once, because they are one decision: the +/- buttons must
    SET (`PUT`) rather than add, and each step is measured from what the CART
    holds. A fumbled double-tap is then absorbed by the dedup guard, and a
    deliberate second tap adds exactly the one bottle it says it adds."""
    await bot.send(user.tap(f"add_to_cart_{BOTTLE_19L}"))
    selector = bot.telegram.last_shown()
    assert f"qty_inc_{BOTTLE_19L}_2" in selector.callback_data()
    assert "Quantity: 2" in selector.text and "30,000" in selector.text

    await bot.send(user.tap(f"qty_inc_{BOTTLE_19L}_2"))
    assert shop.cart == {BOTTLE_19L: 3}

    # A fumbled double tap: the same stale button within the dedup window. The
    # guard is the FIRST defence and drops it before any handler runs.
    await bot.send(user.tap(f"qty_inc_{BOTTLE_19L}_2"))
    assert shop.cart == {BOTTLE_19L: 3}, "a double tap must not become +2"

    # The same button pressed again once the debounce has lapsed is not a
    # fumble — it is a customer asking for one more bottle, and the button's
    # own payload (2) is now stale. The step is measured from the SERVER cart,
    # so it lands on 4. Measuring from the payload instead would "converge" on
    # 3 — which reads as safe until the customer has picked 8 further up the
    # chat, and the same stale button silently rewrites it to 3.
    await deliberate_retap(bot, user.tap(f"qty_inc_{BOTTLE_19L}_2"))
    assert shop.cart == {BOTTLE_19L: 4}, "a deliberate +1 must add exactly one"

    await bot.send(user.tap(f"qty_set_{BOTTLE_19L}_12"))
    assert shop.cart == {BOTTLE_19L: 12}

    quantities = [
        call.data["quantity"]
        for call in bot.backend.calls
        if call.method == "PUT" and call.endpoint == f"/api/v1/cart/items/{BOTTLE_19L}"
    ]
    assert quantities == [3, 4, 12], "every quantity write must be absolute, never a delta"

    final = bot.telegram.last_shown()
    assert "Quantity: 12" in final.text
    assert "180,000" in final.text, "the running total must follow the quantity"


async def test_the_minus_button_never_drops_below_the_products_own_minimum(
    bot, shop, user
):
    """`min_order_quantity` is a backend rule the bot mirrors. If − walks the
    line below it the customer reaches a confirm screen the backend will
    reject, after choosing an address and a payment method."""
    await bot.send(user.tap(f"add_to_cart_{BOTTLE_19L}"))
    await bot.send(user.tap(f"qty_dec_{BOTTLE_19L}_2"))

    written = [
        call.data
        for call in bot.backend.calls
        if call.method == "PUT" and call.endpoint == f"/api/v1/cart/items/{BOTTLE_19L}"
    ]
    assert written == [{"quantity": 2}], "− must clamp to the minimum, not to 1"
    assert shop.cart == {BOTTLE_19L: 2}
    assert "Quantity: 2" in bot.telegram.last_shown().text


async def test_the_cart_screen_shows_the_servers_subtotal_not_a_client_side_sum(
    bot, shop, user
):
    """A line the server drops or reprices (inactive product, contract price)
    keeps its raw `current_price` in the payload. The screen must read
    `cart.subtotal` — the same figure the order is built from — or the
    customer is quoted a number the order will never contain, and the
    minimum-order gate is decided against a fiction."""
    await fill_cart(bot, user, BOTTLE_19L, quantity=4)

    # Server says this cart is worth 41,000 — naive 4 x 15,000 would be 60,000.
    shop.line_total_overrides[BOTTLE_19L] = 41000.0
    shop.subtotal_override = 41000.0

    await bot.send(user.tap("cart_view"))
    cart_screen = bot.telegram.last_shown()

    assert "41,000" in cart_screen.text
    assert "60,000" not in cart_screen.text, (
        "the cart re-multiplied price x quantity instead of reading the "
        "server's subtotal"
    )
    assert "cart_checkout" in cart_screen.callback_data()


async def test_a_cart_below_the_minimum_offers_no_checkout_button_and_says_how_short_it_is(
    bot, shop, user
):
    """The gate and the copy are ONE expression (`min_order_shortfall`). If they
    drift, the bot either lets an order through that the backend rejects or
    tells the customer to add the wrong amount."""
    await fill_cart(bot, user, BOTTLE_5L, quantity=1)  # 6,000 UZS

    await bot.send(user.tap("cart_view"))
    cart_screen = bot.telegram.last_shown()

    assert "cart_checkout" not in cart_screen.callback_data(), (
        "checkout must be unreachable below the minimum order amount"
    )
    shortfall = min_order_shortfall(6000)
    assert f"Add {shortfall:,.0f} UZS more" in cart_screen.text
    assert f"Minimum {MIN_ORDER_AMOUNT:,.0f} UZS" in cart_screen.text


async def test_an_empty_cart_screen_offers_shopping_rather_than_checkout(bot, shop, user):
    """Emptying the cart must leave a way forward. An empty cart that still
    renders Checkout sends the customer into a flow that dead-ends three
    screens later."""
    await fill_cart(bot, user, BOTTLE_19L, quantity=3)
    await bot.send(user.tap("cart_view"))
    await bot.send(user.tap("cart_clear"))

    assert shop.cart == {}
    empty_screen = bot.telegram.last_shown()
    assert empty_screen.callback_data() == ["menu_products", "back_to_main"]
    assert empty_screen.text == "Cart empty"


# ---------------------------------------------------------------------------
# Checkout — addresses
# ---------------------------------------------------------------------------


async def test_checkout_with_no_saved_address_arms_the_location_keyboard_and_survives_the_pin(
    bot, shop, user
):
    """The zero-address path is the one that loses customers: the bot has to
    ask for a pin from the checkout message itself, the pin has to be a
    conversation ENTRY POINT (or it is filed as a support ticket), and the
    address it creates has to be the one the very next checkout offers."""
    await fill_cart(bot, user, BOTTLE_19L, quantity=3)
    await bot.send(user.tap("cart_view"))
    await bot.send(user.tap("cart_checkout"))

    prompt = bot.telegram.last_shown()
    assert prompt.text == "No address prompt"
    assert prompt.button_labels() == [
        "Share location button", "Enter manually button", "Cancel",
    ], "the customer must be able to share, type, or back out — in one message"
    assert user_data(bot)["address_flow_origin"] == "checkout", (
        "without this the saved address dumps the customer on the main menu "
        "holding a full cart"
    )
    assert order_payloads(bot) == [], "no address means no order yet"

    await bot.send(user.location(41.32354, 69.241036))
    await bot.send(user.tap("addr_title_home"))

    (saved,) = bot.backend.addresses.values()
    assert saved["latitude"] == 41.32354

    support_posts = backend_calls(bot, "POST", "/api/v1/support/messages")
    assert support_posts == [], "a shared pin is a delivery address, not a support ticket"

    # Back to checkout: the address the customer just dropped must be offered.
    # A deliberate re-tap — the identical `cart_checkout` above is minutes of
    # address-entry away, well past the debounce window.
    await deliberate_retap(bot, user.tap("cart_checkout"))
    confirmation = bot.telegram.last_shown()
    assert confirmation.callback_data() == [
        f"address_{saved['id']}", "add_new_address_checkout", "back_to_cart",
    ]
    assert_no_swallowed_crash(bot)


async def test_checkout_with_several_addresses_lists_them_all_and_carries_the_pick_to_the_order(
    bot, shop, user
):
    """The picked address travels three screens in `user_data` before it is
    posted. Losing it there is invisible on screen — the order simply ships to
    the wrong door."""
    add_address(bot, 900, "Uy", "Chilonzor 15", is_default=True)
    add_address(bot, 901, "Ish", "Amir Temur 45")
    add_address(bot, 902, "Dala hovli", "Yangiobod 3")

    await fill_cart(bot, user, BOTTLE_19L, quantity=3)
    await bot.send(user.tap("cart_view"))
    await bot.send(user.tap("cart_checkout"))

    picker = bot.telegram.last_shown()
    assert picker.callback_data() == [
        "address_900", "address_901", "address_902",
        "add_new_address_checkout", "back_to_cart",
    ]
    assert picker.text == "Select address"

    await bot.send(user.tap("address_901"))
    (methods_query,) = backend_calls(bot, "GET", "/api/v1/payments/methods")
    assert methods_query.params == {"context": "order", "delivery_address_id": 901}, (
        "the address must reach /payments/methods or the COD place-cap arm "
        "cannot be evaluated and Cash is offered where it will be refused"
    )

    await bot.send(user.tap("payment_cash"))
    assert "Ish" in bot.telegram.last_shown().text

    await bot.send(user.tap("confirm_order"))
    assert order_payloads(bot) == [{
        "delivery_address_id": 901,
        "payment_method": "cash",
        "source": "telegram",
        "items": [{"product_id": BOTTLE_19L, "quantity": 3}],
    }]
    assert_no_swallowed_crash(bot)


# ---------------------------------------------------------------------------
# Checkout — payment methods
# ---------------------------------------------------------------------------


async def test_the_payment_picker_offers_exactly_the_rails_the_backend_enabled(
    bot, shop, user
):
    """`build_payment_method_buttons` renames the `click` rail to "Card" for
    customers and drops anything the backend did not enable. A button for a
    disabled rail is an order the backend will refuse after the customer has
    already committed."""
    add_address(bot, 900, "Uy", "Chilonzor 15")
    shop.available_methods = [
        {"method": "cash", "is_active": True},
        {"method": "click", "is_active": True},
        {"method": "business_account", "is_active": False},
        {"method": "payme", "is_active": True},
    ]

    await fill_cart(bot, user, BOTTLE_19L, quantity=3)
    await reach_payment_picker(bot, user, 900)

    picker = bot.telegram.last_shown()
    assert picker.callback_data() == ["payment_cash", "payment_card", "back_to_delivery"]
    assert picker.button_labels() == ["💰 Naqd pul", "💳 Karta", "Back"], (
        "each button must carry exactly ONE icon — the translation's own, "
        "never a second one the keyboard used to prepend"
    )
    # Owner screenshot review: the message body stays NEUTRAL now — basket
    # plus the plain, undiscounted total, no rail assumed.
    assert picker.text.startswith("To'lov usulini tanlang")
    assert "Total: 45,000 UZS" in picker.text, picker.text


async def test_a_cod_cap_explains_itself_without_leaking_an_unseeded_key_as_english(
    bot, shop, user
):
    """When the COD cap hides Cash the customer must be told why — in their own
    language. `telegram.orders.cod_restricted_place` is newer than the base
    seed, so in any environment whose seed script has not run it renders as the
    literal English debug text "Cod restricted place". orders.py deliberately
    RENDER-checks that key and degrades to the older, always-seeded one; if that
    check is ever replaced with a naive lookup, paying customers see raw English
    at the moment they choose how to pay."""
    add_address(bot, 900, "Uy", "Chilonzor 15")
    shop.available_methods = [{"method": "click", "is_active": True}]
    shop.payment_restrictions = {
        "cod_restricted": True,
        "restriction_scope": "place",
        "place_active_cod_debt_count": 2,
        "active_cod_debt_count": 3,
    }

    await fill_cart(bot, user, BOTTLE_19L, quantity=3)
    await reach_payment_picker(bot, user, 900)

    picker = bot.telegram.last_shown()
    assert "payment_cash" not in picker.callback_data(), (
        "Cash must disappear exactly when order creation would refuse it"
    )
    assert "Cod restricted place" not in picker.text, (
        "an unseeded key leaked to the customer as English debug text"
    )
    assert picker.text.endswith("Cash is unavailable: 3 unpaid orders")


async def test_the_cash_buttons_discount_suffix_comes_off_the_neutral_screens_own_quote(
    bot, shop, user
):
    """Owner screenshot review: the discount now lives on the CASH BUTTON, not
    the message body. The body must stay neutral — never quoting a discounted
    total before the rail is even chosen — while the button names the percent
    that makes cash cheaper, read off the SAME quote the neutral total used."""
    add_address(bot, 900, "Uy", "Chilonzor 15")
    shop.estimate_pricing = {
        "tier_discount": 1800.0,
        "tier_name": "Nimbus",
        "tier_discount_percentage": 4.0,
        "final_total": 43200.0,
    }

    await fill_cart(bot, user, BOTTLE_19L, quantity=3)
    await reach_payment_picker(bot, user, 900)

    picker = bot.telegram.last_shown()
    assert "Nimbus" not in picker.text, picker.text
    assert "To pay" not in picker.text, picker.text
    assert "Total: 45,000 UZS" in picker.text, (
        "the body must show the UNDISCOUNTED basket total, never the "
        f"cash-discounted 43,200: {picker.text!r}"
    )
    assert "💰 Naqd pul −4% 🏷" in picker.button_labels(), picker.button_labels()
    assert "payment_cash" in picker.callback_data()
    assert_no_swallowed_crash(bot)


async def test_the_cash_buttons_discount_suffix_renders_a_fractional_rate(
    bot, shop, user
):
    """The percentage is never assumed to be a whole number — production
    tiers are admin-configured and can land on a fractional rate."""
    add_address(bot, 900, "Uy", "Chilonzor 15")
    shop.estimate_pricing = {
        "tier_discount": 900.0,
        "tier_name": "Nimbus",
        "tier_discount_percentage": 2.5,
        "final_total": 44100.0,
    }

    await fill_cart(bot, user, BOTTLE_19L, quantity=3)
    await reach_payment_picker(bot, user, 900)

    picker = bot.telegram.last_shown()
    assert "💰 Naqd pul −2.5% 🏷" in picker.button_labels(), picker.button_labels()


async def test_the_cash_button_carries_no_suffix_when_the_tier_discount_is_zero(
    bot, shop, user
):
    """Change 3 pin (surface: bot payment-picker button suffix,
    telegram_bot/handlers/orders.py ~1117). A customer whose tier carries no
    discount — a 0% tier, or a loyalty-ineligible entity — reaches this
    screen with `tier_discount` and `tier_discount_percentage` already zeroed
    by `LoyaltyService.quote_tier_discount` (both shapes are proven to zero
    it in tests/integration/test_tier_discount_order_creation.py's
    test_zero_percent_tier_grants_nothing and
    test_ineligible_entity_gets_nothing_even_on_cash — this bot cannot see
    WHY the number is zero, only that it is). The cash button must render
    exactly its plain label: no "−", no "%", no "🏷", no stray trailing
    space from an empty suffix."""
    add_address(bot, 900, "Uy", "Chilonzor 15")
    shop.estimate_pricing = {
        "tier_discount": 0.0,
        "tier_name": None,
        "tier_discount_percentage": 0.0,
        "final_total": 45000.0,
    }

    await fill_cart(bot, user, BOTTLE_19L, quantity=3)
    await reach_payment_picker(bot, user, 900)

    picker = bot.telegram.last_shown()
    assert "💰 Naqd pul" in picker.button_labels(), picker.button_labels()
    cash_label = next(label for label in picker.button_labels() if "Naqd" in label)
    assert cash_label == "💰 Naqd pul", cash_label
    assert_no_swallowed_crash(bot)


async def test_the_cash_confirm_screen_shows_no_tier_line_when_the_tier_discount_is_zero(
    bot, shop, user
):
    """Change 3 pin (surface: bot confirmation tier line,
    telegram_bot/handlers/orders.py ~277, `_build_estimate_block`). Same
    zeroed shape as the button-suffix pin above, but on the CASH rail's own
    confirmation screen — the one screen where a tier line WOULD render if
    the gate were missing. `telegram.orders.estimate_tier_line` is the only
    seeded copy containing the word "discount" in this fake translation
    table, so its absence is a direct proxy for the line never having
    rendered."""
    add_address(bot, 900, "Uy", "Chilonzor 15")
    shop.estimate_pricing = {
        "tier_discount": 0.0,
        "tier_name": None,
        "tier_discount_percentage": 0.0,
        "final_total": 45000.0,
    }

    await fill_cart(bot, user, BOTTLE_19L, quantity=3)
    await reach_confirmation(bot, user, 900, payment="cash")

    screen = bot.telegram.last_shown()
    assert "discount" not in screen.text.lower(), screen.text
    assert_no_swallowed_crash(bot)


async def test_the_picker_quotes_no_cash_price_when_cash_is_not_on_offer(
    bot, shop, user
):
    """Quoting a rail the COD cap has already removed is a promise the write
    path will refuse. When Cash is gone the block goes with it."""
    add_address(bot, 900, "Uy", "Chilonzor 15")
    shop.available_methods = [{"method": "click", "is_active": True}]
    shop.payment_restrictions = {"cod_restricted": True, "active_cod_debt_count": 3}
    shop.estimate_pricing = {
        "tier_discount": 1800.0, "tier_name": "Nimbus",
        "tier_discount_percentage": 4.0, "final_total": 43200.0,
    }

    await fill_cart(bot, user, BOTTLE_19L, quantity=3)
    await reach_payment_picker(bot, user, 900)

    picker = bot.telegram.last_shown()
    assert "payment_cash" not in picker.callback_data()
    assert "Nimbus" not in picker.text, (
        "a cash price was quoted to a customer who cannot choose cash"
    )
    assert "Total:" not in picker.text, picker.text


async def test_the_card_confirm_screen_no_longer_pitches_the_cash_saving(
    bot, shop, user
):
    """Owner screenshot review: the card confirm screen used to ALSO name what
    cash would have saved (`cod_savings`). That pitch now lives on the
    picker's cash button, a screen earlier, where the choice is still open —
    not here, after it is already made."""
    add_address(bot, 900, "Uy", "Chilonzor 15")
    shop.estimate_pricing = {
        "tier_discount": 0.0,
        "tier_name": "Nimbus",
        "tier_discount_percentage": 4.0,
        "cod_savings": 1800.0,
        "final_total": 45000.0,
    }

    await fill_cart(bot, user, BOTTLE_19L, quantity=3)
    await reach_confirmation(bot, user, 900, payment="card")

    screen = bot.telegram.last_shown()
    assert "💳 To pay: 45,000 UZS" in screen.text, screen.text
    assert "Pay cash and save" not in screen.text, screen.text
    assert "Nimbus discount" not in screen.text, (
        "a fiscalized rail earns no discount; showing the line would be a lie"
    )
    assert_no_swallowed_crash(bot)


@pytest.mark.parametrize("tapped, posted_method", [
    ("cash", "cash"),
    ("card", "card"),
    ("business_account", "business_account"),
])
async def test_each_offered_payment_method_posts_itself_verbatim_on_the_order(
    bot, shop, user, tapped, posted_method
):
    """The button the customer taps and the `payment_method` string the order
    carries are the same decision. A rename on either side settles the money on
    the wrong rail — this project has already shipped a COD/Click mismatch."""
    add_address(bot, 900, "Uy", "Chilonzor 15")
    await fill_cart(bot, user, BOTTLE_19L, quantity=3)
    await reach_confirmation(bot, user, 900, payment=tapped)

    await bot.send(user.tap("confirm_order"))

    assert order_payloads(bot) == [{
        "delivery_address_id": 900,
        "payment_method": posted_method,
        "source": "telegram",
        "items": [{"product_id": BOTTLE_19L, "quantity": 3}],
    }]


async def test_a_cash_order_clears_the_cart_and_shows_the_order_number(bot, shop, user):
    """Cash is settled at the door, so the order is final the moment it is
    created — the cart has to go, or the customer's next visit re-orders what
    they just bought."""
    add_address(bot, 900, "Uy", "Chilonzor 15")
    await fill_cart(bot, user, BOTTLE_19L, quantity=3)
    await reach_confirmation(bot, user, 900, payment="cash")

    await bot.send(user.tap("confirm_order"))

    assert shop.cart == {}, "a placed cash order must empty the cart"
    success = bot.telegram.last_shown()
    assert "Placed success" in success.text
    assert f"Order BS-{shop.next_order_id}" in success.text
    assert "Total 45,000 UZS" in success.text
    assert "Cash note" in success.text, "the customer must be told to have cash ready"
    assert_no_swallowed_crash(bot)


async def test_a_card_order_shows_a_payment_link_and_keeps_the_cart_until_it_is_paid(
    bot, shop, user
):
    """Clearing the cart before the PSP confirms would leave a customer who
    abandons the payment page with neither an order nor a cart. The link itself
    must name the order and the amount, because that is all the customer sees
    before handing over card details."""
    add_address(bot, 900, "Uy", "Chilonzor 15")
    await fill_cart(bot, user, BOTTLE_19L, quantity=3)
    await reach_confirmation(bot, user, 900, payment="card")

    await bot.send(user.tap("confirm_order"))

    order_id = shop.orders[-1]["id"]
    (payment_request,) = backend_calls(bot, "POST", "/api/v1/payments/create")
    assert payment_request.data["order_id"] == order_id
    assert payment_request.data["payment_method"] == "click", (
        "customers see 'Card'; the provider is Click — the rail must not be "
        "renamed on the way to the backend"
    )

    link_screen = bot.telegram.last_shown()
    assert link_screen.text == f"Pay order BS-{order_id}: 45,000 UZS"
    urls = [
        button["url"]
        for row in link_screen.reply_markup["inline_keyboard"]
        for button in row
        if "url" in button
    ]
    assert urls == [f"https://click.test/pay/{order_id}"]

    assert backend_calls(bot, "POST", "/api/v1/cart/clear") == [], (
        "an unpaid card order must not empty the cart"
    )
    assert shop.cart == {BOTTLE_19L: 3}
    assert_no_swallowed_crash(bot)


# ---------------------------------------------------------------------------
# Checkout — the ways it goes wrong
# ---------------------------------------------------------------------------


async def test_a_backend_rejection_of_order_creation_keeps_the_cart_and_says_why(
    bot, shop, user
):
    """A 400 from `POST /orders` (out of stock, COD cap, closed shop) must
    leave the customer able to try again: cart intact, reason on screen. A
    silent failure here reads as a bot that swallowed an order."""
    add_address(bot, 900, "Uy", "Chilonzor 15")
    await fill_cart(bot, user, BOTTLE_19L, quantity=3)
    await reach_confirmation(bot, user, 900, payment="cash")

    shop.order_response = backend_failure("Not enough stock for Aqua Element 19L", 400)
    await bot.send(user.tap("confirm_order"))

    assert shop.orders == []
    assert shop.cart == {BOTTLE_19L: 3}, "a refused order must not empty the cart"
    assert "❌ Not enough stock for Aqua Element 19L" in toasts(bot)

    # And the customer can retry once the backend recovers — the confirmation
    # screen's Confirm button is still live.
    shop.order_response = None
    await deliberate_retap(bot, user.tap("confirm_order"))
    assert len(order_payloads(bot)) == 2
    assert shop.cart == {}


async def test_a_tax_committee_outage_offers_a_cash_rescue_that_revives_the_same_order(
    bot, shop, user
):
    """A 503 from Asl belgisi cancels the order server-side and hands back its
    id. The rescue button must revive THAT order rather than rebuild one from
    the cart — rebuilding re-runs the COD debt cap and can strand a customer
    whose card order was already cancelled."""
    add_address(bot, 900, "Uy", "Chilonzor 15")
    await fill_cart(bot, user, BOTTLE_19L, quantity=3)
    await reach_confirmation(bot, user, 900, payment="card")

    # The real 503 shape: an error AND a body carrying the cancelled order id.
    shop.raw_responses[("POST", "/api/v1/orders")] = api_failure_with_body(
        "Asl belgisi unavailable", 503,
        {"data": {"cancelled_order_id": 7101}},
    )
    await bot.send(user.tap("confirm_order"))

    outage = bot.telegram.last_shown()
    assert outage.text == "Asl belgisi error message"
    assert outage.callback_data() == ["select_payment_cash", "confirm_order"]
    assert user_data(bot)["psp_failed_order_id"] == 7101, (
        "the cancelled order id must be read off the 503 body; without it the "
        "rescue button silently degrades to a COD-capped rebuild from cart"
    )

    bot.backend.route(
        "POST", "/api/v1/orders/7101/retry-with-cash",
        lambda call: {"data": {"order": {
            "id": 7101, "order_number": "BS-7101", "total_amount": 45000,
            "status": "pending", "created_at": "2026-08-21T09:15:00+00:00",
        }}},
    )

    await bot.send(user.tap("select_payment_cash"))

    assert backend_calls(bot, "POST", "/api/v1/orders/7101/retry-with-cash"), (
        "the rescue must reuse the cancelled order, not create a new one"
    )
    assert len(order_payloads(bot)) == 1, "the rescue must not post a second order"
    rescued = bot.telegram.last_shown()
    assert "Placed success" in rescued.text
    assert "Order BS-7101" in rescued.text
    assert "Cash note" in rescued.text


async def test_a_failed_payment_link_leaves_one_order_and_a_retry_that_reuses_it(
    bot, shop, user
):
    """A PSP that cannot mint a link must not cost the customer a second order.

    By the time `send_payment_link` runs, `POST /api/v1/orders` has already
    returned 2xx — the order EXISTS. The card rail nonetheless left
    `selected_address_id` and `selected_payment_method` sitting in `user_data`
    while it went to the PSP, and on failure showed the generic
    `telegram.payment.failed_message`: an error message with the Confirm button
    still armed. The customer's only reasonable reading of that is "it did not
    go through", and their next tap bought the same basket a SECOND time. This
    is the wave-1 cash-rail defect on the rail that takes card money.

    Four things have to hold, and none of them is visible to a handler-level
    test: exactly one order exists, the screen SAYS the order was placed and
    NAMES it, Confirm is disarmed, and the retry pays THAT order. The retry
    carries the id in its `callback_data` (`payment_retry_<id>`) rather than
    leaning on `user_data`, which is not persisted — the same reason the
    cancel-order confirmation was rewired in wave 2.
    """
    add_address(bot, 900, "Uy", "Chilonzor 15")
    await fill_cart(bot, user, BOTTLE_19L, quantity=3)
    await reach_confirmation(bot, user, 900, payment="card")

    # The PSP is down. Keep the working responder so the retry below can find a
    # recovered gateway — a retry proven only against a still-broken PSP proves
    # nothing about which order it would have paid.
    psp = bot.backend.routes[("POST", "/api/v1/payments/create")]
    bot.backend.route(
        "POST", "/api/v1/payments/create",
        lambda call: backend_failure("Click gateway timeout", 502),
    )

    await bot.send(user.tap("confirm_order"))

    (order,) = shop.orders
    order_id = order["id"]

    failure = bot.telegram.last_shown()
    assert failure.text == (
        f"Order BS-{order_id} is placed. We could not create the payment link."
    ), (
        "the customer must be told the order stands, and be given its number — "
        "an unnamed order is one they cannot look up or talk to support about"
    )
    assert "Failed message" not in failure.text, (
        "the generic payment-failure copy says nothing about the order having "
        "been placed, which is the fact that stops a second order"
    )

    # Retry pays this order; My Orders lets them go look at it.
    #
    # Switch method is deliberately GONE (B3 fix round 1): its callbacks carried
    # no order id and routed to the CART confirmation screen, so — with the cart
    # still holding this basket — it led to a SECOND order for the same items,
    # the exact outcome the copy above exists to prevent. Retry is the rail move
    # a customer actually has (POST /payments/create normalizes card->click and
    # flips a pending cash order behind the pool guard).
    assert failure.callback_data() == [
        f"payment_retry_{order_id}",
        "menu_orders",
        f"cancel_order_{order_id}",
    ]

    # The confirmation screen is still scrollable-to and its Confirm button
    # still looks live. This is the tap that used to place order number two.
    await deliberate_retap(bot, user.tap("confirm_order"))
    assert shop.orders == [order], "a still-armed Confirm placed a second order"
    assert len(order_payloads(bot)) == 1
    assert shop.cart == {BOTTLE_19L: 3}, "an unpaid card order must keep the cart"

    # Gateway recovers; the customer taps Retry payment.
    bot.backend.route(
        "GET", f"/api/v1/orders/{order_id}", lambda call: {"data": {"order": order}}
    )
    bot.backend.route("POST", "/api/v1/payments/create", psp)

    await bot.send(user.tap(f"payment_retry_{order_id}"))

    assert shop.orders == [order], "Retry must pay the existing order, not create one"
    assert len(order_payloads(bot)) == 1
    assert backend_calls(bot, "POST", "/api/v1/payments/create")[-1].data["order_id"] == order_id
    link = bot.telegram.last_shown()
    assert link.text == f"Pay order BS-{order_id}: 45,000 UZS"
    assert_no_swallowed_crash(bot)

async def test_a_refused_edit_on_a_failed_payment_link_still_leaves_one_screen(
    bot, shop, user
):
    """One failure must produce ONE screen, and it must be the reassuring one.

    `send_payment_link` used to draw its own generic failure screen (an
    `_send_error_message` "could not create the payment link") immediately
    before returning False, and `confirm_order` then drew the dedicated "order
    placed, link failed" screen over the top. While Telegram accepts the first
    edit the second one overwrites it and the duplication is invisible.

    It is not invisible when Telegram REFUSES the edit — a bubble the customer
    deleted, or one too old to edit, both of which are in this project's
    production logs. `_edit_or_replace_callback_message` then DELETES the
    bubble and posts a replacement, so the second edit targets a message that
    no longer exists and posts a replacement of its own: two messages for one
    failure, and the first of them says the payment failed without ever saying
    the order stands. That is the exact reading that makes a customer order the
    same basket again.

    So the rule "what does a link failure look like" belongs in ONE place — the
    caller that knows the order exists.
    """
    add_address(bot, 900, "Uy", "Chilonzor 15")
    await fill_cart(bot, user, BOTTLE_19L, quantity=3)
    await reach_confirmation(bot, user, 900, payment="card")

    bot.backend.route(
        "POST", "/api/v1/payments/create",
        lambda call: backend_failure("Click gateway timeout", 502),
    )
    # Telegram refuses every edit from here on: the confirmation bubble is
    # gone. Every render after this point goes through the delete+replace
    # fallback, which is what makes a second renderer visible.
    bot.telegram.fail("editMessageText", "Message to edit not found", 400)

    before = len(bot.telegram.calls)
    await bot.send(user.tap("confirm_order"))

    (order,) = shop.orders
    delivered = [
        call for call in bot.telegram.calls[before:] if call.method == "sendMessage"
    ]
    assert [call.text for call in delivered] == [
        f"Order BS-{order['id']} is placed. We could not create the payment link."
    ], (
        "one failed payment link must leave the customer exactly one message, "
        "and it must be the one saying the order was placed"
    )
    assert delivered[0].callback_data() == [
        f"payment_retry_{order['id']}",
        "menu_orders",
        f"cancel_order_{order['id']}",
    ], "the surviving screen must still carry the recovery keyboard"
    assert_no_swallowed_crash(bot)


async def test_a_retry_that_the_psp_refuses_again_keeps_the_retry_loop_on_screen(
    bot, shop, user
):
    """The retry path is the OTHER caller of `send_payment_link`.

    Moving the failure screen out of `send_payment_link` must not leave this
    call site mute — a retry that silently changes nothing on screen is
    indistinguishable from a tap that never registered. The screen it draws is
    the same "the order stands, here is Retry" one `confirm_order` draws,
    because it is the same fact, and it must keep the retry button so the loop
    can be run again when the gateway comes back.
    """
    add_address(bot, 900, "Uy", "Chilonzor 15")
    await fill_cart(bot, user, BOTTLE_19L, quantity=3)
    await reach_confirmation(bot, user, 900, payment="card")

    bot.backend.route(
        "POST", "/api/v1/payments/create",
        lambda call: backend_failure("Click gateway timeout", 502),
    )
    await bot.send(user.tap("confirm_order"))

    (order,) = shop.orders
    order_id = order["id"]
    bot.backend.route(
        "GET", f"/api/v1/orders/{order_id}", lambda call: {"data": {"order": order}}
    )

    await bot.send(user.tap(f"payment_retry_{order_id}"))

    still_failing = bot.telegram.last_shown()
    assert still_failing.text == (
        f"Order BS-{order_id} is placed. We could not create the payment link."
    ), "a refused retry must say what happened, not leave the old screen standing"
    assert f"payment_retry_{order_id}" in still_failing.callback_data(), (
        "a retry that cannot be retried again is a dead end"
    )
    assert shop.orders == [order], "a retry must never create a second order"
    assert_no_swallowed_crash(bot)


async def test_a_retry_on_a_cash_order_refused_for_a_short_marking_code_pool_shows_the_cash_stays_message(
    bot, shop, user
):
    """N-2(a): the backend's MARKING_CODES_POOL_SHORT refusal must reach the
    customer as words, and ONLY as the cash-stays copy when the order's rail
    really is cash -- `retry_payment`'s `was_cash` gate (I-1), pinned end to
    end through the real dispatcher with the exact wire literal both
    processes (this backend-shaped body, and the bot's error_code check) have
    to agree on. Nothing under tests/ pinned that literal before this test;
    a one-sided rename of it would previously degrade to the generic screen
    with no test going red.
    """
    add_address(bot, 900, "Uy", "Chilonzor 15")
    await fill_cart(bot, user, BOTTLE_19L, quantity=3)
    await reach_confirmation(bot, user, 900, payment="card")

    shop.raw_responses[("POST", "/api/v1/payments/create")] = api_failure_with_body(
        "Marking codes unavailable", 400,
        {"data": {"error_code": "MARKING_CODES_POOL_SHORT"}},
    )
    await bot.send(user.tap("confirm_order"))

    (order,) = shop.orders
    order_id = order["id"]
    # By retry time the order's rail reads CASH -- e.g. an earlier attempt
    # already refused the flip and left it there.
    cash_order = {**order, "payment_method": "cash"}
    bot.backend.route(
        "GET", f"/api/v1/orders/{order_id}", lambda call: {"data": {"order": cash_order}}
    )

    await bot.send(user.tap(f"payment_retry_{order_id}"))

    shown = bot.telegram.last_shown()
    assert shown.text == "Your order stays on Cash on Delivery.", (
        "a cash order refused for a short pool must show the cash-stays "
        "copy, not the generic link-failed screen"
    )
    assert shop.orders == [order], "a refused retry must never create a second order"
    assert_no_swallowed_crash(bot)


async def test_a_retry_on_a_click_order_refused_for_a_short_marking_code_pool_falls_through_to_the_generic_screen(
    bot, shop, user
):
    """N-2(b): the I-1 regression pin. The SAME refusal body on an order that
    is NOT on cash must not claim it is -- a money-facing false statement
    that sets a COD expectation the driver's rail will not match. It falls
    through to the ordinary "order stands, here is Retry" screen instead,
    same as any other payment-link failure.
    """
    add_address(bot, 900, "Uy", "Chilonzor 15")
    await fill_cart(bot, user, BOTTLE_19L, quantity=3)
    await reach_confirmation(bot, user, 900, payment="card")

    shop.raw_responses[("POST", "/api/v1/payments/create")] = api_failure_with_body(
        "Marking codes unavailable", 400,
        {"data": {"error_code": "MARKING_CODES_POOL_SHORT"}},
    )
    await bot.send(user.tap("confirm_order"))

    (order,) = shop.orders
    order_id = order["id"]
    click_order = {**order, "payment_method": "click"}
    bot.backend.route(
        "GET", f"/api/v1/orders/{order_id}", lambda call: {"data": {"order": click_order}}
    )

    await bot.send(user.tap(f"payment_retry_{order_id}"))

    shown = bot.telegram.last_shown()
    assert shown.text == (
        f"Order BS-{order_id} is placed. We could not create the payment link."
    ), "an order not on cash must fall through to the generic retry screen"
    assert "Cash on Delivery" not in shown.text, (
        "must never tell the customer they are on cash when they are not"
    )
    assert shop.orders == [order]
    assert_no_swallowed_crash(bot)



async def test_an_empty_cart_at_the_confirm_tap_never_reaches_order_creation(
    bot, shop, user
):
    """The confirmation screen is a snapshot. A customer who clears the cart in
    a second chat window (or whose items were removed server-side) still has a
    live Confirm button. Posting it would create an order with no lines."""
    add_address(bot, 900, "Uy", "Chilonzor 15")
    await fill_cart(bot, user, BOTTLE_19L, quantity=3)
    await reach_confirmation(bot, user, 900, payment="cash")

    shop.cart.clear()
    await bot.send(user.tap("confirm_order"))

    assert order_payloads(bot) == [], "an empty cart must never become an order"
    assert "❌ Cart empty" in toasts(bot)


async def test_cancelling_at_the_confirmation_screen_keeps_the_cart_and_disarms_confirm(
    bot, shop, user
):
    """Cancel means "not now", not "throw away my cart". It must also forget the
    address and payment choice, so the stale Confirm button left on screen (or
    scrolled back to) cannot place the order the customer just cancelled."""
    add_address(bot, 900, "Uy", "Chilonzor 15")
    await fill_cart(bot, user, BOTTLE_19L, quantity=3)
    await reach_confirmation(bot, user, 900, payment="cash")

    await bot.send(user.tap("cancel_order"))

    assert bot.telegram.last_shown().text == "Action cancelled"
    assert shop.cart == {BOTTLE_19L: 3}, "cancelling checkout must not empty the cart"
    assert "selected_address_id" not in user_data(bot)
    assert "selected_payment_method" not in user_data(bot)

    # The stale Confirm button from the cancelled screen.
    await bot.send(user.tap("confirm_order"))
    assert order_payloads(bot) == [], "a cancelled checkout must not be confirmable"
    assert "Missing info" in toasts(bot)


async def test_editing_the_cart_from_the_confirmation_screen_returns_with_the_new_total(
    bot, shop, user
):
    """Edit -> change quantity -> Done is a round trip through THREE modules
    (`orders.edit_cart` -> `products.show_cart(edit_mode)` ->
    `orders.back_to_order_confirm`). The address and payment picked before the
    detour have to survive it, and the confirmation must re-read the cart —
    otherwise the customer confirms the total they had before editing."""
    add_address(bot, 900, "Uy", "Chilonzor 15")
    await fill_cart(bot, user, BOTTLE_19L, quantity=3)
    await reach_confirmation(bot, user, 900, payment="cash")
    # The confirm screen's money now comes from the quote block's payable
    # line, not a separately-rendered "Grand total".
    assert "💰 To pay: 45,000 UZS" in bot.telegram.last_shown().text

    await bot.send(user.tap("edit_order"))
    edit_screen = bot.telegram.last_shown()
    assert f"cart_inc_{BOTTLE_19L}" in edit_screen.callback_data()
    assert "back_to_order_confirm" in edit_screen.callback_data(), (
        "Done must return to confirmation, not to the plain cart"
    )

    await bot.send(user.tap(f"cart_inc_{BOTTLE_19L}"))
    assert shop.cart == {BOTTLE_19L: 4}

    await bot.send(user.tap("back_to_order_confirm"))
    confirmation = bot.telegram.last_shown()
    assert "💰 To pay: 60,000 UZS" in confirmation.text
    assert "Uy" in confirmation.text, "the address chosen before the detour was lost"

    await bot.send(user.tap("confirm_order"))
    assert order_payloads(bot) == [{
        "delivery_address_id": 900,
        "payment_method": "cash",
        "source": "telegram",
        "items": [{"product_id": BOTTLE_19L, "quantity": 4}],
    }]
    assert_no_swallowed_crash(bot)


async def test_removing_the_last_line_while_editing_leaves_checkout_unreachable(
    bot, shop, user
):
    """Emptying the cart mid-checkout must collapse the flow back to an empty
    cart screen. Leaving Confirm reachable is how an order with no lines gets
    posted."""
    add_address(bot, 900, "Uy", "Chilonzor 15")
    await fill_cart(bot, user, BOTTLE_19L, quantity=3)
    await reach_confirmation(bot, user, 900, payment="cash")

    await bot.send(user.tap("edit_order"))
    await bot.send(user.tap(f"cart_rm_{BOTTLE_19L}"))

    assert shop.cart == {}
    empty = bot.telegram.last_shown()
    assert empty.text == "Cart empty"
    assert "confirm_order" not in empty.callback_data()
    assert "cart_checkout" not in empty.callback_data()


async def test_switching_language_mid_checkout_renders_the_next_screen_in_the_new_language(
    bot, shop, user
):
    """`i18n.get_user_language` is read per render, so a language change made in
    another screen must take effect on the very next step. A screen that keeps
    serving the old language leaves a customer confirming money in a language
    they just told the bot they do not read."""
    add_address(bot, 900, "Uy", "Chilonzor 15")
    await fill_cart(bot, user, BOTTLE_19L, quantity=3)
    await reach_payment_picker(bot, user, 900)
    # The picker legitimately says more now: cash is on offer, so it quotes it.
    assert bot.telegram.last_shown().text.startswith("To'lov usulini tanlang")

    bot.database.user["preferred_language"] = "ru"

    await bot.send(user.tap("payment_cash"))
    confirmation = bot.telegram.last_shown()
    assert confirmation.text.startswith("Подтвердите заказ")
    assert "Наличные" in confirmation.text
    assert "Naqd pul" not in confirmation.text, "uz copy leaked into a ru screen"


async def test_a_failed_edit_of_the_success_screen_still_leaves_exactly_one_order(
    bot, shop, user
):
    """A placed order is reported as placed even when Telegram refuses the edit.

    `confirm_order` used to render its success screen with a bare
    `query.edit_message_text`, bypassing
    `BaseHandler._edit_or_replace_callback_message`. When Telegram rejected that
    edit ("message to edit not found" — the customer deleted the bubble; it
    appears in this project's production logs) the order HAD already been
    created, but the exception unwound into `_handle_error` and the customer was
    told the operation failed. Worse, `context.user_data.clear()` sat AFTER the
    edit, so the Confirm button stayed armed: a customer who believed the error
    and tapped again paid for a SECOND order.

    The fix guarantees both halves: the render goes through the helper every
    other screen uses (which REPLACES a bubble it cannot edit), and the checkout
    state is cleared BEFORE the render is attempted, so no rendering outcome can
    turn one order into two.
    """
    add_address(bot, 900, "Uy", "Chilonzor 15")
    await fill_cart(bot, user, BOTTLE_19L, quantity=3)
    await reach_confirmation(bot, user, 900, payment="cash")

    bot.telegram.fail("editMessageText", "Bad Request: message to edit not found")
    await bot.send(user.tap("confirm_order"))

    assert len(shop.orders) == 1, "the order was placed exactly once"
    assert "Error occurred" not in toasts(bot), (
        "the order exists — reporting it as a failure sends the customer to "
        "place it a second time"
    )

    success = bot.telegram.last_shown()
    assert "Placed success" in success.text, (
        "the bubble that could not be edited must be replaced by one that can"
    )
    assert f"Order BS-{shop.next_order_id}" in success.text
    assert "Cash note" in success.text

    # The un-edited screen still carries a live-looking Confirm button. It must
    # be disarmed by now: this is the tap that used to double-charge.
    await deliberate_retap(bot, user.tap("confirm_order"))
    assert len(shop.orders) == 1, "a stale Confirm button placed a second order"
    assert order_payloads(bot) == [{
        "delivery_address_id": 900,
        "payment_method": "cash",
        "source": "telegram",
        "items": [{"product_id": BOTTLE_19L, "quantity": 3}],
    }]


async def test_an_order_whose_screen_cannot_be_rendered_at_all_is_still_reported_as_placed(
    bot, shop, user
):
    """Telegram refuses the edit AND the replacement — the last honest word the
    customer gets is a callback answer, and it has to say the order was placed.

    This is the tail of the same production incident: once `POST /api/v1/orders`
    has returned 2xx and the cart has been cleared, the order exists no matter
    what the chat surface does afterwards. `telegram.error_occurred` here is a
    lie that costs money, because the customer's only reasonable response to it
    is to order again.
    """
    add_address(bot, 900, "Uy", "Chilonzor 15")
    await fill_cart(bot, user, BOTTLE_19L, quantity=3)
    await reach_confirmation(bot, user, 900, payment="cash")

    bot.telegram.fail("editMessageText", "Bad Request: message to edit not found")
    bot.telegram.fail("sendMessage", "Forbidden: bot was blocked by the user", status=403)
    await bot.send(user.tap("confirm_order"))

    assert len(shop.orders) == 1
    assert shop.cart == {}, "the order took the cart with it"
    assert "Error occurred" not in toasts(bot)
    assert "Order placed screen not updated" in toasts(bot), (
        "the customer must be told the order stands even though the screen "
        "could not be refreshed"
    )


# ---------------------------------------------------------------------------
# Wiring guards — a tap that lands nowhere is a spinner and then silence
# ---------------------------------------------------------------------------


async def test_every_button_the_checkout_journey_renders_is_claimed_by_a_handler(
    bot, shop, user
):
    """The single most common way a Telegram flow dies is a `callback_data`
    that no registered pattern matches: the button spins and nothing happens.
    Walk the whole journey and check, at each screen, that every button on it
    is claimed by some handler in the state the customer is actually in."""
    add_address(bot, 900, "Uy", "Chilonzor 15")

    # The oracle must be able to say NO, or the loop below asserts nothing.
    # `bot.handlers_matching` alone always says yes — see
    # `handlers_that_would_act`.
    assert bot.handlers_matching(user.tap("no_such_button_anywhere"), include_catch_alls=True), (
        "the pattern-less group -1 debug logger should claim even this"
    )
    assert handlers_that_would_act(bot, user.tap("no_such_button_anywhere")) == []

    journey = [
        (user.tap("menu_products"), "the category picker"),
        (user.tap(f"category_{WATER}"), "the product list"),
        (user.tap(f"product_{BOTTLE_19L}"), "the product card"),
        (user.tap(f"add_to_cart_{BOTTLE_19L}"), "the quantity selector"),
        (user.tap("cart_view"), "the cart"),
        (user.tap("cart_checkout"), "the address confirmation"),
        (user.tap("address_900"), "the payment picker"),
        (user.tap("payment_cash"), "the order confirmation"),
    ]

    for update, where in journey:
        bot.telegram.reset()
        await bot.send(update)
        rendered = bot.telegram.shown
        assert rendered, f"the bot showed nothing on {where}"

        for data in rendered[-1].callback_data():
            assert handlers_that_would_act(bot, user.tap(data)), (
                f"the '{data}' button on {where} lands nowhere: nothing but the "
                f"debug logger claims it, so the customer gets a spinner and "
                f"then silence"
            )


async def test_the_quantity_selectors_checkout_button_reaches_checkout_too(
    bot, shop, user
):
    """There are TWO checkout doors with different callback data: the cart's
    `cart_checkout` (routed via `products.cart_handler`) and the quantity
    selector's bare `checkout` (routed straight to `orders.checkout_handler`).
    Only one of them is covered by the cart journey, and a customer who never
    opens the cart uses the other."""
    add_address(bot, 900, "Uy", "Chilonzor 15")
    await fill_cart(bot, user, BOTTLE_19L, quantity=3)

    selector = bot.telegram.last_shown()
    assert "checkout" in selector.callback_data()

    await bot.send(user.tap("checkout"))

    confirmation = bot.telegram.last_shown()
    assert confirmation.callback_data() == [
        "address_900", "add_new_address_checkout", "back_to_cart",
    ]
    assert "Uy" in confirmation.text
