"""Revenue of a courtesy line is its charged total, on the admin dashboard and in the agent pay basis (spec 2026-11-16 §5.7).

A courtesy line stores `unit_price x quantity != total_price` (4 x 20,000 charged as 76,000). The dashboard's
top-products revenue and the agent pay basis must both read the stored `total_price`, never re-multiply.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from business_app import db as _db
from business_app.models.order import Order, OrderItem
from business_app.services.sales.pay_rules import basis_inputs, basis_snapshot
from shared.enums import OrderStatus, PaymentMethod

pytestmark = pytest.mark.integration


@pytest.fixture
def courtesy_order(db, sample_user, sample_product):
    """A CONFIRMED order created now: 4 x 20,000 with 2 units at the old 18,000, charged 76,000."""
    sample_product.base_price = Decimal("20000.00")
    order = Order(
        user_id=sample_user.id,
        status=OrderStatus.CONFIRMED,
        subtotal=Decimal("76000.00"),
        discount_amount=Decimal("0.00"),
        delivery_fee=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        tier_discount=Decimal("0.00"),
        total_amount=Decimal("76000.00"),
        payment_method=PaymentMethod.CASH,
        is_paid=False,
        created_at=datetime.now(UTC),
    )
    _db.session.add(order)
    _db.session.flush()
    _db.session.add(
        OrderItem.from_priced_line(
            order.id,
            {
                "product_id": sample_product.id,
                "quantity": 4,
                "unit_price": Decimal("20000.00"),
                "discount_amount": Decimal("4000.00"),
                "total_price": Decimal("76000.00"),
                "courtesy_quantity": 2,
                "courtesy_unit_price": Decimal("18000.00"),
            },
        )
    )
    _db.session.commit()
    return order


def test_top_products_revenue_is_the_charged_line_total(
    client, admin_claim_headers, courtesy_order, sample_product
):
    resp = client.get("/api/v1/admin/dashboard?period=month", headers=admin_claim_headers)

    assert resp.status_code == 200, resp.get_json()
    rows = resp.get_json()["data"]["dashboard"]["top_performers"]["products"]
    row = next(r for r in rows if r["product_id"] == sample_product.id)
    assert (row["units_sold"], row["revenue"]) == (4, 76000.0)


def test_the_agent_pay_basis_of_a_courtesy_order_is_its_charged_total(db, courtesy_order):
    """A pure-function guard on the frozen basis: no route publishes the basis before a credit, so it is
    built here from the live order exactly as the first credit builds it. It passes today; it fails if a
    re-multiplied line total ever reaches the basis."""
    order = Order.query.get(courtesy_order.id)

    snapshot = basis_snapshot(*basis_inputs(order))

    assert snapshot["gross"] == "76000.00"
    assert snapshot["items"][0]["total_price"] == snapshot["items"][0]["net"] == "76000.00"
