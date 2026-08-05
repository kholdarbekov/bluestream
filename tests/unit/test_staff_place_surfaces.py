"""Plan 2c / Task 3 — staff-facing PLACE surfaces.

Driver-facing reads that must become place-aware once a debt can belong to a
*place* (an ownerless address group spanning different customers):

  * ``_place_cod_context`` — the place COD block spread into every
    ``GET /staff/delivery/active`` card,
  * the union fields on ``GET /staff/bottles/customer/<id>/addresses``.

Regression baseline: an ungrouped address must produce all-falsy/zero fields so
today's payloads are unchanged apart from the constant-false additions.
"""

from datetime import datetime, UTC
from decimal import Decimal

import pytest

from business_app.api.staff import _place_cod_context
from business_app.models.user import User, UserAddress
from business_app.models.order import Order
from business_app.models.payment import Payment
from business_app.services.cash_collection_service import CashCollectionService
from business_app.services.customer_link_service import CustomerLinkService
from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.utils.password_security import hash_password
from shared.enums import BottleLedgerEventType, OrderStatus, PaymentMethod, PaymentStatus, UserRole, UserType

LAT, LNG = 41.3111, 69.2797


def _user(db, email, phone):
    u = User(email=email, phone=phone, password_hash=hash_password("TestPassword123!"),
             first_name="T", last_name=email.split("@")[0], user_type=UserType.INDIVIDUAL,
             role=UserRole.CUSTOMER, is_verified=True, created_at=datetime.now(UTC))
    db.session.add(u)
    db.session.commit()
    return u


def _address(db, user):
    a = UserAddress(user_id=user.id, title="work", full_address="Office",
                    latitude=LAT, longitude=LNG)
    db.session.add(a)
    db.session.commit()
    return a


def _delivered_cod_debt(db, user, order_number, *, address, outstanding=Decimal("15000.00")):
    order = Order(user_id=user.id, order_number=order_number, status=OrderStatus.DELIVERED,
                  subtotal=outstanding, delivery_fee=Decimal("0.00"),
                  discount_amount=Decimal("0.00"), loyalty_discount=Decimal("0.00"),
                  total_amount=outstanding, payment_method=PaymentMethod.CASH,
                  delivery_address_id=address.id, created_at=datetime.now(UTC))
    db.session.add(order)
    db.session.flush()
    payment = Payment(order_id=order.id, user_id=user.id, payment_method=PaymentMethod.CASH,
                      amount=outstanding, currency="UZS", status=PaymentStatus.PENDING,
                      payment_id=f"pay_{order_number}", outstanding_amount=outstanding,
                      created_at=datetime.now(UTC))
    db.session.add(payment)
    db.session.commit()
    return order


@pytest.mark.unit
class TestPlaceCodContext:
    def test_grouped_address_reports_place_totals(self, db):
        admin = _user(db, "adm@example.com", "+998900000009")
        u1 = _user(db, "a@example.com", "+998900000001")
        u2 = _user(db, "b@example.com", "+998900000002")
        a1, a2 = _address(db, u1), _address(db, u2)
        group = CustomerLinkService().create_place_group(
            [a1.id, a2.id], acting_admin_id=admin.id, reason="office", label="Acme office")
        order = _delivered_cod_debt(db, u1, "ORD-1", address=a1)
        _delivered_cod_debt(db, u2, "ORD-2", address=a2, outstanding=Decimal("20000.00"))
        ctx = _place_cod_context(order)
        assert ctx["is_place_grouped"] is True
        assert ctx["place_group_id"] == group.id
        assert ctx["place_group_label"] == "Acme office"
        assert ctx["place_outstanding_cod_total"] == 35000.0
        assert ctx["place_active_cod_debt_count"] == 2

    def test_ungrouped_address_is_all_zero(self, db):
        u = _user(db, "solo@example.com", "+998900000007")
        a = _address(db, u)
        order = _delivered_cod_debt(db, u, "ORD-9", address=a)
        ctx = _place_cod_context(order)
        assert ctx == {
            "is_place_grouped": False, "place_group_id": None, "place_group_label": None,
            "place_outstanding_cod_total": 0.0, "place_active_cod_debt_count": 0,
        }

    def test_no_order_or_address_is_safe(self, db):
        assert _place_cod_context(None)["is_place_grouped"] is False

    def test_helper_delegates_to_the_service(self, db):
        """The API helper must stay a thin adapter — the lookup lives in the
        service layer, so the two must agree for the same address."""
        admin = _user(db, "adm@example.com", "+998900000009")
        u1 = _user(db, "a@example.com", "+998900000001")
        u2 = _user(db, "b@example.com", "+998900000002")
        a1, a2 = _address(db, u1), _address(db, u2)
        CustomerLinkService().create_place_group(
            [a1.id, a2.id], acting_admin_id=admin.id, reason="office", label="Acme office")
        order = _delivered_cod_debt(db, u1, "ORD-1", address=a1)
        assert _place_cod_context(order) == CashCollectionService().get_place_cod_context(a1.id)
        # The address-less arm degrades to the same all-zero shape.
        assert CashCollectionService().get_place_cod_context(None) == _place_cod_context(None)


@pytest.mark.unit
class TestBottleAddressesUnionFields:
    def test_addresses_payload_gains_union_fields(self, db, client):
        # Exercised at the service level the endpoint composes from: the
        # endpoint loops get_customer_scopes and reads address.address_group_id.
        admin = _user(db, "adm@example.com", "+998900000009")
        u1 = _user(db, "a@example.com", "+998900000001")
        u2 = _user(db, "b@example.com", "+998900000002")
        a1, a2 = _address(db, u1), _address(db, u2)
        CustomerLinkService().create_place_group(
            [a1.id, a2.id], acting_admin_id=admin.id, reason="office")
        svc = BottleTrackingService()
        # A place is one pool and can only be seeded once (BOTTLE_INITIAL_BALANCE_EXISTS
        # guard); u2's entry is a second movement on the same place, not a second seed.
        svc.set_initial_balance(u1.id, a1.id, Decimal("2"), actor_user_id=admin.id)
        svc._create_ledger_entry(user_id=u2.id, address_id=a2.id,
                                 event_type=BottleLedgerEventType.DELIVERY, quantity=Decimal("3"))
        # get_group_union_balance is gone (Task 6); the place-scoped truth is
        # get_place_balance — one row per place, not a per-address union.
        assert float(svc.get_place_balance(a1.id)) == 5.0
