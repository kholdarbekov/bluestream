"""Starting a stop (picked_up / in_transit) must enqueue a re-optimization so
the tail re-anchors on the newly committed stop — silently, because the
driver caused it (spec §5.2 row 4). Drives the REAL endpoint the staff bot
calls: PUT /api/v1/staff/delivery/<id>/status."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.order import Order
from business_app.models.user import User, UserAddress
from business_app.tasks.delivery_tasks import optimize_driver_route_task
from shared.enums import DeliveryStatus, OrderStatus, UserRole, UserType


def _auth_headers(app, user_id: int) -> dict:
    with app.app_context():
        token = create_access_token(identity=str(user_id))
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


@pytest.fixture
def driver(db):
    user = User(
        email="commit-trg-driver@example.com",
        phone="+998900000091",
        password_hash="x",
        first_name="Trg",
        last_name="Driver",
        user_type=UserType.STAFF,
        role=UserRole.DELIVERY_DRIVER,
        is_verified=True,
        telegram_id="777000091",
    )
    db.session.add(user)
    db.session.commit()
    person = DeliveryPerson(
        user_id=user.id,
        full_name="Trg Driver",
        phone="+998900000091",
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
        email="commit-trg-cust@example.com",
        phone="+998900000092",
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


def _make_delivery(db, customer_id, driver_id, order_no, lat, lng, status):
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
class TestCommitTriggers:
    def test_picked_up_enqueues_with_picked_up_trigger(self, app, client, db, driver, customer):
        delivery = _make_delivery(
            db, customer.id, driver.id, "ORD-CT-1", 41.31, 69.27, DeliveryStatus.ASSIGNED
        )
        with patch(
            "business_app.tasks.delivery_tasks.optimize_driver_route_task.delay"
        ) as delay:
            resp = client.put(
                f"/api/v1/staff/delivery/{delivery.id}/status",
                headers=_auth_headers(app, driver.id),
                json={"status": "picked_up"},
            )
        assert resp.status_code == 200
        delay.assert_called_once_with(driver.id, "picked_up")

    def test_in_transit_enqueues_with_in_transit_trigger(self, app, client, db, driver, customer):
        delivery = _make_delivery(
            db, customer.id, driver.id, "ORD-CT-2", 41.31, 69.27, DeliveryStatus.PICKED_UP
        )
        with patch(
            "business_app.tasks.delivery_tasks.optimize_driver_route_task.delay"
        ) as delay:
            resp = client.put(
                f"/api/v1/staff/delivery/{delivery.id}/status",
                headers=_auth_headers(app, driver.id),
                json={"status": "in_transit"},
            )
        assert resp.status_code == 200
        delay.assert_called_once_with(driver.id, "in_transit")

    def test_end_to_end_in_transit_reanchors_silently(
        self, app, client, db, driver, customer, monkeypatch
    ):
        """Full chain: endpoint -> task -> optimizer -> webhook. Starting the
        FAR stop makes it the committed anchor; the near stop moves into the
        tail; the webhook is SILENT (driver-initiated)."""
        far = _make_delivery(
            db, customer.id, driver.id, "ORD-CT-3", 41.300, 69.330, DeliveryStatus.PICKED_UP
        )
        near = _make_delivery(
            db, customer.id, driver.id, "ORD-CT-4", 41.3005, 69.2505, DeliveryStatus.ASSIGNED
        )
        monkeypatch.setattr(
            "business_app.services.maps_service.MapsService.get_distance_matrix",
            _haversine_matrix,
        )
        # Run the enqueued task inline so the whole chain executes.
        monkeypatch.setattr(
            optimize_driver_route_task,
            "delay",
            lambda *a, **kw: optimize_driver_route_task.run(*a, **kw),
        )
        with patch(
            "business_app.utils.bot_webhook._send_staff_bot_webhook", return_value=True
        ) as hook:
            resp = client.put(
                f"/api/v1/staff/delivery/{far.id}/status",
                headers=_auth_headers(app, driver.id),
                json={"status": "in_transit"},
            )
        assert resp.status_code == 200

        from business_app.models.delivery import DeliveryRoute

        route = (
            DeliveryRoute.query.filter_by(delivery_person_id=driver.id)
            .order_by(DeliveryRoute.created_at.desc())
            .first()
        )
        assert route.optimized_order[0] == far.id
        assert (route.extra_data or {}).get("start_source") == "committed_stop"

        payloads = [
            c.args[1]
            for c in hook.call_args_list
            if c.args[0] == "/internal/route-updated"
        ]
        assert len(payloads) == 1
        assert payloads[0]["sound"] is False
        assert payloads[0]["driver_initiated"] is True
        assert payloads[0]["trigger"] == "in_transit"
