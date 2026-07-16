from datetime import datetime, timezone
from decimal import Decimal

import pytest
from flask_jwt_extended import create_access_token

from business_app import db
from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.payment import Payment
from business_app.models.user import User
from business_app.services.cash_collection_service import CashCollectionService
from business_app.utils.password_security import hash_password
from shared.enums import DeliveryStatus, OrderStatus, PaymentMethod, UserRole, UserType


def _headers(app, user_id):
    with app.app_context():
        token = create_access_token(identity=str(user_id))
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _make_staff(db, role, email):
    u = User(
        email=email, phone=f"+9989011{role.value[:5]:0>5}", password_hash=hash_password("Passw0rd!"),
        first_name=role.value, last_name="Staff", user_type=UserType.STAFF, role=role, is_verified=True,
    )
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def seeded_cod(db, sample_order, sample_user, delivery_driver):
    sample_order.status = OrderStatus.DELIVERED
    sample_order.payment_method = PaymentMethod.CASH
    sample_order.total_amount = Decimal("54000")
    sample_order.paid_at = datetime.now(timezone.utc)
    db.session.add(DeliveryPerson(
        user_id=delivery_driver.id, full_name="D", phone=delivery_driver.phone,
        email=delivery_driver.email, is_active=True, is_available=True))
    delivery = Delivery(
        order_id=sample_order.id, delivery_person_id=delivery_driver.id, status=DeliveryStatus.DELIVERED,
        scheduled_date=datetime.now(timezone.utc), scheduled_time_slot="09:00-12:00",
        actual_delivery_time=datetime.now(timezone.utc))
    db.session.add(delivery)
    db.session.commit()
    svc = CashCollectionService()
    svc.ensure_cod_payment_for_order(sample_order)
    svc.post_collection(
        customer_id=sample_user.id, amount=Decimal("54000"), source="delivery_completion",
        collector_user_id=delivery_driver.id, recorded_by_user_id=delivery_driver.id,
        order_id=sample_order.id, delivery_id=delivery.id, notes="seed")
    return sample_order


def test_manager_and_operator_forbidden(app, client, db, seeded_cod):
    manager = _make_staff(db, UserRole.MANAGER, "mgr.cash@example.com")
    operator = _make_staff(db, UserRole.OPERATOR, "op.cash@example.com")
    body = {"new_amount": 60000, "reason": "manager attempt"}
    for user in (manager, operator):
        r = client.post(f"/api/v1/admin/orders/{seeded_cod.id}/collected-cash",
                        json=body, headers=_headers(app, user.id))
        assert r.status_code == 403


def test_admin_preview_then_apply(app, client, db, admin_user, sample_user, seeded_cod):
    preview = client.post(f"/api/v1/admin/orders/{seeded_cod.id}/collected-cash/preview",
                          json={"new_amount": 60000}, headers=_headers(app, admin_user.id))
    assert preview.status_code == 200
    data = preview.get_json()["data"]
    assert data["applied_to_order"] == 54000
    assert data["customer_credit_delta"] == 6000
    assert data["is_editable"] is True

    apply = client.post(f"/api/v1/admin/orders/{seeded_cod.id}/collected-cash",
                        json={"new_amount": 60000, "reason": "driver collected 60k"},
                        headers=_headers(app, admin_user.id))
    assert apply.status_code == 200
    apply_body = apply.get_json()["data"]
    assert apply_body["order_id"] == seeded_cod.id
    assert apply_body["replacement_event_id"] is not None
    assert apply_body["summary"]["applied_to_order"] == 54000
    assert isinstance(apply_body["warnings"], list)
    payment = Payment.query.filter_by(order_id=seeded_cod.id).first()
    assert Decimal(str(payment.amount_collected)) == Decimal("54000")
    assert CashCollectionService().get_customer_prepaid_balance(sample_user.id) == Decimal("6000")


def test_admin_apply_requires_reason(app, client, db, admin_user, seeded_cod):
    r = client.post(f"/api/v1/admin/orders/{seeded_cod.id}/collected-cash",
                    json={"new_amount": 60000, "reason": "x"}, headers=_headers(app, admin_user.id))
    assert r.status_code == 400
