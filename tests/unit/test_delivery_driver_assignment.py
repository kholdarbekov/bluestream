"""Regression: DeliveryService.assign_delivery_driver must persist the driver.

Two prod bugs (both surfaced via auto_assign_delivery_task):

1. The method wrote ``delivery.driver_id = driver_id`` — but ``driver_id`` is
   NOT a mapped column (the real column is ``delivery_person_id``). The write
   was silently dropped, so the row committed with status='assigned' and
   delivery_person_id=NULL, violating ck_deliveries_person_required_after_assigned
   on Postgres (CheckViolation → IntegrityError).

2. The driver was validated with ``User.query.filter_by(role='delivery_driver')``,
   but driver identity is the DeliveryPerson profile (User.role has drifted), so
   valid drivers raised NotFoundError('Driver not found').
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.order import Order
from business_app.models.user import User, UserAddress
from business_app.services.delivery_service import DeliveryService
from shared.enums import DeliveryStatus, OrderStatus, UserRole, UserType


def _make_driver(db, *, role, email, phone):
    user = User(
        email=email,
        phone=phone,
        password_hash="x",
        first_name="Drv",
        last_name="R",
        user_type=UserType.STAFF,
        role=role,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    person = DeliveryPerson(
        user_id=user.id,
        full_name="Drv R",
        phone=phone,
        is_active=True,
        is_available=True,
        working_hours_start="00:00",
        working_hours_end="23:59",
    )
    db.session.add(person)
    db.session.commit()
    return user


def _make_scheduled_delivery(db):
    customer = User(
        email="cust-assign@example.com",
        phone="+998907000011",
        password_hash="x",
        first_name="C",
        last_name="U",
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        is_verified=True,
    )
    db.session.add(customer)
    db.session.commit()
    addr = UserAddress(
        user_id=customer.id, title="h", full_address="h", street_address="h",
        latitude=41.3, longitude=69.25,
    )
    db.session.add(addr)
    db.session.flush()
    order = Order(
        user_id=customer.id, order_number="ORD-ASSIGN-1", status=OrderStatus.CONFIRMED,
        subtotal=Decimal("0"), total_amount=Decimal("0"), delivery_address_id=addr.id,
    )
    db.session.add(order)
    db.session.flush()
    delivery = Delivery(
        order_id=order.id, status=DeliveryStatus.SCHEDULED,
        scheduled_date=datetime.now(UTC), scheduled_time_slot="09:00-12:00",
    )
    db.session.add(delivery)
    db.session.commit()
    return delivery


@pytest.mark.unit
@pytest.mark.delivery
class TestAssignDeliveryDriver:
    def test_persists_delivery_person_id_when_assigned(self, app, db):
        with app.app_context():
            driver = _make_driver(db, role=UserRole.DELIVERY_DRIVER,
                                  email="d1@example.com", phone="+998901111101")
            delivery = _make_scheduled_delivery(db)

            DeliveryService().assign_delivery_driver(delivery.id, driver.id)

            refreshed = Delivery.query.get(delivery.id)
            # The mapped FK column must be set so status='assigned' has a person.
            assert refreshed.delivery_person_id == driver.id
            assert refreshed.status == DeliveryStatus.ASSIGNED

    def test_accepts_driver_identified_by_delivery_person_profile(self, app, db):
        """A driver whose User.role isn't the singular DELIVERY_DRIVER but who
        has an active DeliveryPerson profile must still be assignable."""
        with app.app_context():
            # role drift: an operator who also drives (canonical identity is the
            # DeliveryPerson profile, created in _make_driver).
            driver = _make_driver(db, role=UserRole.OPERATOR,
                                  email="d2@example.com", phone="+998901111102")
            delivery = _make_scheduled_delivery(db)

            DeliveryService().assign_delivery_driver(delivery.id, driver.id)

            refreshed = Delivery.query.get(delivery.id)
            assert refreshed.delivery_person_id == driver.id
            assert refreshed.status == DeliveryStatus.ASSIGNED
