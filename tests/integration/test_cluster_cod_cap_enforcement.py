from datetime import datetime, UTC
from decimal import Decimal

import pytest

from business_app import db as _db
from business_app.models.user import User
from business_app.models.order import Order
from business_app.models.payment import Payment
from business_app.models.customer_link import CanonicalCustomer
from business_app.services.cash_collection_service import CashCollectionService
from shared.enums import OrderStatus, PaymentMethod, PaymentStatus, UserRole, UserType
from business_app.utils.password_security import hash_password
from business_app.utils.exceptions import ValidationError


def _user(email, phone):
    u = User(email=email, phone=phone, password_hash=hash_password("TestPassword123!"),
             first_name="T", last_name="U", user_type=UserType.INDIVIDUAL,
             role=UserRole.CUSTOMER, is_verified=True, created_at=datetime.now(UTC))
    _db.session.add(u); _db.session.commit()
    return u


def _debt(user, n):
    order = Order(user_id=user.id, order_number=n, status=OrderStatus.DELIVERED,
                  subtotal=Decimal("15000"), delivery_fee=Decimal("0"), discount_amount=Decimal("0"),
                  loyalty_discount=Decimal("0"), total_amount=Decimal("15000"),
                  payment_method=PaymentMethod.CASH, created_at=datetime.now(UTC))
    _db.session.add(order); _db.session.flush()
    _db.session.add(Payment(order_id=order.id, user_id=user.id, payment_method=PaymentMethod.CASH,
                            amount=Decimal("15000"), currency="UZS", status=PaymentStatus.PENDING,
                            payment_id=f"pay_{n}", outstanding_amount=Decimal("15000"),
                            created_at=datetime.now(UTC)))
    _db.session.commit()


@pytest.mark.integration
def test_second_phone_cannot_use_cod_after_link(db):
    u1 = _user("a@example.com", "+998900000001")
    u2 = _user("b@example.com", "+998900000002")
    _debt(u1, "ORD-1")
    _debt(u2, "ORD-2")

    svc = CashCollectionService()
    # Before linking, each phone is under the cap on its own.
    assert svc.validate_customer_can_use_cod(u2.id)["cod_restricted"] is False

    # Link the two phones as one customer.
    canonical = CanonicalCustomer(primary_user_id=u1.id)
    _db.session.add(canonical); _db.session.commit()
    u1.canonical_customer_id = canonical.id
    u2.canonical_customer_id = canonical.id
    _db.session.commit()

    # Now the cluster is at the cap (2) — COD is refused on the second phone.
    with pytest.raises(ValidationError) as exc:
        svc.validate_customer_can_use_cod(u2.id)
    assert exc.value.error_code == "COD_DEBT_LIMIT_REACHED"
