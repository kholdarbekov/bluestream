from datetime import datetime, UTC
from decimal import Decimal

import pytest

from business_app.models.user import User
from business_app.models.order import Order
from business_app.models.payment import Payment
from business_app.models.customer_link import CanonicalCustomer
from business_app.services.cash_collection_service import CashCollectionService
from shared.enums import OrderStatus, PaymentMethod, PaymentStatus, UserRole, UserType
from business_app.utils.password_security import hash_password


def _user(db, email, phone, *, exempt=False):
    u = User(email=email, phone=phone, password_hash=hash_password("TestPassword123!"),
             first_name="T", last_name="U", user_type=UserType.INDIVIDUAL,
             role=UserRole.CUSTOMER, is_verified=True, cod_debt_check_exempt=exempt,
             created_at=datetime.now(UTC))
    db.session.add(u); db.session.commit()
    return u


def _delivered_cod_debt(db, user, order_number, outstanding=Decimal("15000.00")):
    order = Order(user_id=user.id, order_number=order_number, status=OrderStatus.DELIVERED,
                  subtotal=Decimal("15000.00"), delivery_fee=Decimal("0.00"),
                  discount_amount=Decimal("0.00"), loyalty_discount=Decimal("0.00"),
                  total_amount=Decimal("15000.00"), payment_method=PaymentMethod.CASH,
                  created_at=datetime.now(UTC))
    db.session.add(order); db.session.flush()
    payment = Payment(order_id=order.id, user_id=user.id, payment_method=PaymentMethod.CASH,
                      amount=Decimal("15000.00"), currency="UZS", status=PaymentStatus.PENDING,
                      payment_id=f"pay_{order_number}", outstanding_amount=outstanding,
                      created_at=datetime.now(UTC))
    db.session.add(payment); db.session.commit()
    return order, payment


def _link(db, users):
    canonical = CanonicalCustomer(primary_user_id=users[0].id)
    db.session.add(canonical); db.session.commit()
    for u in users:
        u.canonical_customer_id = canonical.id
    db.session.commit()
    return canonical


@pytest.mark.unit
class TestClusterCodCap:
    def test_unlinked_user_one_debt_not_restricted(self, db):
        u = _user(db, "a@example.com", "+998900000001")
        _delivered_cod_debt(db, u, "ORD-1")
        svc = CashCollectionService()
        assert svc.get_cluster_active_cod_debt_count(u.id) == 1
        assert svc.is_customer_cod_restricted(u.id) is False

    def test_two_linked_phones_one_debt_each_hits_cap(self, db):
        u1 = _user(db, "a@example.com", "+998900000001")
        u2 = _user(db, "b@example.com", "+998900000002")
        _delivered_cod_debt(db, u1, "ORD-1")
        _delivered_cod_debt(db, u2, "ORD-2")
        _link(db, [u1, u2])
        svc = CashCollectionService()
        # Cluster count is 2 (== COD_ACTIVE_DEBT_LIMIT) from EITHER phone.
        assert svc.get_cluster_active_cod_debt_count(u1.id) == 2
        assert svc.is_customer_cod_restricted(u2.id) is True

    def test_or_exemption_across_cluster(self, db):
        u1 = _user(db, "a@example.com", "+998900000001", exempt=True)
        u2 = _user(db, "b@example.com", "+998900000002")
        _delivered_cod_debt(db, u1, "ORD-1")
        _delivered_cod_debt(db, u2, "ORD-2")
        _link(db, [u1, u2])
        svc = CashCollectionService()
        # One exempt member exempts the whole cluster.
        assert svc.is_customer_cod_restricted(u2.id) is False
        ctx = svc.get_cod_restriction_context(u2.id)
        assert ctx["cod_restricted"] is False
        assert ctx["cod_exempt"] is True
        assert ctx["active_cod_debt_count"] == 2
