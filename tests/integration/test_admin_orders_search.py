"""Integration tests for admin order/payment search + customer sort.

Regression for the ambiguous ``orders``/``payments`` -> ``users`` join:
``orders`` has two FKs to ``users`` (``user_id`` customer + ``created_by_staff_id``
call-center operator) and ``payments`` has two (``user_id`` + ``collected_by``),
so a bare ``query.join(User)`` raises
"Can't determine join between '<table>' and 'users'..." and the endpoint 500s.

These tests drive the real admin endpoints with a customer-name search and a
``sort_by=customer`` to lock the explicit-onclause fix.
"""

from datetime import UTC, datetime
from decimal import Decimal

from business_app import db as _db
from business_app.models.order import Order
from business_app.models.payment import Payment
from shared.enums import OrderStatus, PaymentMethod, PaymentStatus


def _seed_order(sample_user, admin_user):
    """Order with BOTH user FKs populated (customer + created-by-staff)."""
    order = Order(
        user_id=sample_user.id,
        created_by_staff_id=admin_user.id,
        order_number="ORD-SEARCH-001",
        status=OrderStatus.PENDING,
        subtotal=Decimal("15000.00"),
        delivery_fee=Decimal("3000.00"),
        discount_amount=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        total_amount=Decimal("18000.00"),
        created_at=datetime.now(UTC),
    )
    _db.session.add(order)
    _db.session.commit()
    return order


def test_admin_orders_search_by_customer_name_returns_200(
    client, db, admin_auth_headers, sample_user, admin_user
):
    order = _seed_order(sample_user, admin_user)  # sample_user last_name == "User"

    resp = client.get(
        "/api/v1/admin/orders?page=1&per_page=20&search=User&status=",
        headers=admin_auth_headers,
    )

    assert resp.status_code == 200
    numbers = [o["order_number"] for o in resp.get_json()["data"]["items"]]
    assert order.order_number in numbers


def test_admin_orders_search_non_matching_returns_empty(
    client, db, admin_auth_headers, sample_user, admin_user
):
    _seed_order(sample_user, admin_user)

    resp = client.get(
        "/api/v1/admin/orders?search=ZZZ_no_such_customer",
        headers=admin_auth_headers,
    )

    assert resp.status_code == 200
    assert resp.get_json()["data"]["items"] == []


def test_admin_orders_sort_by_customer_returns_200(
    client, db, admin_auth_headers, sample_user, admin_user
):
    _seed_order(sample_user, admin_user)

    resp = client.get(
        "/api/v1/admin/orders?sort_by=customer&sort_order=asc",
        headers=admin_auth_headers,
    )

    assert resp.status_code == 200


def test_admin_orders_search_and_sort_by_customer_returns_200(
    client, db, admin_auth_headers, sample_user, admin_user
):
    """search joins User AND sort_by=customer would join it again -> must not double-join."""
    order = _seed_order(sample_user, admin_user)

    resp = client.get(
        "/api/v1/admin/orders?search=User&sort_by=customer&sort_order=asc",
        headers=admin_auth_headers,
    )

    assert resp.status_code == 200
    numbers = [o["order_number"] for o in resp.get_json()["data"]["items"]]
    assert order.order_number in numbers


def test_admin_payments_search_by_customer_name_returns_200(
    client, db, admin_auth_headers, sample_user, admin_user
):
    order = _seed_order(sample_user, admin_user)
    payment = Payment(
        order_id=order.id,
        user_id=sample_user.id,
        collected_by=admin_user.id,  # second FK to users -> ambiguity
        payment_method=PaymentMethod.CASH,
        amount=order.total_amount,
        currency="UZS",
        status=PaymentStatus.COMPLETED,
        payment_id="pay_search_001",
        created_at=datetime.now(UTC),
    )
    _db.session.add(payment)
    _db.session.commit()

    resp = client.get(
        "/api/v1/admin/payments?page=1&per_page=20&search=User",
        headers=admin_auth_headers,
    )

    assert resp.status_code == 200
