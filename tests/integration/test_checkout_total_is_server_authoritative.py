"""The customer-bot checkout GRAND TOTAL must be the server's figure, not the
bot's arithmetic over a client-visible price.

WHAT WAS SHIPPED. ``OrderHandlers._show_order_confirmation`` — the last screen
before "Confirm order" — recomputed the money itself::

    item_subtotal_price = product_payload.get('current_price', 0) * quantity
    cart_total_amount  += item_subtotal_price
    ...
    grand_total_amount  = max(0.0, float(cart_total_amount) - reward_discount)

That is a SECOND expression of "how much", sitting beside the server's own. The
two agree only by luck:

* ``Product.calculate_price`` (``business_app/models/product.py:140-143``)
  **ignores its ``user`` argument**, so the ``current_price`` that
  ``CartItem.to_dict`` bakes in is contract-blind and price-rule-blind.
  ``CartService.get_cart_details`` patches it afterwards from
  ``get_cart_summary`` — but only for lines that summary KEPT. Any line the
  summary drops (an inactive product) keeps the raw contract-blind number and
  the bot happily added it to a total the server never charges.
* order creation prices through ``resolve_contract_pricing_for_user_product``
  (``order_service.py:1461``), so a corporate-contract customer read one number
  on the confirm screen and was charged another.

THE FIX PRINCIPLE, proven six times over in this repo: **the figure shown and
the figure charged must come from one decision.** The server already publishes
that decision — ``cart['subtotal']`` and per-line ``cart_items[].total_price``,
both composed by ``CartService.get_cart_summary`` — so the bot reads it instead
of deriving its own.

WHY THESE TESTS DO NOT FABRICATE A CART. The defect is a payload-shape
assumption; a hand-written cart dict can only re-assert the fixture author's
idea of the shape. Every payload below comes out of the REAL
``CartService.get_cart_details`` over REAL ``carts`` / ``cart_items`` /
``corporate_contract_product_prices`` rows, and every "charged" figure comes out
of the REAL ``OrderService.create_order``.
"""

import pathlib
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

# The bots use workdir-relative BARE imports, which collide with the
# repo-root `config.py` once business_app has imported it. See
# tests/integration/_bot_import.py for the full mechanism.
from tests.integration._bot_import import REPO_ROOT, import_bot_module  # noqa: E402

orders_module = import_bot_module("telegram_bot", "handlers.orders")

from business_app.models.cart import Cart, CartItem  # noqa: E402
from business_app.models.corporate import (  # noqa: E402
    CorporateContract,
    CorporateContractProductPrice,
    CorporateContractStatus,
)
from business_app.models.user import UserAddress  # noqa: E402
from business_app.services.cart_service import CartService  # noqa: E402
from business_app.services.order_service import OrderService  # noqa: E402
from shared.enums import EntitySubtype  # noqa: E402
from tests.telegram_bot.helpers import (  # noqa: E402
    DummyCallbackQuery,
    DummyUpdate,
    FakeAPIClientContext,
    make_context,
)


BASE_PRICE = Decimal("15000.00")
CONTRACT_PRICE = Decimal("9000.00")
QUANTITY = 3


def _resp(success=True, data=None):
    return SimpleNamespace(success=success, data=data or {}, error=None, status_code=200)


def _i18n_get(key, language, *args, **kwargs):
    """Echo key:language and append interpolations, so a money kwarg still
    reaches the rendered string (the real copy interpolates ``{amount}``)."""
    rendered = f"{key}:{language}"
    if kwargs:
        rendered += "(" + ",".join(f"{k}={v}" for k, v in sorted(kwargs.items())) + ")"
    return rendered


# ---------------------------------------------------------------------------
# Fixtures — real rows, real payloads
# ---------------------------------------------------------------------------


@pytest.fixture
def contract_customer(db, sample_user, sample_product):
    """A workplace entity whose ACTIVE contract prices the product BELOW base.

    9 000 against a 15 000 base price: the gap is what the bot used to show and
    the server used to charge, in opposite directions.
    """
    sample_product.base_price = BASE_PRICE
    sample_product.discount_price = None
    sample_user.user_type = "entity"
    sample_user.entity_subtype = EntitySubtype.WORKPLACE
    db.session.commit()

    contract = CorporateContract(
        user_id=sample_user.id,
        contract_number="C-SHOW-VS-CHARGE-1",
        name="Checkout parity contract",
        status=CorporateContractStatus.ACTIVE,
        start_date=datetime.now(UTC) - timedelta(days=1),
        currency="UZS",
        is_active=True,
        allows_debt=True,
    )
    db.session.add(contract)
    db.session.flush()
    db.session.add(
        CorporateContractProductPrice(
            contract_id=contract.id,
            product_id=sample_product.id,
            unit_price=CONTRACT_PRICE,
            is_prepayment_eligible=True,
            is_active=True,
        )
    )
    db.session.commit()
    return sample_user


@pytest.fixture
def stocked_cart(db, contract_customer, sample_product):
    """``QUANTITY`` units of the product in this customer's REAL cart."""
    cart = Cart(user_id=contract_customer.id)
    db.session.add(cart)
    db.session.flush()
    db.session.add(
        CartItem(cart_id=cart.id, product_id=sample_product.id, quantity=QUANTITY)
    )
    db.session.commit()
    return cart


@pytest.fixture
def delivery_address(db, contract_customer):
    address = UserAddress(
        user_id=contract_customer.id,
        title="Office",
        full_address="Office Street 1, Tashkent",
        street_address="Office Street 1",
        city="Tashkent",
        latitude=41.31,
        longitude=69.28,
        is_default=True,
    )
    db.session.add(address)
    db.session.commit()
    return address


def _served_cart(user_id):
    """The payload ``GET /api/v1/cart`` actually serves for this user."""
    return CartService().get_cart_details(user_id)


async def _render_confirmation(monkeypatch, cart_payload, payment_method="cash"):
    """Drive the REAL ``_show_order_confirmation`` over a REAL cart payload and
    return the string the customer reads."""
    import eligibility

    handler = orders_module.OrderHandlers()
    update = DummyUpdate()
    update.callback_query = DummyCallbackQuery(data=f"payment_{payment_method}")
    context = make_context()
    context.user_data["selected_address_id"] = 5
    context.user_data["selected_payment_method"] = payment_method

    monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))
    monkeypatch.setattr(orders_module.i18n, "get", _i18n_get)
    monkeypatch.setattr(eligibility, "is_loyalty_eligible", AsyncMock(return_value=False))
    monkeypatch.setattr(orders_module, "get_auth_token", AsyncMock(return_value="jwt"))
    monkeypatch.setattr(
        orders_module.OrderKeyboards, "order_confirmation", lambda *_a, **_k: "confirm-kbd"
    )
    monkeypatch.setattr(
        orders_module,
        "api_client",
        FakeAPIClientContext(get_cart=_resp(data={"data": {"cart": cart_payload}})),
    )

    await handler._show_order_confirmation(update, context)
    return update.callback_query.edit_message_text.await_args.kwargs["text"]


def _charged_subtotal(user_id, product_id, address, mock_inventory_service):
    """What the server ACTUALLY charges — from the REAL order-creation path."""
    mock_inventory_service.check_multiple_products_availability.return_value = [
        SimpleNamespace(
            product_id=product_id,
            requested_quantity=QUANTITY,
            available_quantity=100,
            reserved_quantity=0,
            is_available=True,
            reason="Available",
        )
    ]
    mock_inventory_service.reserve_inventory.return_value = {"success": True, "expires_at": None}

    with patch(
        "business_app.services.payment_service.PaymentService.initialize_order_payment",
        return_value=None,
    ):
        order = OrderService(inventory_service=mock_inventory_service).create_order(
            user_id,
            {
                "items": [{"product_id": product_id, "quantity": QUANTITY}],
                "delivery_address": {
                    "delivery_address_id": address.id,
                    "street": address.street_address,
                    "latitude": address.latitude,
                    "longitude": address.longitude,
                },
                "payment_method": "cash",
            },
        )
    return order


# ---------------------------------------------------------------------------
# Guard rail
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_the_fixture_really_prices_the_contract_below_base(
    app, db, contract_customer, sample_product, stocked_cart
):
    """If contract pricing stopped biting, every assertion below would pass for
    the wrong reason. The raw ``Product.to_dict`` price and the served cart's
    unit price must DISAGREE — that disagreement IS the defect's fuel."""
    raw_product_price = sample_product.to_dict(user=contract_customer, quantity=QUANTITY)
    assert float(raw_product_price["current_price"]) == float(BASE_PRICE), (
        "Product.calculate_price ignores its `user` argument — if that changed, "
        "rewrite this file's premise rather than deleting the assertion"
    )

    served = _served_cart(contract_customer.id)
    assert served["subtotal"] == float(CONTRACT_PRICE) * QUANTITY
    assert served["subtotal"] != float(raw_product_price["current_price"]) * QUANTITY


# ---------------------------------------------------------------------------
# 🔴 THE PIN — shown == charged
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.anyio
async def test_grand_total_on_the_confirm_screen_is_what_the_server_charges(
    app, db, monkeypatch, contract_customer, sample_product, stocked_cart,
    delivery_address, mock_inventory_service,
):
    """🔴 THE INVARIANT. Do not delete this test.

    One number, read off the screen the customer confirms, compared with the
    order the server then creates from the same cart.
    """
    screen = await _render_confirmation(monkeypatch, _served_cart(contract_customer.id))

    order = _charged_subtotal(
        contract_customer.id, sample_product.id, delivery_address, mock_inventory_service
    )
    charged = float(order.subtotal)
    assert charged == float(CONTRACT_PRICE) * QUANTITY, "contract pricing must have applied"
    assert float(order.delivery_fee or 0) == 0.0, (
        "the screen states a 0 delivery fee; if delivery stops being free the "
        "screen has to state the real one before this comparison means anything"
    )

    from utils import format_price

    assert format_price(charged) in screen, (
        f"the charged {charged} is nowhere on the confirm screen: {screen!r}"
    )
    # ...and the contract-BLIND figure the bot used to compute must be absent.
    assert format_price(float(BASE_PRICE) * QUANTITY) not in screen, (
        f"the screen still shows the non-contract total: {screen!r}"
    )


@pytest.mark.integration
@pytest.mark.anyio
async def test_the_per_line_amount_is_the_servers_line_total(
    app, db, monkeypatch, contract_customer, sample_product, stocked_cart,
):
    """The item rows carry money too, and they were derived from the same
    contract-blind ``current_price``. They must be the server's ``total_price``."""
    served = _served_cart(contract_customer.id)
    line = served["cart_items"][0]
    assert line["total_price"] == float(CONTRACT_PRICE) * QUANTITY

    screen = await _render_confirmation(monkeypatch, served)

    from utils import format_price

    assert format_price(line["total_price"]) in screen, screen
    assert format_price(float(BASE_PRICE) * QUANTITY) not in screen, screen


@pytest.mark.integration
@pytest.mark.anyio
async def test_a_line_the_server_dropped_is_not_added_to_the_total(
    app, db, monkeypatch, contract_customer, sample_product, stocked_cart,
):
    """The fall-through path, which no contract is needed to reach.

    ``get_cart_details`` only overwrites ``current_price`` for lines that
    ``get_cart_summary`` kept; an INACTIVE product is skipped there and keeps the
    raw ``Product.to_dict`` price, while the server's ``subtotal`` excludes it
    entirely. The bot's own summation therefore charged the customer's eyes for a
    line the order will not contain.
    """
    sample_product.is_active = False
    db.session.commit()

    served = _served_cart(contract_customer.id)
    assert served["subtotal"] == 0.0, "the server drops inactive lines from its subtotal"
    # The line is still on the payload, still carrying the raw base price.
    raw_line = served["cart_items"][0]
    assert float(raw_line["product"]["current_price"]) == float(BASE_PRICE)

    screen = await _render_confirmation(monkeypatch, served)

    from utils import format_price

    assert format_price(float(BASE_PRICE) * QUANTITY) not in screen, (
        "a line the server will not charge for was summed into the shown total: "
        f"{screen!r}"
    )


@pytest.mark.integration
@pytest.mark.anyio
async def test_the_bot_performs_no_price_arithmetic_of_its_own(
    app, db, monkeypatch, contract_customer, sample_product, stocked_cart,
):
    """Structural pin, so the fix cannot be undone by "just adding a fallback".

    A second client-side calculation is the defect itself, not a safety net: it
    is what disagreed with the server. The handler must therefore never multiply
    a unit price by a quantity.
    """
    source = (REPO_ROOT / "telegram_bot" / "handlers" / "orders.py").read_text(encoding="utf-8")
    start = source.index("async def _show_order_confirmation")
    end = source.index("\n    async def ", start + 1)
    # Comment lines are stripped: the fix's own explanatory comment quotes the
    # code it replaced, and a scan that counted that would be unfixable.
    body = "\n".join(
        line for line in source[start:end].splitlines()
        if not line.lstrip().startswith("#")
    )

    for forbidden in ("current_price'] *", "current_price') *", "current_price', 0) *"):
        assert forbidden not in body, (
            f"_show_order_confirmation re-derives money ({forbidden!r}); the "
            "server's cart subtotal / line total_price is the one decision"
        )
    assert "cart.get('subtotal')" in body, (
        "the shown total must be read from the server's cart payload"
    )
    assert "item.get('total_price')" in body, (
        "the per-line money must be the server's line total"
    )
