"""POST /api/v1/staff/delivery/<id>/location must only accept the assigned driver.

Before this fix any authenticated driver token could write any delivery's
location, and the service mirrored the coordinates onto the ASSIGNED driver's
DeliveryPerson row — poisoning that driver's route start point (spec §10,
'Handled separately — security')."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.order import Order
from business_app.models.user import User, UserAddress
from shared.enums import DeliveryStatus, OrderStatus, UserRole, UserType


def _auth_headers(app, user_id: int) -> dict:
    with app.app_context():
        token = create_access_token(identity=str(user_id))
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _make_driver(db, email, phone):
    user = User(
        email=email,
        phone=phone,
        password_hash="x",
        first_name="D",
        last_name="River",
        user_type=UserType.STAFF,
        role=UserRole.DELIVERY_DRIVER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    person = DeliveryPerson(
        user_id=user.id,
        full_name="D River",
        phone=phone,
        current_location_lat=41.30,
        current_location_lng=69.25,
        last_location_update=datetime.now(UTC),
        is_active=True,
        is_available=True,
    )
    db.session.add(person)
    db.session.commit()
    return user, person


@pytest.fixture
def owner_driver(db):
    return _make_driver(db, "owner-drv@example.com", "+998900000011")


@pytest.fixture
def foreign_driver(db):
    return _make_driver(db, "foreign-drv@example.com", "+998900000012")


@pytest.fixture
def customer(db):
    user = User(
        email="loc-cust@example.com",
        phone="+998900000013",
        password_hash="x",
        first_name="C",
        last_name="",
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


def _make_delivery(db, customer_id, driver_id, order_no):
    addr = UserAddress(
        user_id=customer_id,
        title="Stop",
        full_address=f"Stop {order_no}",
        street_address=f"Stop {order_no}",
        latitude=41.31,
        longitude=69.27,
    )
    db.session.add(addr)
    db.session.flush()
    order = Order(
        user_id=customer_id,
        order_number=order_no,
        status=OrderStatus.CONFIRMED,
        subtotal=Decimal("10000"),
        total_amount=Decimal("10000"),
        delivery_address_id=addr.id,
        delivery_date=datetime.now(UTC) + timedelta(hours=2),
        delivery_time_slot="09:00-12:00",
    )
    db.session.add(order)
    db.session.flush()
    delivery = Delivery(
        order_id=order.id,
        delivery_person_id=driver_id,
        status=DeliveryStatus.IN_TRANSIT,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot="09:00-12:00",
    )
    db.session.add(delivery)
    db.session.commit()
    return delivery


@pytest.mark.unit
@pytest.mark.delivery
class TestDeliveryLocationOwnership:
    def test_foreign_driver_gets_404_and_owner_location_untouched(
        self, app, client, db, owner_driver, foreign_driver, customer
    ):
        owner_user, owner_person = owner_driver
        foreign_user, _ = foreign_driver
        delivery = _make_delivery(db, customer.id, owner_user.id, "ORD-OWN-1")
        before_lat = owner_person.current_location_lat
        before_ts = owner_person.last_location_update

        resp = client.post(
            f"/api/v1/staff/delivery/{delivery.id}/location",
            headers=_auth_headers(app, foreign_user.id),
            json={"latitude": 40.0, "longitude": 68.0},
        )

        assert resp.status_code == 404
        assert resp.get_json()["error_code"] == "STAFF_DELIVERY_NOT_FOUND"
        db.session.refresh(owner_person)
        db.session.refresh(delivery)
        # The poisoning write must not have happened on either row.
        assert owner_person.current_location_lat == before_lat
        assert owner_person.last_location_update == before_ts
        assert delivery.current_location_lat is None

    def test_assigned_driver_can_still_write_location(
        self, app, client, db, owner_driver, customer
    ):
        owner_user, owner_person = owner_driver
        delivery = _make_delivery(db, customer.id, owner_user.id, "ORD-OWN-2")

        resp = client.post(
            f"/api/v1/staff/delivery/{delivery.id}/location",
            headers=_auth_headers(app, owner_user.id),
            json={"latitude": 41.3333, "longitude": 69.2222},
        )

        assert resp.status_code == 200
        db.session.refresh(delivery)
        db.session.refresh(owner_person)
        assert delivery.current_location_lat == 41.3333
        assert delivery.current_location_lng == 69.2222
        assert owner_person.current_location_lat == 41.3333

    def test_unassigned_delivery_is_404_for_any_driver(
        self, app, client, db, owner_driver, customer
    ):
        owner_user, _ = owner_driver
        delivery = _make_delivery(db, customer.id, None, "ORD-POOL-1")

        resp = client.post(
            f"/api/v1/staff/delivery/{delivery.id}/location",
            headers=_auth_headers(app, owner_user.id),
            json={"latitude": 41.31, "longitude": 69.26},
        )

        assert resp.status_code == 404
        assert resp.get_json()["error_code"] == "STAFF_DELIVERY_NOT_FOUND"
