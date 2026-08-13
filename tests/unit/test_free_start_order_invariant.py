"""INVARIANT (spec §6.4): the optimized sequence is ADVISORY. No code path may
reject a delivery status transition because the delivery is not first in
optimized_order. There is deliberately NO IN_TRANSIT -> ASSIGNED edge —
starting stop #4 simply makes #4 the committed stop and re-anchors the tail.

This test is a REGRESSION PIN: it passes today and must keep passing. If a
future change makes it fail, that change added a sequence-position guard the
design forbids."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.delivery import Delivery, DeliveryPerson, DeliveryRoute
from business_app.models.order import Order
from business_app.models.user import User, UserAddress
from business_app.services.route_optimization_service import RouteOptimizationService
from business_app.tasks.delivery_tasks import optimize_driver_route_task
from shared.enums import DeliveryStatus, OrderStatus, UserRole, UserType
from shared.status_transitions import DELIVERY_STATUS_TRANSITIONS


def _auth_headers(app, user_id: int) -> dict:
    with app.app_context():
        token = create_access_token(identity=str(user_id))
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


@pytest.fixture
def driver(db):
    user = User(
        email="free-driver@example.com",
        phone="+998900000101",
        password_hash="x",
        first_name="Free",
        last_name="Driver",
        user_type=UserType.STAFF,
        role=UserRole.DELIVERY_DRIVER,
        is_verified=True,
        telegram_id="777000101",
    )
    db.session.add(user)
    db.session.commit()
    person = DeliveryPerson(
        user_id=user.id,
        full_name="Free Driver",
        phone="+998900000101",
        current_location_lat=41.3000,
        current_location_lng=69.2500,
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
        email="free-cust@example.com",
        phone="+998900000102",
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


def _make_delivery(db, customer_id, driver_id, order_no, lat, lng):
    addr = UserAddress(
        user_id=customer_id,
        title="Stop",
        full_address=f"Stop {order_no}",
        street_address=f"Stop {order_no}",
        latitude=lat,
        longitude=lng,
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
        status=DeliveryStatus.ASSIGNED,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot="09:00-12:00",
    )
    db.session.add(delivery)
    db.session.commit()
    return delivery


def _haversine_matrix(self, points, traffic=True, use_cache=True):
    from business_app.utils.helpers import calculate_distance

    matrix = {}
    for i, pi in enumerate(points):
        for j, pj in enumerate(points):
            km = 0.0 if i == j else calculate_distance(pi[0], pi[1], pj[0], pj[1])
            matrix[(i, j)] = {"distance_km": km, "duration_minutes": km * 2.4}
    return matrix, "haversine"


@pytest.mark.unit
@pytest.mark.delivery
class TestFreeStartOrderInvariant:
    def test_starting_the_last_stop_of_the_route_is_accepted(
        self, app, client, db, driver, customer, monkeypatch
    ):
        """Driver starts stop #3 of 3 through the real endpoint: 200, it
        becomes the committed stop, and nothing sounded is sent."""
        d1 = _make_delivery(db, customer.id, driver.id, "ORD-F-1", 41.30, 69.26)
        d2 = _make_delivery(db, customer.id, driver.id, "ORD-F-2", 41.30, 69.29)
        d3 = _make_delivery(db, customer.id, driver.id, "ORD-F-3", 41.30, 69.33)
        route = DeliveryRoute(
            name="t",
            delivery_person_id=driver.id,
            start_location_lat=41.30,
            start_location_lng=69.25,
            route_date=datetime.now(UTC),
            optimized_order=[d1.id, d2.id, d3.id],
            status="planned",
        )
        db.session.add(route)
        db.session.commit()

        monkeypatch.setattr(
            "business_app.services.maps_service.MapsService.get_distance_matrix",
            _haversine_matrix,
        )
        monkeypatch.setattr(
            optimize_driver_route_task,
            "delay",
            lambda *a, **kw: optimize_driver_route_task.run(*a, **kw),
        )

        with patch(
            "business_app.utils.bot_webhook._send_staff_bot_webhook", return_value=True
        ) as hook:
            r1 = client.put(
                f"/api/v1/staff/delivery/{d3.id}/status",
                headers=_auth_headers(app, driver.id),
                json={"status": "picked_up"},
            )
            r2 = client.put(
                f"/api/v1/staff/delivery/{d3.id}/status",
                headers=_auth_headers(app, driver.id),
                json={"status": "in_transit"},
            )
        # THE INVARIANT: never rejected for being out of sequence.
        assert r1.status_code == 200
        assert r2.status_code == 200

        with app.app_context():
            committed = RouteOptimizationService().get_committed_stop(driver.id)
            assert committed is not None and committed.id == d3.id

        sounded = [
            c.args[1]
            for c in hook.call_args_list
            if c.args[0] == "/internal/route-updated" and c.args[1].get("sound")
        ]
        assert sounded == []

    def test_transition_map_itself_has_no_sequence_dimension(self):
        """DELIVERY_STATUS_TRANSITIONS keys on STATUS alone. If anyone adds a
        sequence-aware wrapper, the endpoint test above catches it; this one
        documents that the shared map must stay position-blind — and that
        there is no IN_TRANSIT -> ASSIGNED 'un-start' edge to lean on."""
        assert DELIVERY_STATUS_TRANSITIONS[DeliveryStatus.ASSIGNED] == [
            DeliveryStatus.PICKED_UP,
            DeliveryStatus.FAILED,
            DeliveryStatus.CANCELLED,
        ]
        assert DeliveryStatus.ASSIGNED not in DELIVERY_STATUS_TRANSITIONS[DeliveryStatus.IN_TRANSIT]
