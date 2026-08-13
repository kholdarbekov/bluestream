"""§10 fix: annotate_active_items must read TODAY's route (same UTC-midnight
scope as current_route/_upsert_route), never yesterday's sequence."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from business_app.models.delivery import Delivery, DeliveryPerson, DeliveryRoute
from business_app.models.order import Order
from business_app.models.user import User, UserAddress
from business_app.services.route_optimization_service import RouteOptimizationService
from shared.enums import DeliveryStatus, OrderStatus, UserRole, UserType


@pytest.fixture
def driver(db):
    user = User(
        email="ann-driver@example.com",
        phone="+998900000141",
        password_hash="x",
        first_name="Ann",
        last_name="Driver",
        user_type=UserType.STAFF,
        role=UserRole.DELIVERY_DRIVER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    person = DeliveryPerson(
        user_id=user.id,
        full_name="Ann Driver",
        phone="+998900000141",
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
        email="ann-cust@example.com",
        phone="+998900000142",
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


@pytest.mark.unit
@pytest.mark.delivery
class TestAnnotateDateFilter:
    def test_yesterdays_route_is_ignored(self, app, db, driver, customer, monkeypatch):
        d1 = _make_delivery(db, customer.id, driver.id, "ORD-AN-1", 41.30, 69.26)
        d2 = _make_delivery(db, customer.id, driver.id, "ORD-AN-2", 41.30, 69.33)
        yesterday = datetime.now(UTC) - timedelta(days=1)
        stale = DeliveryRoute(
            name="stale",
            delivery_person_id=driver.id,
            start_location_lat=41.30,
            start_location_lng=69.25,
            route_date=yesterday,
            optimized_order=[d2.id, d1.id],  # yesterday said far-first
            status="planned",
        )
        db.session.add(stale)
        db.session.commit()
        # created_at is auto-stamped 'now'; force it into the past so the
        # stale row is genuinely the newest-by-date-but-old-by-route_date row.
        stale.created_at = yesterday
        db.session.commit()

        monkeypatch.setattr(
            "business_app.services.maps_service.MapsService.get_distance_matrix",
            lambda self, points, traffic=True, use_cache=True: (
                {(0, 1): {"distance_km": 1.0, "duration_minutes": 2.0},
                 (1, 0): {"distance_km": 1.0, "duration_minutes": 2.0},
                 (0, 0): {"distance_km": 0.0, "duration_minutes": 0.0},
                 (1, 1): {"distance_km": 0.0, "duration_minutes": 0.0}},
                "haversine",
            ),
        )

        items = [
            {"delivery_id": d1.id, "destination_latitude": 41.30, "destination_longitude": 69.26},
            {"delivery_id": d2.id, "destination_latitude": 41.30, "destination_longitude": 69.33},
        ]
        with app.app_context():
            out = RouteOptimizationService().annotate_active_items(driver.id, items)
        # No TODAY route -> fallback ordering by delivery_id, positions None.
        assert [it["delivery_id"] for it in out] == [d1.id, d2.id]
        assert out[0]["route_position"] is None
        assert out[1]["route_position"] is None

    def test_todays_route_still_applies(self, app, db, driver, customer, monkeypatch):
        d1 = _make_delivery(db, customer.id, driver.id, "ORD-AN-3", 41.30, 69.26)
        d2 = _make_delivery(db, customer.id, driver.id, "ORD-AN-4", 41.30, 69.33)
        route = DeliveryRoute(
            name="today",
            delivery_person_id=driver.id,
            start_location_lat=41.30,
            start_location_lng=69.25,
            route_date=datetime.now(UTC),
            optimized_order=[d2.id, d1.id],
            status="planned",
        )
        db.session.add(route)
        db.session.commit()
        monkeypatch.setattr(
            "business_app.services.maps_service.MapsService.get_distance_matrix",
            lambda self, points, traffic=True, use_cache=True: (
                {(0, 1): {"distance_km": 1.0, "duration_minutes": 2.0},
                 (1, 0): {"distance_km": 1.0, "duration_minutes": 2.0},
                 (0, 0): {"distance_km": 0.0, "duration_minutes": 0.0},
                 (1, 1): {"distance_km": 0.0, "duration_minutes": 0.0}},
                "haversine",
            ),
        )
        items = [
            {"delivery_id": d1.id, "destination_latitude": 41.30, "destination_longitude": 69.26},
            {"delivery_id": d2.id, "destination_latitude": 41.30, "destination_longitude": 69.33},
        ]
        with app.app_context():
            out = RouteOptimizationService().annotate_active_items(driver.id, items)
        assert [it["delivery_id"] for it in out] == [d2.id, d1.id]
        assert out[0]["route_position"] == 0
        assert out[0]["is_next"] is True
