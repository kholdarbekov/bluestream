"""Order creation writes every line through the one line-money shape (price-change plan, Task 3).

Behaviour-neutral with no courtesy: the three route tests pin today's figures over the real
routes with the bodies the clients send; the factory test pins the plumbing Part C relies on.
"""

from decimal import Decimal

import pytest

from business_app import db as _db
from business_app.models.order import Order, OrderItem
from shared.enums import UserRole
from tests.integration.contract_pricing_factory import (
    QUANTITY,
    admin_headers,
    customer_headers,
    make_staff,
    make_workplace,
)
from tests.integration.tier_discount_factory import post_order, verify_phone

pytestmark = pytest.mark.integration


def _stored_line(order_id):
    _db.session.expire_all()
    order = Order.query.get(order_id)
    (line,) = order.order_items
    return order, line


def test_a_web_order_line_is_unit_times_quantity_with_no_courtesy(app, db, sample_user, sample_product, user_address):
    verify_phone(db, sample_user)

    resp = post_order(
        app,
        customer_headers(app, sample_user),
        product_id=sample_product.id,
        address_id=user_address.id,
        payment_method="cash",
        quantity=3,
    )

    assert resp.status_code == 201, resp.get_data(as_text=True)
    _order, line = _stored_line(resp.get_json()["data"]["order"]["id"])
    assert (line.unit_price, line.discount_amount, line.total_price) == (
        Decimal("15000.00"),
        Decimal("0.00"),
        Decimal("45000.00"),
    )
    assert (line.courtesy_quantity, line.courtesy_unit_price) == (None, None)


def test_an_admin_order_line_is_unit_times_quantity_with_no_courtesy(
    app, db, admin_user, sample_user, sample_product, user_address
):
    resp = app.test_client().post(
        "/api/v1/admin/orders",
        json={
            "user_id": sample_user.id,
            "delivery_address_id": user_address.id,
            "payment_method": "cash",
            "items": [{"product_id": sample_product.id, "quantity": 2}],
        },
        headers=admin_headers(app, admin_user),
    )

    assert resp.status_code == 201, resp.get_data(as_text=True)
    data = resp.get_json()["data"]
    order_id = (data.get("order") or data)["id"]
    order, line = _stored_line(order_id)
    assert order.total_amount == Decimal("30000.00")
    assert (line.unit_price, line.discount_amount, line.total_price) == (
        Decimal("15000.00"),
        Decimal("0.00"),
        Decimal("30000.00"),
    )
    assert (line.courtesy_quantity, line.courtesy_unit_price) == (None, None)


def test_the_phone_order_line_equals_the_operator_quote(app, db, sample_product):
    operator = make_staff(db, UserRole.OPERATOR, "+998901119913")
    client, address = make_workplace(db)
    body = {"items": [{"product_id": sample_product.id, "quantity": QUANTITY}]}

    estimate_resp = app.test_client().post(
        f"/api/v1/staff/operator/users/{client.id}/order-estimate",
        json={**body, "payment_method": "cash"},
        headers=customer_headers(app, operator),
    )
    assert estimate_resp.status_code == 200, estimate_resp.get_data(as_text=True)
    estimate = estimate_resp.get_json()["data"]

    order_resp = app.test_client().post(
        "/api/v1/staff/operator/orders",
        json={
            "client_id": client.id,
            **body,
            "delivery_address_id": address.id,
            "payment_method": "cash",
            "delivery_notes": "",
        },
        headers=customer_headers(app, operator),
    )
    assert order_resp.status_code == 201, order_resp.get_data(as_text=True)
    order, line = _stored_line(order_resp.get_json()["data"]["id"])

    assert estimate["items"][0]["total_price"] == 30000.0
    assert (line.discount_amount, line.total_price, line.courtesy_quantity) == (
        Decimal("0.00"),
        Decimal("30000.00"),
        None,
    )
    assert estimate["total_amount"] == float(order.total_amount)


def test_a_priced_line_becomes_a_row_with_its_courtesy():
    line = {
        "product_id": 5,
        "contract_id": None,
        "contract_product_price_id": None,
        "quantity": 4,
        "unit_price": Decimal("20000.00"),
        "discount_amount": Decimal("4000.00"),
        "total_price": Decimal("76000.00"),
        "courtesy_quantity": 2,
        "courtesy_unit_price": Decimal("18000.00"),
        "contract_price_row": None,
    }

    row = OrderItem.from_priced_line(11, line)

    assert (
        row.order_id,
        row.product_id,
        row.quantity,
        row.unit_price,
        row.discount_amount,
        row.total_price,
        row.courtesy_quantity,
        row.courtesy_unit_price,
    ) == (11, 5, 4, Decimal("20000.00"), Decimal("4000.00"), Decimal("76000.00"), 2, Decimal("18000.00"))


def test_a_plain_priced_line_defaults_to_no_discount_and_no_courtesy():
    row = OrderItem.from_priced_line(
        11,
        {"product_id": 5, "quantity": 2, "unit_price": Decimal("15000.00"), "total_price": Decimal("30000.00")},
    )

    assert (
        row.discount_amount,
        row.courtesy_quantity,
        row.courtesy_unit_price,
        row.contract_product_price_id,
    ) == (Decimal("0.00"), None, None, None)
