"""Route tests for admin order payment-method edit endpoints (Admin-only)."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.corporate import (
    CorporateContract,
    CorporateContractProductPrice,
    CorporateContractStatus,
    CorporatePrepaymentAccount,
    CorporatePrepaymentBalance,
)
from business_app.models.order import Order, OrderItem
from business_app.models.user import User
from business_app.utils.password_security import hash_password
from shared.enums import (
    CorporateContractTrackingMode,
    EntitySubtype,
    OrderStatus,
    PaymentMethod,
    UserRole,
    UserType,
)


def _headers(app, user_id):
    with app.app_context():
        token = create_access_token(identity=str(user_id))
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _make_staff(db, role, email):
    u = User(
        email=email,
        phone=f"+9989013{role.value[:5]:0>5}",
        password_hash=hash_password("Passw0rd!"),
        first_name=role.value,
        last_name="Staff",
        user_type=UserType.STAFF,
        role=role,
        is_verified=True,
    )
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def workplace_user(db):
    user = User(
        email=f"wp-{uuid4().hex[:8]}@example.com",
        phone=f"+99895{uuid4().int % 10000000:07d}",
        password_hash="x" * 60,
        first_name="Work",
        last_name="Place",
        user_type=UserType.ENTITY,
        entity_subtype=EntitySubtype.WORKPLACE,
        company_name="Test Workplace",
        role=UserRole.CUSTOMER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def covered_contract(db, workplace_user, sample_product):
    """Workplace entity + active contract + prepaid balance covering sample_product."""
    contract = CorporateContract(
        user_id=workplace_user.id,
        contract_number=f"C-{uuid4().hex[:10]}",
        name="Coverage Contract",
        status=CorporateContractStatus.ACTIVE,
        start_date=datetime.now(timezone.utc) - timedelta(days=1),
        currency="UZS",
        is_active=True,
        tracking_mode=CorporateContractTrackingMode.UNITS,
    )
    db.session.add(contract)
    db.session.flush()
    price_row = CorporateContractProductPrice(
        contract_id=contract.id,
        product_id=sample_product.id,
        unit_price=Decimal("18000.00"),
        is_prepayment_eligible=True,
        is_active=True,
    )
    db.session.add(price_row)
    account = CorporatePrepaymentAccount(contract_id=contract.id, is_active=True)
    db.session.add(account)
    db.session.flush()
    balance = CorporatePrepaymentBalance(
        account_id=account.id,
        product_id=sample_product.id,
        prepaid_units=Decimal("50.00"),
        reserved_units=Decimal("0.00"),
        consumed_units=Decimal("0.00"),
        is_active=True,
    )
    db.session.add(balance)
    db.session.commit()
    return contract, price_row, account, balance


@pytest.fixture
def qualifying_order(db, workplace_user, sample_product, covered_contract):
    """Delivered cash order whose cart qualifies for business_account settlement."""
    contract, price_row, account, balance = covered_contract
    order = Order(
        user_id=workplace_user.id,
        order_number=f"ORD-{uuid4().hex[:10]}",
        status=OrderStatus.DELIVERED,
        subtotal=Decimal("36000.00"),
        delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        total_amount=Decimal("36000.00"),
        payment_method=PaymentMethod.CASH,
    )
    db.session.add(order)
    db.session.flush()
    item = OrderItem(
        order_id=order.id,
        product_id=sample_product.id,
        contract_id=contract.id,
        contract_product_price_id=price_row.id,
        quantity=2,
        unit_price=Decimal("18000.00"),
        total_price=Decimal("36000.00"),
    )
    db.session.add(item)
    db.session.commit()
    return order


def test_manager_and_operator_forbidden(app, client, db, qualifying_order):
    manager = _make_staff(db, UserRole.MANAGER, "mgr.pm@example.com")
    operator = _make_staff(db, UserRole.OPERATOR, "op.pm@example.com")
    for user in (manager, operator):
        preview = client.post(
            f"/api/v1/admin/orders/{qualifying_order.id}/payment-method/preview",
            json={"new_method": "business_account"},
            headers=_headers(app, user.id),
        )
        assert preview.status_code == 403

        apply = client.post(
            f"/api/v1/admin/orders/{qualifying_order.id}/payment-method",
            json={"new_method": "business_account", "reason": "manager attempt"},
            headers=_headers(app, user.id),
        )
        assert apply.status_code == 403


def test_admin_preview_then_apply_cash_to_business_account(app, client, db, admin_user, qualifying_order):
    preview = client.post(
        f"/api/v1/admin/orders/{qualifying_order.id}/payment-method/preview",
        json={"new_method": "business_account"},
        headers=_headers(app, admin_user.id),
    )
    assert preview.status_code == 200
    data = preview.get_json()["data"]
    assert data["order_id"] == qualifying_order.id
    assert data["current_method"] == "cash"
    assert data["new_method"] == "business_account"
    assert data["is_delivered"] is True
    assert data["blocking_reasons"] == []

    apply = client.post(
        f"/api/v1/admin/orders/{qualifying_order.id}/payment-method",
        json={"new_method": "business_account", "reason": "reclassify to business account"},
        headers=_headers(app, admin_user.id),
    )
    assert apply.status_code == 200
    body = apply.get_json()["data"]
    assert body["order_id"] == qualifying_order.id
    assert body["new_method"] == "business_account"
    assert body["corporate_action"] == "settled_business_account"
    assert isinstance(body["warnings"], list)
    assert "payment_link" not in body

    db.session.expire_all()
    order = Order.query.get(qualifying_order.id)
    assert order.payment_method == PaymentMethod.BUSINESS_ACCOUNT
    assert order.is_paid is True


def test_preview_requires_new_method(app, client, db, admin_user, qualifying_order):
    r = client.post(
        f"/api/v1/admin/orders/{qualifying_order.id}/payment-method/preview",
        json={},
        headers=_headers(app, admin_user.id),
    )
    assert r.status_code == 400


def test_apply_requires_new_method(app, client, db, admin_user, qualifying_order):
    r = client.post(
        f"/api/v1/admin/orders/{qualifying_order.id}/payment-method",
        json={"reason": "reclassify to business account"},
        headers=_headers(app, admin_user.id),
    )
    assert r.status_code == 400


def test_apply_requires_reason_min_length(app, client, db, admin_user, qualifying_order):
    r = client.post(
        f"/api/v1/admin/orders/{qualifying_order.id}/payment-method",
        json={"new_method": "business_account", "reason": "no"},
        headers=_headers(app, admin_user.id),
    )
    assert r.status_code == 400


def test_preview_not_found_for_unknown_order(app, client, db, admin_user):
    r = client.post(
        "/api/v1/admin/orders/999999999/payment-method/preview",
        json={"new_method": "business_account"},
        headers=_headers(app, admin_user.id),
    )
    assert r.status_code == 404
