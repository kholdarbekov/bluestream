"""GET /api/v1/staff/delivery/active must publish `route_summary` so the bot's
route card reads its numbers instead of deriving them (CLAUDE.md SSOT;
route-UX plan 2026-08-11 Phase 3 Task 1). Drives the REAL endpoint the staff
bot calls, with a driver JWT."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.delivery import Delivery, DeliveryPerson, DeliveryRoute
from business_app.models.order import Order
from business_app.models.user import User, UserAddress
from business_app.services.route_optimization_service import _driver_day_start_utc
from business_app.utils.timezone_utils import utc_to_local
from shared.constants import DISPLAY_TIMEZONE
from shared.enums import DeliveryStatus, OrderStatus, UserRole, UserType


def _auth_headers(app, user_id: int) -> dict:
    with app.app_context():
        token = create_access_token(identity=str(user_id))
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def driver(db):
    user = User(
        email="rs-driver@example.com",
        phone="+998900000061",
        password_hash="x",
        first_name="Route",
        last_name="Summary",
        user_type=UserType.STAFF,
        role=UserRole.DELIVERY_DRIVER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    # No location shared -> location_status == "missing" -> annotate makes no
    # maps call, so this test needs no MapsService monkeypatch.
    person = DeliveryPerson(
        user_id=user.id,
        full_name="Route Summary",
        phone="+998900000061",
        is_active=True,
        is_available=True,
    )
    db.session.add(person)
    db.session.commit()
    return user


@pytest.fixture
def customer(db):
    user = User(
        email="rs-cust@example.com",
        phone="+998900000062",
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


def _make_delivery(db, customer_id, driver_id, order_no, status, delivered_at=None):
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
    )
    db.session.add(order)
    db.session.flush()
    delivery = Delivery(
        order_id=order.id,
        delivery_person_id=driver_id,
        status=status,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot="09:00-12:00",
        delivered_at=delivered_at,
    )
    db.session.add(delivery)
    db.session.commit()
    return delivery


@pytest.mark.unit
@pytest.mark.delivery
class TestRouteSummaryField:
    def test_summary_counts_committed_and_finish_eta(self, app, client, db, driver, customer):
        active_a = _make_delivery(db, customer.id, driver.id, "ORD-RS-1", DeliveryStatus.IN_TRANSIT)
        _make_delivery(db, customer.id, driver.id, "ORD-RS-2", DeliveryStatus.ASSIGNED)
        # Clamped to the start of the driver's day: a bare `now - 1h` lands in
        # YESTERDAY for the first hour after midnight, which would zero out
        # stops_completed_today. Uses the production boundary so the two can
        # never disagree.
        _make_delivery(
            db, customer.id, driver.id, "ORD-RS-3", DeliveryStatus.DELIVERED,
            delivered_at=max(_driver_day_start_utc(), datetime.now(UTC) - timedelta(hours=1)),
        )
        solved_at = datetime.now(UTC) - timedelta(minutes=10)
        route = DeliveryRoute(
            name="test-route",
            delivery_person_id=driver.id,
            start_location_lat=41.30,
            start_location_lng=69.25,
            route_date=datetime.now(UTC),
            optimized_order=[active_a.id],
            estimated_duration_minutes=45,
            extra_data={
                "committed_delivery_id": active_a.id,
                "last_optimized_at": solved_at.isoformat(),
                "fallback": False,
            },
        )
        db.session.add(route)
        db.session.commit()

        resp = client.get("/api/v1/staff/delivery/active", headers=_auth_headers(app, driver.id))

        assert resp.status_code == 200
        data = resp.get_json()["data"]
        summary = data["route_summary"]
        assert summary["remaining"] == 2
        assert summary["stops_completed_today"] == 1
        assert summary["stops_total_today"] == 3
        assert summary["committed_delivery_id"] == active_a.id
        # The app's pre-existing, global TimezoneMiddleware.after_request
        # rewrites ANY response JSON key literally named "updated_at"
        # (recursively, any nesting depth) from UTC into DISPLAY_TIMEZONE
        # before the response leaves the process -- same instant, different
        # offset. It is unrelated to this task and applies identically to
        # every "updated_at" field in the whole API, so route_summary's is
        # no exception. "finish_eta" is not on that middleware's field list,
        # so it is untouched and stays UTC below. Reuse the exact production
        # helper the middleware calls rather than reimplementing the
        # conversion here.
        assert summary["updated_at"] == utc_to_local(solved_at, DISPLAY_TIMEZONE).isoformat()
        expected_finish = solved_at + timedelta(minutes=45)
        assert summary["finish_eta"] == expected_finish.isoformat()

    def test_haversine_fallback_suppresses_finish_eta(self, app, client, db, driver, customer):
        active = _make_delivery(db, customer.id, driver.id, "ORD-RS-4", DeliveryStatus.ASSIGNED)
        route = DeliveryRoute(
            name="test-route-hv",
            delivery_person_id=driver.id,
            start_location_lat=41.30,
            start_location_lng=69.25,
            route_date=datetime.now(UTC),
            optimized_order=[active.id],
            estimated_duration_minutes=45,
            extra_data={
                "committed_delivery_id": None,
                "last_optimized_at": datetime.now(UTC).isoformat(),
                "fallback": True,
            },
        )
        db.session.add(route)
        db.session.commit()

        resp = client.get("/api/v1/staff/delivery/active", headers=_auth_headers(app, driver.id))

        assert resp.status_code == 200
        summary = resp.get_json()["data"]["route_summary"]
        assert summary["finish_eta"] is None
        assert summary["committed_delivery_id"] is None
        assert summary["remaining"] == 1

    def test_no_route_row_yields_null_optional_fields(self, app, client, db, driver, customer):
        _make_delivery(db, customer.id, driver.id, "ORD-RS-5", DeliveryStatus.ASSIGNED)

        resp = client.get("/api/v1/staff/delivery/active", headers=_auth_headers(app, driver.id))

        assert resp.status_code == 200
        summary = resp.get_json()["data"]["route_summary"]
        assert summary == {
            "remaining": 1,
            "stops_completed_today": 0,
            "stops_total_today": 1,
            "committed_delivery_id": None,
            "finish_eta": None,
            "updated_at": None,
        }

    def test_yesterdays_delivered_not_counted(self, app, client, db, driver, customer):
        _make_delivery(db, customer.id, driver.id, "ORD-RS-6", DeliveryStatus.ASSIGNED)
        _make_delivery(
            db, customer.id, driver.id, "ORD-RS-7", DeliveryStatus.DELIVERED,
            delivered_at=datetime.now(UTC) - timedelta(days=2),
        )

        resp = client.get("/api/v1/staff/delivery/active", headers=_auth_headers(app, driver.id))

        summary = resp.get_json()["data"]["route_summary"]
        assert summary["stops_completed_today"] == 0
        assert summary["stops_total_today"] == 1
