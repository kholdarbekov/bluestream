"""A contract customer is charged the price of the payment method (spec 2026-10-09 §4).

Driven over HTTP with the bodies the clients send.
"""

from decimal import Decimal

import pytest

from business_app import db as _db
from business_app.models.order import Order
from shared.enums import PaymentMethod, UserRole
from tests.integration.contract_pricing_factory import (
    BANK,
    CASH,
    CATALOGUE,
    QUANTITY,
    customer_headers,
    make_contract,
    make_staff,
    make_workplace,
)
from tests.integration.tier_discount_factory import post_order

pytestmark = pytest.mark.integration


@pytest.fixture
def world(db, sample_product):
    user, address = make_workplace(db)
    _contract, row = make_contract(db, user, sample_product)
    return user, address, row


def _reload(order_id):
    _db.session.expire_all()
    return Order.query.get(order_id)


def _place(app, user, address, product, method, quantity=QUANTITY):
    resp = post_order(
        app,
        customer_headers(app, user),
        product_id=product.id,
        address_id=address.id,
        payment_method=method,
        quantity=quantity,
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    return _reload(resp.get_json()["data"]["order"]["id"])


@pytest.mark.parametrize(
    "method, unit",
    [("cash", CASH), ("business_account", BANK), ("click", CATALOGUE), (None, BANK)],
    ids=["cash", "business_account", "click", "default-business-account"],
)
def test_each_method_charges_its_own_price(app, db, world, sample_product, method, unit):
    user, address, row = world
    order = _place(app, user, address, sample_product, method)

    (line,) = order.order_items
    assert Decimal(str(line.unit_price)) == unit
    assert Decimal(str(line.total_price)) == unit * QUANTITY
    assert Decimal(str(order.subtotal)) == unit * QUANTITY
    # Coverage is recorded whatever the method (spec §3.3).
    assert line.contract_product_price_id == row.id
    assert line.contract_id == row.contract_id


def test_an_empty_cash_price_charges_the_catalogue(app, db, world, sample_product):
    user, address, row = world
    row.cash_unit_price = None
    _db.session.commit()

    order = _place(app, user, address, sample_product, "cash")
    assert Decimal(str(order.order_items[0].unit_price)) == CATALOGUE


def test_a_row_with_both_prices_empty_still_allows_business_account_at_the_catalogue(
    app, db, world, sample_product
):
    """Review Focus #5: an empty row still COVERS the product."""
    user, address, row = world
    row.business_account_unit_price = None
    row.cash_unit_price = None
    _db.session.commit()

    order = _place(app, user, address, sample_product, "business_account")
    assert order.payment_method == PaymentMethod.BUSINESS_ACCOUNT
    assert Decimal(str(order.order_items[0].unit_price)) == CATALOGUE


@pytest.mark.parametrize("method", ["cash", "business_account", "click"])
def test_the_checkout_quote_equals_the_charge(app, db, world, sample_product, method):
    user, address, _row = world
    quote = app.test_client().post(
        "/api/v1/orders/cart/estimate",
        headers=customer_headers(app, user),
        json={
            "items": [{"product_id": sample_product.id, "quantity": QUANTITY}],
            "payment_method": method,
            "delivery_address_id": address.id,
        },
    )
    assert quote.status_code == 200, quote.get_data(as_text=True)
    pricing = quote.get_json()["data"]["pricing"]

    order = _place(app, user, address, sample_product, method)
    assert Decimal(str(pricing["items_subtotal"])) == Decimal(str(order.subtotal))
    assert Decimal(str(pricing["final_total"])) == Decimal(str(order.total_amount))


@pytest.mark.parametrize(
    "endpoint, items_key",
    [("/api/v1/orders/cart/estimate", "items"), ("/api/v1/cart/estimate", "cart_items")],
    ids=["orders-cart-estimate", "cart-estimate"],
)
def test_a_basket_below_the_minimum_at_the_contract_price_is_refused_by_the_quote(
    app, db, world, sample_product, endpoint, items_key
):
    """Review Focus #1: 2 × 15 000 clears the 20 000 floor at the catalogue; 2 × 9 000 on cash does not.

    The refusal is machine-readable (`ORDER_MIN_AMOUNT`, the code `create_order` uses) and carries the
    server's own figures, so the bot's confirm screen states the shortfall without pricing anything.
    """
    user, address, row = world
    row.cash_unit_price = Decimal("9000.00")
    _db.session.commit()

    quote = app.test_client().post(
        endpoint,
        headers=customer_headers(app, user),
        json={
            items_key: [{"product_id": sample_product.id, "quantity": QUANTITY}],
            "payment_method": "cash",
            "delivery_address_id": address.id,
        },
    )
    assert quote.status_code == 400, quote.get_data(as_text=True)
    body = quote.get_json()
    assert "Minimum order amount" in body["message"]
    assert body["error_code"] == "ORDER_MIN_AMOUNT", body
    assert body["details"]["min_amount"] == float(app.config["MIN_ORDER_AMOUNT"]), body
    assert body["details"]["items_subtotal"] == 18000.0, body


@pytest.mark.parametrize("method", ["cash", "business_account", "click"])
def test_two_contracts_covering_one_product_are_refused_on_every_method(app, db, world, sample_product, method):
    """Review Focus #4: coverage is resolved before the method, so Click cannot slip past it."""
    user, address, _row = world
    make_contract(db, user, sample_product)

    resp = post_order(
        app,
        customer_headers(app, user),
        product_id=sample_product.id,
        address_id=address.id,
        payment_method=method,
        quantity=QUANTITY,
    )
    assert resp.status_code == 400, resp.get_data(as_text=True)
    assert "Ambiguous contract pricing" in resp.get_data(as_text=True)


# --------------------------------------------------------------------------- #
# Phone orders: the operator's quote on a method is what the phone order charges
# --------------------------------------------------------------------------- #


@pytest.fixture
def operator(db):
    return make_staff(db, UserRole.OPERATOR, "+998901119912")


def _operator_estimate(app, operator_user, client_id, product, payment_method=None):
    body = {"items": [{"product_id": product.id, "quantity": QUANTITY}]}
    if payment_method is not None:
        body["payment_method"] = payment_method
    resp = app.test_client().post(
        f"/api/v1/staff/operator/users/{client_id}/order-estimate",
        json=body,
        headers=customer_headers(app, operator_user),
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()["data"]


def _operator_order(app, operator_user, client_id, product, address, payment_method):
    resp = app.test_client().post(
        "/api/v1/staff/operator/orders",
        json={
            "client_id": client_id,
            "items": [{"product_id": product.id, "quantity": QUANTITY}],
            "delivery_address_id": address.id,
            "payment_method": payment_method,
            "delivery_notes": "",
        },
        headers=customer_headers(app, operator_user),
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    return _reload(resp.get_json()["data"]["id"])


@pytest.mark.parametrize("method, unit", [("cash", CASH), ("business_account", BANK)])
def test_the_operator_quote_on_a_method_equals_the_phone_order_charge(
    app, db, world, sample_product, operator, method, unit
):
    user, address, _row = world
    estimate = _operator_estimate(app, operator, user.id, sample_product, method)
    order = _operator_order(app, operator, user.id, sample_product, address, method)

    assert Decimal(str(estimate["subtotal"])) == Decimal(str(order.subtotal)) == unit * QUANTITY
    assert Decimal(str(estimate["total_amount"])) == Decimal(str(order.total_amount))
    assert estimate["items"][0]["is_contract_price"] is True
    assert estimate["tier_discount"] == 0.0  # phone orders carry no tier discount


def test_the_operator_quote_before_a_method_is_the_catalogue(app, db, world, sample_product, operator):
    user, _address, _row = world
    estimate = _operator_estimate(app, operator, user.id, sample_product)
    assert Decimal(str(estimate["items"][0]["unit_price"])) == CATALOGUE
    assert estimate["items"][0]["is_contract_price"] is False


def test_the_operator_quote_and_the_phone_order_parse_the_method_alike(app, db, world, sample_product, operator):
    """One parse for both halves: "Cash" is the cash method on the quote AND on the order, so the
    figure read down the phone is the figure charged (both the contract cash price)."""
    user, address, _row = world
    estimate = _operator_estimate(app, operator, user.id, sample_product, "Cash")
    order = _operator_order(app, operator, user.id, sample_product, address, "Cash")

    assert order.payment_method == PaymentMethod.CASH
    assert Decimal(str(estimate["subtotal"])) == Decimal(str(order.subtotal)) == CASH * QUANTITY
    assert Decimal(str(estimate["total_amount"])) == Decimal(str(order.total_amount))
    assert estimate["items"][0]["is_contract_price"] is True


@pytest.mark.parametrize("raw", [["cash"], {"method": "cash"}], ids=["list", "dict"])
def test_the_operator_quote_on_a_non_string_method_is_the_catalogue(app, db, world, sample_product, operator, raw):
    """A JSON list or dict is not a payment method: the quote is the method-less catalogue figure, never a
    server error (it used to reach the price table as an unhashable key)."""
    user, _address, _row = world
    estimate = _operator_estimate(app, operator, user.id, sample_product, raw)
    assert Decimal(str(estimate["items"][0]["unit_price"])) == CATALOGUE
    assert estimate["items"][0]["is_contract_price"] is False
