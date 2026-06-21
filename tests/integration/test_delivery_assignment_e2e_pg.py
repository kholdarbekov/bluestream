"""Postgres end-to-end tests where the REAL ARCH-006 CHECK constraint fires.

The SQLite suite silently ignores the migration-only CHECK constraint
``ck_deliveries_person_required_after_assigned``:

    status NOT IN (assigned, picked_up, in_transit, arrived, delivered)
        OR delivery_person_id IS NOT NULL

so the original prod bug — committing a row with ``status='assigned'`` and
``delivery_person_id=NULL`` — passed 4,000+ green tests but exploded in prod
with a ``CheckViolation`` -> ``IntegrityError`` on commit.

These tests run against a real, fully-migrated Postgres database (``pg_db``):

* Positive: ``assign_delivery_driver`` + COMMIT succeeds, person column set,
  no IntegrityError (the fixed happy path).
* Negative: directly flipping a delivery to ASSIGNED with a NULL person and
  committing raises ``IntegrityError`` — the EXACT prod failure the fix
  prevents. Proves the constraint is genuinely enforced (the safety net the
  SQLite suite can never exercise).
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.order import Order
from business_app.models.user import User, UserAddress
from business_app.services.delivery_service import DeliveryService
from shared.enums import DeliveryStatus, OrderStatus, UserRole, UserType


def _make_driver(pg_db, *, email, phone, role=UserRole.DELIVERY_DRIVER):
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
    pg_db.session.add(user)
    pg_db.session.commit()
    person = DeliveryPerson(
        user_id=user.id,
        full_name="Drv R",
        phone=phone,
        is_active=True,
        is_available=True,
        working_hours_start="00:00",
        working_hours_end="23:59",
    )
    pg_db.session.add(person)
    pg_db.session.commit()
    return user


def _make_scheduled_delivery(pg_db, *, n=1):
    customer = User(
        email=f"cust-pg-{n}@example.com",
        phone=f"+99890900{n:04d}",
        password_hash="x",
        first_name="C",
        last_name="U",
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        is_verified=True,
    )
    pg_db.session.add(customer)
    pg_db.session.commit()
    addr = UserAddress(
        user_id=customer.id,
        title="h",
        full_address="9 Pg St",
        street_address="9 Pg St",
        latitude=41.3,
        longitude=69.25,
    )
    pg_db.session.add(addr)
    pg_db.session.flush()
    order = Order(
        user_id=customer.id,
        order_number=f"ORD-PG-{n}",
        status=OrderStatus.CONFIRMED,
        subtotal=Decimal("0"),
        total_amount=Decimal("0"),
        delivery_address_id=addr.id,
    )
    pg_db.session.add(order)
    pg_db.session.flush()
    delivery = Delivery(
        order_id=order.id,
        status=DeliveryStatus.SCHEDULED,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot="09:00-12:00",
    )
    pg_db.session.add(delivery)
    pg_db.session.commit()
    return delivery


@pytest.mark.integration
@pytest.mark.delivery
class TestDeliveryAssignmentCheckConstraintPg:
    def test_assign_then_commit_satisfies_constraint(self, pg_app, pg_db):
        """The fixed path: assign_delivery_driver sets delivery_person_id BEFORE
        flipping to ASSIGNED, so the COMMIT passes the real CHECK constraint."""
        driver = _make_driver(pg_db, email="pgd1@x.com", phone="+998909111101")
        delivery = _make_scheduled_delivery(pg_db, n=1)

        DeliveryService().assign_delivery_driver(delivery.id, driver.id)
        # assign_delivery_driver commits internally; a fresh read must reflect it.
        pg_db.session.expire_all()

        refreshed = Delivery.query.get(delivery.id)
        assert refreshed.status == DeliveryStatus.ASSIGNED
        assert refreshed.delivery_person_id == driver.id

    def test_assigned_with_null_person_commit_raises_integrity_error(self, pg_app, pg_db):
        """The EXACT prod bug: status flipped to ASSIGNED while
        delivery_person_id stays NULL (what the phantom ``driver_id`` write
        produced). The real CHECK constraint must reject the COMMIT."""
        delivery = _make_scheduled_delivery(pg_db, n=2)

        # Simulate the silent no-op: status advances, person column stays NULL.
        delivery.status = DeliveryStatus.ASSIGNED
        delivery.delivery_person_id = None

        with pytest.raises(IntegrityError) as exc_info:
            pg_db.session.commit()

        # The specific named CHECK constraint must be the cause.
        assert "ck_deliveries_person_required_after_assigned" in str(exc_info.value)
        pg_db.session.rollback()

    def test_in_transit_with_null_person_commit_raises_integrity_error(self, pg_app, pg_db):
        """The constraint covers every active state from ASSIGNED onward, not
        just ASSIGNED — IN_TRANSIT with a NULL person is equally rejected."""
        delivery = _make_scheduled_delivery(pg_db, n=3)

        delivery.status = DeliveryStatus.IN_TRANSIT
        delivery.delivery_person_id = None

        with pytest.raises(IntegrityError) as exc_info:
            pg_db.session.commit()

        assert "ck_deliveries_person_required_after_assigned" in str(exc_info.value)
        pg_db.session.rollback()

    def test_scheduled_with_null_person_is_allowed(self, pg_app, pg_db):
        """Sanity: a SCHEDULED (pool) delivery legitimately has NULL person and
        must commit cleanly — the constraint only bites from ASSIGNED onward."""
        delivery = _make_scheduled_delivery(pg_db, n=4)

        # Re-commit an untouched scheduled row — no constraint should fire.
        delivery.scheduled_time_slot = "12:00-15:00"
        pg_db.session.commit()

        refreshed = Delivery.query.get(delivery.id)
        assert refreshed.status == DeliveryStatus.SCHEDULED
        assert refreshed.delivery_person_id is None
