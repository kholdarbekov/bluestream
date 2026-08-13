"""ARRIVED must no longer enqueue route optimization (spec §5.3).

The driver is standing at the customer's door; a re-solve from that doorstep
churns the plan and (pre-gate) pushed a useless 'Route updated' message.
DELIVERED keeps its trigger: the next leg starts from the drop point.
Drives the REAL endpoint the staff bot calls: PUT /api/v1/staff/delivery/<id>/status."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.order import Order
from business_app.models.user import User, UserAddress
from business_app.services.delivery_service import DeliveryService
from shared.enums import DeliveryStatus, OrderStatus, UserRole, UserType


def _auth_headers(app, user_id: int) -> dict:
    with app.app_context():
        token = create_access_token(identity=str(user_id))
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


@pytest.fixture
def driver(db):
    user = User(
        email="arr-driver@example.com",
        phone="+998900000021",
        password_hash="x",
        first_name="Arr",
        last_name="Driver",
        user_type=UserType.STAFF,
        role=UserRole.DELIVERY_DRIVER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    person = DeliveryPerson(
        user_id=user.id,
        full_name="Arr Driver",
        phone="+998900000021",
        current_location_lat=41.30,
        current_location_lng=69.25,
        last_location_update=datetime.now(UTC),
        is_active=True,
        is_available=True,
    )
    db.session.add(person)
    db.session.commit()
    return user


@pytest.fixture
def customer(db):
    user = User(
        email="arr-cust@example.com",
        phone="+998900000022",
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


def _make_delivery(db, customer_id, driver_id, order_no, status):
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
        status=status,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot="09:00-12:00",
    )
    db.session.add(delivery)
    db.session.commit()
    return delivery


@pytest.mark.unit
@pytest.mark.delivery
class TestArrivedTriggerRemoved:
    def test_put_status_arrived_does_not_enqueue_optimization(
        self, app, client, db, driver, customer
    ):
        delivery = _make_delivery(
            db, customer.id, driver.id, "ORD-ARR-1", DeliveryStatus.IN_TRANSIT
        )
        with patch(
            "business_app.tasks.delivery_tasks.optimize_driver_route_task.delay"
        ) as delay:
            resp = client.put(
                f"/api/v1/staff/delivery/{delivery.id}/status",
                headers=_auth_headers(app, driver.id),
                json={"status": "arrived"},
            )
        assert resp.status_code == 200
        delay.assert_not_called()

    def test_put_status_delivered_still_enqueues_with_delivery_trigger(
        self, app, client, db, driver, customer
    ):
        delivery = _make_delivery(
            db, customer.id, driver.id, "ORD-ARR-2", DeliveryStatus.ARRIVED
        )
        with patch(
            "business_app.tasks.delivery_tasks.optimize_driver_route_task.delay"
        ) as delay:
            resp = client.put(
                f"/api/v1/staff/delivery/{delivery.id}/status",
                headers=_auth_headers(app, driver.id),
                json={"status": "delivered"},
            )
        assert resp.status_code == 200
        delay.assert_called_once_with(driver.id, "delivery")

    def test_mark_delivery_arrived_service_does_not_enqueue(
        self, app, db, driver, customer
    ):
        delivery = _make_delivery(
            db, customer.id, driver.id, "ORD-ARR-3", DeliveryStatus.IN_TRANSIT
        )
        with app.app_context():
            with patch(
                "business_app.tasks.delivery_tasks.optimize_driver_route_task.delay"
            ) as delay:
                DeliveryService().mark_delivery_arrived(
                    delivery.id, actor_user_id=driver.id
                )
        delay.assert_not_called()
