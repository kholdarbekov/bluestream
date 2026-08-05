"""The customer-bot CART SCREEN total must be the server's figure, and so must
the minimum-order gate that total drives.

WHAT WAS SHIPPED. ``ProductHandlers.show_cart`` — the screen one tap BEFORE the
order confirmation — priced the cart itself::

    price        = product['current_price']
    line_total   = price * quantity
    total_amount += line_total
    ...
    if total_amount < MIN_ORDER_AMOUNT:

``_show_order_confirmation`` was fixed for exactly this and carries a comment
forbidding the reintroduction of arithmetic
(``tests/integration/test_checkout_total_is_server_authoritative.py``).
``show_cart`` renders the SAME ``GET /cart`` payload and never got the same
treatment, so the second expression survived one screen earlier.

WHY IT DIVERGES, mechanically:

* ``CartService.get_cart_summary`` **skips inactive products entirely**
  (``cart_service.py``: ``if not product or not product.is_active: continue``),
  so its ``subtotal`` — the figure order creation is built from — excludes them.
* ``CartService.get_cart_details`` patches ``current_price`` only for the lines
  that summary KEPT. A dropped line keeps the raw, contract-blind price baked by
  ``CartItem.to_dict`` → ``Product.calculate_price`` (which ignores its ``user``
  argument), and the bot summed it into a total the order will never contain.
* That inflated total then decided ``MIN_ORDER_AMOUNT``, i.e. whether the
  **checkout button existed at all** — a gate opened against a number the server
  disagrees with.

THE FIX PRINCIPLE: the figure shown and the figure charged are ONE decision, and
that decision is the server's. ``show_cart`` reads ``cart['subtotal']`` and
per-line ``cart_items[].total_price``, and feeds that same subtotal to
``min_order_shortfall`` — one expression behind both the gate and its copy.

WHY THESE TESTS DO NOT FABRICATE A CART. The defect is a payload-shape
assumption; a hand-written cart dict can only re-assert the fixture author's
idea of the shape. Every payload below comes out of the REAL
``CartService.get_cart_details`` over REAL ``carts`` / ``cart_items`` /
``products`` rows.
"""

import pathlib
import sys
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

# The bots use workdir-relative BARE imports, which collide with the
# repo-root `config.py` once business_app has imported it. See
# tests/integration/_bot_import.py for the full mechanism.
from tests.integration._bot_import import REPO_ROOT, import_bot_module  # noqa: E402

products_module = import_bot_module("telegram_bot", "handlers.products")

from business_app.models.cart import Cart, CartItem  # noqa: E402
from business_app.models.product import Product  # noqa: E402
from business_app.services.cart_service import CartService  # noqa: E402
from shared.business_config import MIN_ORDER_AMOUNT  # noqa: E402
from tests.telegram_bot.helpers import (  # noqa: E402
    DummyCallbackQuery,
    DummyUpdate,
    FakeAPIClientContext,
    make_context,
)


# Priced off the floor itself so the scenario keeps its meaning if the business
# constant moves: the KEPT line alone cannot clear it, the DROPPED line alone
# would clear it three times over.
KEPT_PRICE = Decimal(MIN_ORDER_AMOUNT) * Decimal("0.75")      # 15 000
KEPT_QTY = 1
DROPPED_PRICE = Decimal(MIN_ORDER_AMOUNT) * Decimal("1.50")   # 30 000
DROPPED_QTY = 2

SERVER_SUBTOTAL = float(KEPT_PRICE) * KEPT_QTY                # 15 000
DROPPED_LINE_RAW = float(DROPPED_PRICE) * DROPPED_QTY         # 60 000
BOT_INFLATED_TOTAL = SERVER_SUBTOTAL + DROPPED_LINE_RAW       # 75 000


def _resp(success=True, data=None):
    return SimpleNamespace(success=success, data=data or {}, error=None, status_code=200)


def _i18n_get(key, language, *args, **kwargs):
    """Echo ``key:language`` and append interpolations, so a money kwarg still
    reaches the rendered string (the real copy interpolates ``{remaining}``)."""
    rendered = f"{key}:{language}"
    if kwargs:
        rendered += "(" + ",".join(f"{k}={v}" for k, v in sorted(kwargs.items())) + ")"
    return rendered


# ---------------------------------------------------------------------------
# Fixtures — real rows, real payloads
# ---------------------------------------------------------------------------


def _make_product(db, sample_category, name, price, is_active):
    product = Product(
        name=name,
        description=name,
        category_id=sample_category.id,
        size="19L",
        volume=19.0,
        volume_unit="L",
        base_price=price,
        stock_quantity=100,
        min_stock_level=1,
        max_stock_level=500,
        is_active=is_active,
        created_at=datetime.now(UTC),
    )
    db.session.add(product)
    db.session.commit()
    return product


@pytest.fixture
def mixed_cart(db, sample_user, sample_category):
    """One line the server PRICES and one line the server DROPS.

    The dropped product is deactivated AFTER it is in the cart — which is what
    happens in production (a product is retired while it sits in carts), and is
    the only state in which ``get_cart_details`` serves a line with a raw
    ``current_price`` and no ``total_price``.
    """
    kept = _make_product(db, sample_category, "Kept 19L", KEPT_PRICE, True)
    dropped = _make_product(db, sample_category, "Retired 19L", DROPPED_PRICE, True)

    cart = Cart(user_id=sample_user.id)
    db.session.add(cart)
    db.session.flush()
    db.session.add(CartItem(cart_id=cart.id, product_id=kept.id, quantity=KEPT_QTY))
    db.session.add(CartItem(cart_id=cart.id, product_id=dropped.id, quantity=DROPPED_QTY))
    db.session.commit()

    dropped.is_active = False
    db.session.commit()
    return SimpleNamespace(cart=cart, kept=kept, dropped=dropped)


@pytest.fixture
def sufficient_cart(db, sample_user, sample_category):
    """An all-active cart whose SERVER subtotal clears the floor."""
    product = _make_product(db, sample_category, "Kept 19L", KEPT_PRICE, True)
    cart = Cart(user_id=sample_user.id)
    db.session.add(cart)
    db.session.flush()
    db.session.add(CartItem(cart_id=cart.id, product_id=product.id, quantity=2))
    db.session.commit()
    return cart


def _served_cart(user_id):
    """The payload ``GET /api/v1/cart`` actually serves for this user."""
    return CartService().get_cart_details(user_id)


async def _render_cart(monkeypatch, cart_payload):
    """Drive the REAL ``show_cart`` over a REAL cart payload.

    Returns ``(screen_text, callback_data_of_every_button)`` — the two things a
    customer can act on. The keyboard is the REAL ``OrderKeyboards.cart_actions``
    because the minimum-order gate is not a string: it is whether the checkout
    button exists.
    """
    handler = products_module.ProductHandlers()
    update = DummyUpdate()
    update.callback_query = DummyCallbackQuery(data="cart_view")
    context = make_context()

    monkeypatch.setattr(products_module.i18n, "get_user_language", AsyncMock(return_value="en"))
    monkeypatch.setattr(products_module.i18n, "get", _i18n_get)
    monkeypatch.setattr(products_module, "get_auth_token", AsyncMock(return_value="jwt"))
    monkeypatch.setattr(
        products_module,
        "api_client",
        FakeAPIClientContext(get_cart=_resp(data={"data": {"cart": cart_payload}})),
    )

    await handler.show_cart(update, context)

    kwargs = update.callback_query.edit_message_text.await_args.kwargs
    markup = kwargs["reply_markup"]
    callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    return kwargs["text"], callbacks


# ---------------------------------------------------------------------------
# Guard rail
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_the_fixture_really_serves_a_line_the_server_dropped(app, db, sample_user, mixed_cart):
    """If the server stopped dropping inactive lines, every assertion below would
    pass for the wrong reason. The raw line price and the server's subtotal must
    DISAGREE — that disagreement IS the defect's fuel."""
    served = _served_cart(sample_user.id)

    assert served["subtotal"] == SERVER_SUBTOTAL, (
        "get_cart_summary must exclude the inactive product from the subtotal"
    )

    lines_by_product = {line["product_id"]: line for line in served["cart_items"]}
    kept_line = lines_by_product[mixed_cart.kept.id]
    dropped_line = lines_by_product[mixed_cart.dropped.id]

    assert kept_line["total_price"] == SERVER_SUBTOTAL
    # The dropped line is STILL on the payload, still carrying a raw price, and
    # carries no server line total at all — this is exactly what the bot summed.
    assert "total_price" not in dropped_line, (
        "get_cart_details only patches lines get_cart_summary kept"
    )
    assert float(dropped_line["product"]["current_price"]) == float(DROPPED_PRICE)
    assert BOT_INFLATED_TOTAL > MIN_ORDER_AMOUNT > SERVER_SUBTOTAL, (
        "the scenario is only meaningful if the bot's old sum cleared the floor "
        "and the server's subtotal does not"
    )


# ---------------------------------------------------------------------------
# 🔴 THE PINS — shown == the server's figure, and the gate reads that figure
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.anyio
async def test_cart_screen_shows_the_servers_subtotal_not_an_inflated_sum(
    app, db, monkeypatch, sample_user, mixed_cart,
):
    """🔴 THE INVARIANT. Do not delete this test.

    One number, read off the cart screen, compared with the only subtotal the
    server publishes — the one ``CartService.get_cart_summary`` composes and the
    order is built from.
    """
    from utils import format_price

    served = _served_cart(sample_user.id)
    screen, _ = await _render_cart(monkeypatch, served)

    assert format_price(served["subtotal"]) in screen, (
        f"the server's subtotal {served['subtotal']} is nowhere on the cart "
        f"screen: {screen!r}"
    )
    assert format_price(BOT_INFLATED_TOTAL) not in screen, (
        "the screen still totals a line the order will not contain: "
        f"{screen!r}"
    )
    assert format_price(DROPPED_LINE_RAW) not in screen, (
        "the dropped line still renders its raw contract-blind price: "
        f"{screen!r}"
    )


@pytest.mark.integration
@pytest.mark.anyio
async def test_the_min_order_gate_is_decided_by_the_servers_subtotal(
    app, db, monkeypatch, sample_user, mixed_cart,
):
    """🔴 THE GATE. The checkout button is money logic, not decoration.

    The bot's own sum cleared ``MIN_ORDER_AMOUNT`` three times over while the
    server's subtotal sits below it. Whether the customer can start a checkout
    the server would reject must be decided by the server's figure.
    """
    from utils import format_price

    served = _served_cart(sample_user.id)
    screen, callbacks = await _render_cart(monkeypatch, served)

    assert "cart_checkout" not in callbacks, (
        "checkout was unlocked against the bot's own inflated total; the server "
        f"subtotal is {served['subtotal']} against a {MIN_ORDER_AMOUNT} floor"
    )
    assert "telegram.cart_min_order_warning:en" in screen, screen
    # ...and the shortfall quoted to the customer is measured off the same figure.
    shortfall = products_module.min_order_shortfall(served["subtotal"])
    assert shortfall == MIN_ORDER_AMOUNT - SERVER_SUBTOTAL
    assert f"remaining={format_price(shortfall)}" in screen, screen


@pytest.mark.integration
@pytest.mark.anyio
async def test_the_gate_still_opens_when_the_servers_subtotal_clears_the_floor(
    app, db, monkeypatch, sample_user, sufficient_cart,
):
    """The mirror case, so the gate is not merely stuck shut.

    Without this, replacing the gate with `False` would pass the test above.
    """
    served = _served_cart(sample_user.id)
    assert served["subtotal"] >= MIN_ORDER_AMOUNT

    screen, callbacks = await _render_cart(monkeypatch, served)

    assert "cart_checkout" in callbacks, screen
    assert "telegram.cart_min_order_warning:en" not in screen, screen
    assert "telegram.cart_ready_checkout:en" in screen, screen


@pytest.mark.integration
def test_the_cart_screen_performs_no_price_arithmetic_of_its_own():
    """Structural pin, so the fix cannot be undone by "just adding a fallback".

    A second client-side calculation is the defect itself, not a safety net: it
    is what disagreed with the server. Mirrors
    ``test_the_bot_performs_no_price_arithmetic_of_its_own`` on the confirm
    screen, and is enforced repo-wide by
    ``tests/unit/test_show_vs_settle_invariant.py``.
    """
    source = (REPO_ROOT / "telegram_bot" / "handlers" / "products.py").read_text(encoding="utf-8")
    start = source.index("    async def show_cart(")
    end = source.index("\n    async def ", start + 1)
    # Comment lines are stripped: the fix's own explanatory comment quotes the
    # code it replaced, and a scan that counted that would be unfixable.
    body = "\n".join(
        line for line in source[start:end].splitlines()
        if not line.lstrip().startswith("#")
    )

    for forbidden in ("current_price'] *", "current_price') *", "current_price', 0) *"):
        assert forbidden not in body, (
            f"show_cart re-derives money ({forbidden!r}); the server's cart "
            "subtotal / line total_price is the one decision"
        )
    assert "cart.get('subtotal')" in body, (
        "the shown total must be read from the server's cart payload"
    )
    assert "item.get('total_price')" in body, (
        "the per-line money must be the server's line total"
    )
    assert "min_order_shortfall(subtotal)" in body, (
        "the minimum-order gate must be fed the server's subtotal through the "
        "one shared expression"
    )
