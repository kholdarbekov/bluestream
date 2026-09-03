"""The operator bot showed **0 UZS for everything**.

``serialize_product`` publishes price NESTED under ``pricing``
(``business_app/serializers/product_serializers.py:366-377``) — there is no
top-level ``price`` key anywhere in the payload. The operator's order-creation
screens read exactly that missing key::

    staff_bot/handlers/operator/create_order.py:250   product.get('price', 0)
    staff_bot/handlers/operator/create_order.py:538   item.get('price', 0)
    staff_bot/keyboards/operator.py:61                product.get('price', 0)

``.get(..., 0)`` cannot return anything but ``0``, so every product button, the
product detail line and the cart subtotal read **0 UZS** while the server
charged the real price (``StaffService.create_phone_order`` →
``resolve_contract_pricing_for_user_product``, ``staff_service.py:1879-1884``).
The operator reads those numbers to the customer on the phone.

WHY THE PAYLOAD IS FETCHED THROUGH THE REAL ROUTE. The bug is a key-shape
assumption, so a hand-written product dict can only re-assert the fixture
author's version of the shape. Every payload below comes off the wire from
``GET /api/v1/products/`` with a real operator JWT, which is byte-for-byte what
``StaffAPIClient.get_products`` receives.

THE RESIDUAL GAP IS CLOSED — see
``test_operator_screen_shows_the_clients_contract_price``.
``/api/v1/products/`` prices for the *caller*, and the caller here is the
OPERATOR, so a corporate-contract client's screen stated the generic price while
the order charged the contract one (measured 45 000 against 27 000).
``POST /api/v1/staff/operator/users/<id>/order-estimate`` now prices the basket
for the CLIENT by calling ``StaffService.price_phone_order`` — the *same*
function ``StaffService.create_phone_order`` charges from. The screens below
render that response and compute nothing, so the quote and the charge are one
expression rather than two that happen to agree.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from business_app.models.corporate import (
    CorporateContract,
    CorporateContractProductPrice,
    CorporateContractStatus,
)
from business_app.models.user import User, UserAddress
from business_app.services.staff_service import StaffService
from business_app.utils.password_security import hash_password
from shared.enums import EntitySubtype, UserRole, UserType
from staff_bot.handlers.operator.create_order import (
    SELECT_PRODUCTS,
    SELECT_QUANTITY,
    CreateOrderHandler,
)
from staff_bot.keyboards.operator import OperatorKeyboards


BASE_PRICE = Decimal("15000.00")
QUANTITY = 3


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _AsyncClient:
    """Async-context-manager stand-in for the module-level ``api_client``."""

    def __init__(self, **methods):
        self.client = MagicMock()
        for name, mock in methods.items():
            setattr(self.client, name, mock)

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, *_):
        return False


def _ok(data):
    return MagicMock(success=True, data=data)


def _update(callback_data):
    upd = MagicMock()
    upd.effective_user = MagicMock(id=777)
    upd.message = None
    upd.callback_query = MagicMock()
    upd.callback_query.data = callback_data
    upd.callback_query.answer = AsyncMock()
    upd.callback_query.edit_message_text = AsyncMock()
    return upd


def _ctx():
    ctx = MagicMock()
    ctx.user_data = {"language": "en", "authenticated": True, "staff_roles": ["operator"]}
    ctx.bot = MagicMock()
    return ctx


def _edited_text(update):
    call = update.callback_query.edit_message_text.call_args
    return call.args[0] if call.args else call.kwargs["text"]


def _edited_markup(update):
    return update.callback_query.edit_message_text.call_args.kwargs.get("reply_markup")


def _button_labels(markup):
    return [button.text for row in markup.inline_keyboard for button in row]


@pytest.fixture
def operator(db):
    person = User(
        email="operator.parity@example.com",
        phone="+998901119911",
        password_hash=hash_password("OperatorPassword123!"),
        first_name="Olim",
        last_name="Operator",
        user_type=UserType.STAFF,
        role=UserRole.OPERATOR,
        is_verified=True,
        created_at=datetime.now(UTC),
    )
    db.session.add(person)
    db.session.commit()
    return person


@pytest.fixture
def priced_product(db, sample_product):
    """No discount price, no price rules — the plain configuration in which the
    published effective price and the charged price are the SAME number, so a
    mismatch can only come from the bot."""
    sample_product.base_price = BASE_PRICE
    sample_product.discount_price = None
    db.session.commit()
    return sample_product


@pytest.fixture
def client_address(db, sample_user):
    address = UserAddress(
        user_id=sample_user.id,
        title="Home",
        full_address="Client Street 1, Tashkent",
        street_address="Client Street 1",
        city="Tashkent",
        latitude=41.31,
        longitude=69.28,
        is_default=True,
    )
    db.session.add(address)
    db.session.commit()
    return address


def _operator_token(app, operator_user):
    from flask_jwt_extended import create_access_token

    with app.app_context():
        return create_access_token(identity=str(operator_user.id))


def _served_products(client, app, operator_user):
    """The payload ``StaffAPIClient.get_products`` receives, off the wire."""
    token = _operator_token(app, operator_user)
    response = client.get("/api/v1/products/", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["data"]


def _post_estimate(client, app, operator_user, client_id, items):
    """The REAL order-estimate route, called with a real operator JWT."""
    token = _operator_token(app, operator_user)
    return client.post(
        f"/api/v1/staff/operator/users/{client_id}/order-estimate",
        json={"items": items},
        headers={"Authorization": f"Bearer {token}"},
    )


def _estimate_off_the_wire(client, app, operator_user):
    """``StaffAPIClient.get_operator_order_estimate``, backed by the real route.

    The handler's whole job is now to RENDER this response, so a hand-written
    stub would only re-assert the fixture author's idea of the payload — the
    exact mistake that produced the 0-UZS bug this file was opened for.
    """

    async def _call(_token, client_id, items):
        response = _post_estimate(client, app, operator_user, client_id, items)
        if response.status_code != 200:
            return MagicMock(success=False, data=None, error=response.get_json())
        return _ok(response.get_json()["data"])

    return AsyncMock(side_effect=_call)


def _drive_operator_screens(
    monkeypatch, served, product_id, quantity, client_id, address_id, client=None, app=None,
    operator_user=None,
):
    """Walk the REAL operator flow and return every screen it renders."""
    from staff_bot.handlers.operator import create_order as mod

    handler = CreateOrderHandler()
    context = _ctx()
    context.user_data["new_order"] = {"client_id": client_id, "items": []}

    api = _AsyncClient(
        get_products=AsyncMock(return_value=_ok(served)),
        get_operator_order_estimate=_estimate_off_the_wire(client, app, operator_user),
    )
    monkeypatch.setattr(mod, "api_client", api)
    monkeypatch.setattr(handler, "_get_language", AsyncMock(return_value="en"))
    monkeypatch.setattr(handler, "_get_auth_token", AsyncMock(return_value="tok"))

    address_update = _update(f"staff_op_addr_{address_id}")
    state = asyncio.run(handler.select_address(address_update, context))
    assert state == SELECT_PRODUCTS

    detail_update = _update(f"staff_op_product_{product_id}")
    state = asyncio.run(handler.select_product(detail_update, context))
    assert state == SELECT_QUANTITY

    cart_update = _update(f"staff_op_qty_{product_id}_{quantity}")
    state = asyncio.run(handler.select_quantity(cart_update, context))
    assert state == SELECT_PRODUCTS

    return {
        "product_list": address_update,
        "detail": detail_update,
        "cart": cart_update,
        "context": context,
    }


def _charge(operator_user, client_user, product, quantity, address):
    """What the server ACTUALLY charges for this basket — the REAL phone-order
    path, not a reimplementation of its pricing rule."""
    with patch(
        "business_app.services.payment_service.PaymentService.initialize_order_payment",
        return_value=None,
    ):
        return StaffService.create_phone_order(
            operator_id=operator_user.id,
            client_id=client_user.id,
            order_data={
                "items": [{"product_id": product.id, "quantity": quantity}],
                "delivery_address_id": address.id,
                "payment_method": "cash",
                "delivery_notes": "parity check",
            },
        )


# ---------------------------------------------------------------------------
# Guard rail — the payload really has no top-level `price`
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_the_served_product_payload_has_no_top_level_price_key(
    app, db, client, operator, priced_product
):
    """The defect's root cause, asserted against the wire rather than assumed.

    If a top-level ``price`` key ever IS published, the handler's old
    ``.get('price', 0)`` would start working and this file's premise changes —
    so pin the shape, not the workaround.
    """
    served = _served_products(client, app, operator)
    payload = next(item for item in served["items"] if item["id"] == priced_product.id)

    assert "price" not in payload, (
        "the payload now publishes a top-level `price`; revisit the resolution "
        "order in CreateOrderHandler._display_unit_price"
    )
    assert payload["pricing"]["current_price"] == float(BASE_PRICE)


# ---------------------------------------------------------------------------
# 🔴 THE PIN — the rendered subtotal is what the server charges
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_operator_cart_subtotal_equals_what_the_server_charges(
    app, db, client, monkeypatch, operator, sample_user, priced_product, client_address
):
    """🔴 THE INVARIANT. Do not delete this test.

    Three operator surfaces render money — the product BUTTONS, the product
    DETAIL line and the CART subtotal — and every one of them printed 0 while
    the server charged 45 000.
    """
    served = _served_products(client, app, operator)
    screens = _drive_operator_screens(
        monkeypatch, served, priced_product.id, QUANTITY, sample_user.id, client_address.id,
        client=client, app=app, operator_user=operator,
    )

    order = _charge(operator, sample_user, priced_product, QUANTITY, client_address)
    charged = float(order.subtotal)
    assert charged == float(BASE_PRICE) * QUANTITY, "fixture must charge the plain price"

    # 1. the product buttons the operator taps
    labels = _button_labels(_edited_markup(screens["product_list"]))
    assert any("15,000" in label for label in labels), labels
    assert not any(label.startswith("Pure Water 19L - 0 ") for label in labels), labels

    # 2. the product detail screen
    assert "15,000" in _edited_text(screens["detail"]), _edited_text(screens["detail"])

    # 3. THE assertion — the subtotal the operator reads out loud
    cart_text = _edited_text(screens["cart"])
    assert f"{charged:,.0f}" in cart_text, (
        f"the charged {charged} is not on the cart screen: {cart_text!r}"
    )
    assert "0 UZS" not in cart_text.replace(f"{charged:,.0f}", ""), cart_text


@pytest.mark.integration
def test_operator_cart_subtotal_tracks_quantity(
    app, db, client, monkeypatch, operator, sample_user, priced_product, client_address
):
    """A different quantity, so a hard-coded single-unit price cannot satisfy
    the pin above by accident."""
    served = _served_products(client, app, operator)
    screens = _drive_operator_screens(
        monkeypatch, served, priced_product.id, 5, sample_user.id, client_address.id,
        client=client, app=app, operator_user=operator,
    )

    order = _charge(operator, sample_user, priced_product, 5, client_address)
    assert f"{float(order.subtotal):,.0f}" in _edited_text(screens["cart"])


@pytest.mark.integration
def test_the_cart_line_and_the_subtotal_are_one_resolution(
    app, db, client, monkeypatch, operator, sample_user, priced_product, client_address
):
    """Two screens, one price resolution.

    ``select_product`` (detail), ``OperatorKeyboards.product_list`` (buttons) and
    ``_format_cart_summary`` (line + subtotal) each read a price. Three
    independent reads is how one of them ended up on a key that does not exist,
    so the handler normalises ONCE and everything downstream reads that.
    """
    served = _served_products(client, app, operator)
    payload = next(item for item in served["items"] if item["id"] == priced_product.id)
    unit = CreateOrderHandler._display_unit_price(payload)

    assert unit == float(BASE_PRICE)

    screens = _drive_operator_screens(
        monkeypatch, served, priced_product.id, QUANTITY, sample_user.id, client_address.id,
        client=client, app=app, operator_user=operator,
    )
    stashed = screens["context"].user_data["available_products"][str(priced_product.id)]
    # The keyboard module reads a top-level `price`; it gets the SAME number.
    assert stashed["price"] == unit
    assert any(
        f"{unit:,.0f}" in label
        for label in _button_labels(OperatorKeyboards.product_list("en", [stashed]))
    )
    cart_item = screens["context"].user_data["new_order"]["items"][0]
    assert cart_item["price"] == unit


@pytest.mark.integration
def test_an_empty_cart_still_renders(
    app, db, client, monkeypatch, operator, sample_user, priced_product, client_address
):
    """The pre-selection screen: no items, no crash, no fabricated money."""
    from staff_bot.handlers.operator import create_order as mod

    served = _served_products(client, app, operator)
    handler = CreateOrderHandler()
    context = _ctx()
    context.user_data["new_order"] = {"client_id": sample_user.id, "items": []}
    monkeypatch.setattr(mod, "api_client", _AsyncClient(
        get_products=AsyncMock(return_value=_ok(served)),
        get_operator_order_estimate=_estimate_off_the_wire(client, app, operator),
    ))
    monkeypatch.setattr(handler, "_get_language", AsyncMock(return_value="en"))
    monkeypatch.setattr(handler, "_get_auth_token", AsyncMock(return_value="tok"))

    update = _update(f"staff_op_addr_{client_address.id}")
    asyncio.run(handler.select_address(update, context))

    text = _edited_text(update)
    assert "15,000" not in text, "an empty cart must not state a subtotal"


# ---------------------------------------------------------------------------
# 🔴 THE GAP THAT WAS — now an equality
# ---------------------------------------------------------------------------


@pytest.fixture
def contract_client(db, sample_user, priced_product):
    """``sample_user`` on an active corporate contract at 9 000/unit.

    Contract pricing only applies to LEGAL-ENTITY users
    (`CorporateContractService.list_active_contracts_for_user` gates on
    `_is_legal_entity_user`), which is exactly the workplace phone order the
    operator flow exists to take.
    """
    sample_user.user_type = "entity"
    sample_user.entity_subtype = EntitySubtype.WORKPLACE
    db.session.commit()

    contract = CorporateContract(
        user_id=sample_user.id,
        contract_number="C-OPERATOR-GAP-1",
        name="Operator gap contract",
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
            product_id=priced_product.id,
            unit_price=Decimal("9000.00"),
            is_prepayment_eligible=True,
            is_active=True,
        )
    )
    db.session.commit()
    return sample_user


@pytest.mark.integration
def test_operator_screen_shows_the_clients_contract_price(
    app, db, client, monkeypatch, operator, contract_client, priced_product, client_address
):
    """🔴 THE INVARIANT THIS FILE WAS OPENED FOR. Do not delete this test.

    WAS ``test_operator_screen_cannot_show_a_clients_contract_price``, a pin on
    the DIVERGENCE: ``GET /api/v1/products/`` resolves ``pricing.current_price``
    for the CALLER (``business_app/api/products.py:100-111`` →
    ``serialize_product(..., user=current_user)``) and the caller on this screen
    is the OPERATOR, so the screen stated 45 000 for an order that charged
    27 000. Its docstring said to convert it to an equality when a client-scoped
    quote landed. It has: ``POST /staff/operator/users/<id>/order-estimate``.

    The equality is asserted against a REAL created order rather than against a
    recomputed expectation — the point is not that two numbers match, it is that
    the screen and the charge are the same function
    (``StaffService.price_phone_order``) called twice.
    """
    served = _served_products(client, app, operator)
    screens = _drive_operator_screens(
        monkeypatch, served, priced_product.id, QUANTITY, contract_client.id, client_address.id,
        client=client, app=app, operator_user=operator,
    )
    order = _charge(operator, contract_client, priced_product, QUANTITY, client_address)

    charged = float(order.subtotal)
    assert charged == 27000.0, "the contract must price this client at 9 000/unit"

    # 1. the cart line the operator reads out loud
    cart_text = _edited_text(screens["cart"])
    assert f"{charged:,.0f}" in cart_text, cart_text
    assert "45,000" not in cart_text, (
        "the generic, OPERATOR-scoped price is back on the screen: " + cart_text
    )

    # 2. the stashed cart item — what the number on the screen was built from
    shown = screens["context"].user_data["new_order"]["items"][0]["price"] * QUANTITY
    assert shown == charged, (
        f"the screen quoted {shown:,.0f} and the order charged {charged:,.0f}"
    )

    # 3. the product BUTTONS and the detail line, which are also money
    labels = _button_labels(_edited_markup(screens["product_list"]))
    assert any("9,000" in label for label in labels), labels
    assert not any("15,000" in label for label in labels), labels
    assert "9,000" in _edited_text(screens["detail"]), _edited_text(screens["detail"])


# ---------------------------------------------------------------------------
# THE ENDPOINT ITSELF
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_the_estimate_equals_what_the_order_charges_for_a_contract_client(
    app, db, client, operator, contract_client, priced_product, client_address
):
    """The quote and the charge, compared against a REAL order.

    Not "the estimate returns 27 000" — that would only re-assert this test's
    own arithmetic. The order is created through the real phone-order path and
    its persisted subtotal is the expectation.
    """
    response = _post_estimate(
        client, app, operator, contract_client.id,
        [{"product_id": priced_product.id, "quantity": QUANTITY}],
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    estimate = response.get_json()["data"]

    order = _charge(operator, contract_client, priced_product, QUANTITY, client_address)

    assert estimate["subtotal"] == float(order.subtotal) == 27000.0
    assert estimate["items"][0]["unit_price"] == float(order.order_items[0].unit_price) == 9000.0
    assert estimate["items"][0]["total_price"] == float(order.order_items[0].total_price) == 27000.0
    assert estimate["items"][0]["is_contract_price"] is True


@pytest.mark.integration
def test_the_estimate_is_the_generic_price_for_a_non_contract_client(
    app, db, client, operator, sample_user, priced_product, client_address
):
    """No contract, no surprise: the quote is the plain catalogue price, and it
    is still the SAME number the order charges."""
    response = _post_estimate(
        client, app, operator, sample_user.id,
        [{"product_id": priced_product.id, "quantity": QUANTITY}],
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    estimate = response.get_json()["data"]

    order = _charge(operator, sample_user, priced_product, QUANTITY, client_address)

    assert estimate["subtotal"] == float(order.subtotal) == float(BASE_PRICE) * QUANTITY
    assert estimate["items"][0]["unit_price"] == float(BASE_PRICE)
    assert estimate["items"][0]["is_contract_price"] is False


@pytest.mark.integration
def test_the_estimate_creates_nothing(app, db, client, operator, contract_client, priced_product):
    """READ-ONLY. A quote that can leave a row behind is a checkout with a
    typo in its name — an operator exploring prices on the phone must not be
    able to mint orders."""
    from business_app.models.order import Order, OrderItem

    before_orders = Order.query.count()
    before_items = OrderItem.query.count()

    for _ in range(3):
        response = _post_estimate(
            client, app, operator, contract_client.id,
            [{"product_id": priced_product.id, "quantity": QUANTITY}],
        )
        assert response.status_code == 200, response.get_data(as_text=True)

    assert Order.query.count() == before_orders
    assert OrderItem.query.count() == before_items


@pytest.mark.integration
def test_the_estimate_refuses_a_caller_without_the_operator_role(
    app, db, client, sample_user, priced_product
):
    """Same guard as every neighbouring operator route (`require_staff_roles`).

    A client-scoped price list is exactly the sort of endpoint that gets opened
    up "because it only reads" — it publishes one customer's negotiated contract
    rates to whoever asks.
    """
    from flask_jwt_extended import create_access_token

    with app.app_context():
        customer_token = create_access_token(identity=str(sample_user.id))

    response = client.post(
        f"/api/v1/staff/operator/users/{sample_user.id}/order-estimate",
        json={"items": [{"product_id": priced_product.id, "quantity": 1}]},
        headers={"Authorization": f"Bearer {customer_token}"},
    )
    assert response.status_code == 403, response.get_data(as_text=True)

    anonymous = client.post(
        f"/api/v1/staff/operator/users/{sample_user.id}/order-estimate",
        json={"items": [{"product_id": priced_product.id, "quantity": 1}]},
    )
    assert anonymous.status_code == 401, anonymous.get_data(as_text=True)


@pytest.mark.integration
def test_the_estimate_rejects_an_empty_basket(app, db, client, operator, sample_user):
    """An empty quote has no honest number to state."""
    response = _post_estimate(client, app, operator, sample_user.id, [])
    assert response.status_code == 400, response.get_data(as_text=True)


@pytest.mark.integration
def test_the_operator_quote_publishes_every_discount_term(
    app, db, client, operator, sample_user, priced_product, client_address
):
    """🔴 `price_phone_order` used to compute `subtotal + delivery_fee` — NO
    discount term at all (design spec §4.6). An operator-placed order for a
    customer entitled to a discount was quoted, and charged, the full price.

    It now goes through `compute_order_total` and publishes all three discount
    terms. They are zero for a plain basket; what is pinned here is that the
    FIELDS EXIST and that the total is the formula's output, so the loyalty-tier
    work has exactly one place to fill in and cannot ship a quote that omits it.
    """
    response = _post_estimate(
        client, app, operator, sample_user.id,
        [{"product_id": priced_product.id, "quantity": QUANTITY}],
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    estimate = response.get_json()["data"]

    for field in ("discount_amount", "loyalty_discount", "tier_discount"):
        assert field in estimate, (
            f"the operator quote omits `{field}`. A payload that does not "
            "publish the number the screen needs forces the client to compute "
            "it, and a second expression is born."
        )
        assert estimate[field] == 0.0

    assert estimate["total_amount"] == (
        estimate["subtotal"]
        - estimate["discount_amount"]
        + estimate["delivery_fee"]
        - estimate["loyalty_discount"]
        - estimate["tier_discount"]
    )

    # And the ORDER the quote describes carries the same terms on its row.
    order = _charge(operator, sample_user, priced_product, QUANTITY, client_address)
    assert float(order.discount_amount) == estimate["discount_amount"]
    assert float(order.loyalty_discount) == estimate["loyalty_discount"]
    assert float(order.total_amount) == estimate["total_amount"]
