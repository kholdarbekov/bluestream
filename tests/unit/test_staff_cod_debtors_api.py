"""Route tests for GET /api/v1/staff/customers/with-open-cod.

Auth contract via `require_staff_roles("delivery_driver", "operator")`;
payload parity with the admin with-open-cod list, plus pagination metadata.
"""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.order import Order
from business_app.models.user import User
from business_app.services.cash_collection_service import CashCollectionService
from business_app.utils.password_security import hash_password
from shared.enums import OrderStatus, PaymentMethod, UserRole, UserType


def _auth_headers(app, user_id: int) -> dict:
    with app.app_context():
        token = create_access_token(identity=str(user_id))
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _make_debtor(db, *, email, phone, name, amount):
    user = User(
        email=email,
        phone=phone,
        password_hash=hash_password('DebtorPassword123!'),
        first_name=name,
        last_name='Debtor',
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        is_verified=True,
        created_at=datetime.now(UTC),
    )
    db.session.add(user)
    db.session.flush()
    order = Order(
        user_id=user.id,
        order_number=f"ORD-API-DEBT-{name.upper()}-{uuid4().hex[:8]}",
        status=OrderStatus.DELIVERED,
        subtotal=Decimal(amount),
        delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        total_amount=Decimal(amount),
        payment_method=PaymentMethod.CASH,
        created_at=datetime.now(UTC),
    )
    db.session.add(order)
    db.session.flush()
    CashCollectionService().ensure_cod_payment_for_order(order)
    db.session.commit()
    return user


@pytest.mark.unit
class TestListCustomersWithOpenCod:
    def test_requires_auth(self, app):
        # The session-scoped `client` fixture can carry a JWT cookie left by
        # earlier web-login tests (JWT_TOKEN_LOCATION includes cookies), which
        # would authenticate this request. A fresh client guarantees no auth.
        response = app.test_client().get("/api/v1/staff/customers/with-open-cod")
        assert response.status_code == 401

    def test_rejects_non_staff_roles(self, app, client, db, sample_user):
        response = client.get(
            "/api/v1/staff/customers/with-open-cod",
            headers=_auth_headers(app, sample_user.id),
        )
        assert response.status_code == 403

    def test_driver_gets_paginated_debtors_sorted_by_outstanding(self, app, client, db, delivery_driver):
        big = _make_debtor(db, email='cod.big@example.com', phone='+998900000201',
                           name='Big', amount='90000.00')
        _make_debtor(db, email='cod.small@example.com', phone='+998900000202',
                     name='Small', amount='40000.00')

        response = client.get(
            "/api/v1/staff/customers/with-open-cod?page=1&per_page=1",
            headers=_auth_headers(app, delivery_driver.id),
        )
        assert response.status_code == 200
        data = response.get_json()["data"]
        assert [item["id"] for item in data["items"]] == [big.id]
        assert data["pagination"] == {"page": 1, "per_page": 1, "total": 2, "pages": 2}
        row = data["items"][0]
        assert row["first_name"] == "Big"
        assert row["total_outstanding_amount"] == 90000.0
        assert row["active_cod_debt_count"] == 1

    def test_defaults_to_page_1_per_page_10(self, app, client, db, delivery_driver):
        _make_debtor(db, email='cod.one@example.com', phone='+998900000203',
                     name='One', amount='12000.00')

        response = client.get(
            "/api/v1/staff/customers/with-open-cod",
            headers=_auth_headers(app, delivery_driver.id),
        )
        assert response.status_code == 200
        pagination = response.get_json()["data"]["pagination"]
        assert pagination["page"] == 1
        assert pagination["per_page"] == 10
