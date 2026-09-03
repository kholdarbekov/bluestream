"""The admin order payloads must publish every money line, on both surfaces.

Orders.js renders subtotal / subscription discount / reward discount / tier
discount / delivery fee / total. Every one of those figures has to arrive from
the backend: the modal must never subtract one number from another to guess a
missing line. It also has to be BOTH endpoints — the detail payload replaces
the list row inside `selectedOrder` halfway through opening the modal, so a
field present on only one of them makes the breakdown flicker or blank out.
"""

from decimal import Decimal

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.order import Order
from shared.enums import OrderStatus


def _auth_headers(app, user_id: int) -> dict:
    """Admin routes read the role CLAIM; the shared admin_auth_headers fixture
    carries none and 403s."""
    with app.app_context():
        token = create_access_token(identity=str(user_id), additional_claims={"role": "admin"})
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


@pytest.fixture
def discounted_order(db, sample_user):
    """One order carrying all three discount kinds at once — they stack."""
    order = Order(
        user_id=sample_user.id,
        order_number="ORD-TIER-BREAKDOWN",
        status=OrderStatus.PENDING,
        subtotal=Decimal("36000.00"),
        discount_amount=Decimal("1000.00"),
        loyalty_discount=Decimal("2000.00"),
        tier_discount=Decimal("720.00"),
        delivery_fee=Decimal("0.00"),
        total_amount=Decimal("32280.00"),
    )
    db.session.add(order)
    db.session.commit()
    return order


@pytest.mark.unit
def test_admin_orders_list_publishes_every_money_line(client, app, admin_user, discounted_order):
    resp = client.get("/api/v1/admin/orders?per_page=100", headers=_auth_headers(app, admin_user.id))

    assert resp.status_code == 200
    rows = resp.get_json()["data"]["items"]
    row = next(r for r in rows if r["order_number"] == "ORD-TIER-BREAKDOWN")
    assert row["subtotal"] == 36000.0
    assert row["discount_amount"] == 1000.0
    assert row["loyalty_discount"] == 2000.0
    assert row["tier_discount"] == 720.0
    assert row["delivery_fee"] == 0.0
    assert row["total_amount"] == 32280.0


@pytest.mark.unit
def test_admin_order_detail_publishes_every_money_line(client, app, admin_user, discounted_order):
    resp = client.get(
        f"/api/v1/admin/orders/{discounted_order.id}", headers=_auth_headers(app, admin_user.id)
    )

    assert resp.status_code == 200
    order = resp.get_json()["data"]["order"]
    assert order["subtotal"] == 36000.0
    assert order["discount_amount"] == 1000.0
    assert order["loyalty_discount"] == 2000.0
    assert order["tier_discount"] == 720.0
    assert order["delivery_fee"] == 0.0
    assert order["total_amount"] == 32280.0


@pytest.mark.unit
def test_money_lines_are_present_and_zero_on_an_undiscounted_order(
    client, app, admin_user, sample_order
):
    """The keys must exist unconditionally. Orders.js hides a discount ROW at
    zero, but it reads the key to decide — an absent key reads as NaN."""
    resp = client.get(
        f"/api/v1/admin/orders/{sample_order.id}", headers=_auth_headers(app, admin_user.id)
    )

    assert resp.status_code == 200
    order = resp.get_json()["data"]["order"]
    assert order["tier_discount"] == 0.0
    assert order["loyalty_discount"] == 0.0
    assert order["subtotal"] == 15000.0
