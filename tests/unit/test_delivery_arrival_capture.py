"""Unit tests for DeliveryService._capture_arrival_position and the
implicit driver-location refresh on ARRIVED transitions.

These exercise the behavior wired into `mark_delivery_arrived`:
  - The destination coords are stamped onto DeliveryStatusHistory.location_*
    so the route-optimizer's `last_completed` fallback finds them.
  - DeliveryPerson.current_location_* is refreshed when the live GPS is
    stale or missing, but preserved when it's still fresh.
  - optimize_driver_route_task.delay is enqueued with trigger="arrival".
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest

from business_app.models.delivery import (
    Delivery,
    DeliveryPerson,
    DeliveryStatusHistory,
)
from business_app.models.order import Order
from business_app.models.user import User, UserAddress
from business_app.services.delivery_service import DeliveryService
from shared.enums import DeliveryStatus, OrderStatus, UserRole, UserType


# ---------------------------------------------------------------------------
# Fixtures (mirroring tests/unit/test_route_optimization_service.py style)
# ---------------------------------------------------------------------------


@pytest.fixture
def driver_user(db):
    user = User(
        email="arrival-driver@example.com",
        phone="+998901112255",
        password_hash="x",
        first_name="Arrival",
        last_name="Driver",
        user_type=UserType.STAFF,
        role=UserRole.DELIVERY_DRIVER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def customer_user(db):
    user = User(
        email="arrival-customer@example.com",
        phone="+998907654399",
        password_hash="x",
        first_name="Arr",
        last_name="Customer",
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def driver_with_stale_location(db, driver_user):
    """Driver whose live location is over an hour old at a far-away point."""
    person = DeliveryPerson(
        user_id=driver_user.id,
        full_name="Arrival Driver",
        phone="+998901112255",
        current_location_lat=41.0000,
        current_location_lng=69.0000,
        last_location_update=datetime.now(UTC) - timedelta(hours=2),
        is_active=True,
        is_available=True,
    )
    db.session.add(person)
    db.session.commit()
    return person


@pytest.fixture
def driver_with_fresh_location(db, driver_user):
    """Driver whose live GPS reading is recent (within freshness threshold)."""
    person = DeliveryPerson(
        user_id=driver_user.id,
        full_name="Arrival Driver",
        phone="+998901112255",
        current_location_lat=41.2700,
        current_location_lng=69.2200,
        last_location_update=datetime.now(UTC) - timedelta(minutes=5),
        is_active=True,
        is_available=True,
    )
    db.session.add(person)
    db.session.commit()
    return person


def _make_address(db, user_id, lat, lng, label="Dropoff"):
    addr = UserAddress(
        user_id=user_id,
        title=label,
        full_address=f"{label} address",
        street_address=label,
        latitude=lat,
        longitude=lng,
    )
    db.session.add(addr)
    db.session.flush()
    return addr


def _make_in_transit_delivery(db, customer_id, driver_id, address):
    """Create an order + delivery in IN_TRANSIT state so it can be marked arrived."""
    order = Order(
        user_id=customer_id,
        order_number=f"ORD-ARR-{address.id}",
        status=OrderStatus.CONFIRMED,
        subtotal=Decimal("10000"),
        delivery_fee=Decimal("0"),
        total_amount=Decimal("10000"),
        delivery_address_id=address.id,
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.delivery
class TestArrivalPositionCapture:
    """Behavior of DeliveryService._capture_arrival_position via mark_delivery_arrived."""

    def test_mark_arrived_stamps_history_location(
        self, app, db, driver_user, customer_user, driver_with_stale_location
    ):
        """DeliveryStatusHistory rows for ARRIVED carry the destination coords."""
        with app.app_context():
            dest_lat, dest_lng = 41.3123, 69.2456
            addr = _make_address(db, customer_user.id, dest_lat, dest_lng)
            delivery = _make_in_transit_delivery(db, customer_user.id, driver_user.id, addr)
            with patch(
                "business_app.tasks.delivery_tasks.optimize_driver_route_task.delay"
            ):
                DeliveryService().mark_delivery_arrived(
                    delivery.id, actor_user_id=driver_user.id
                )

            history = (
                DeliveryStatusHistory.query.filter_by(delivery_id=delivery.id)
                .order_by(DeliveryStatusHistory.changed_at.desc())
                .first()
            )
            assert history is not None
            assert history.new_status == DeliveryStatus.ARRIVED
            assert history.location_lat == pytest.approx(dest_lat)
            assert history.location_lng == pytest.approx(dest_lng)

    def test_mark_arrived_refreshes_stale_driver_location(
        self, app, db, driver_user, customer_user, driver_with_stale_location
    ):
        """Stale DeliveryPerson location is updated to the destination coords."""
        with app.app_context():
            dest_lat, dest_lng = 41.3123, 69.2456
            addr = _make_address(db, customer_user.id, dest_lat, dest_lng)
            delivery = _make_in_transit_delivery(db, customer_user.id, driver_user.id, addr)
            before = datetime.now(UTC)
            with patch(
                "business_app.tasks.delivery_tasks.optimize_driver_route_task.delay"
            ):
                DeliveryService().mark_delivery_arrived(
                    delivery.id, actor_user_id=driver_user.id
                )

            db.session.expire_all()
            person = DeliveryPerson.query.filter_by(user_id=driver_user.id).first()
            assert person.current_location_lat == pytest.approx(dest_lat)
            assert person.current_location_lng == pytest.approx(dest_lng)
            last_update = person.last_location_update
            if last_update is not None and last_update.tzinfo is None:
                last_update = last_update.replace(tzinfo=UTC)
            assert last_update is not None and last_update >= before

    def test_mark_arrived_preserves_fresh_gps(
        self, app, db, driver_user, customer_user, driver_with_fresh_location
    ):
        """Fresh GPS is more accurate than the address centroid; don't overwrite it.

        History row is still stamped to the destination — that's the canonical
        record of where this delivery happened.
        """
        with app.app_context():
            original_lat = driver_with_fresh_location.current_location_lat
            original_lng = driver_with_fresh_location.current_location_lng

            dest_lat, dest_lng = 41.3700, 69.3000
            addr = _make_address(db, customer_user.id, dest_lat, dest_lng)
            delivery = _make_in_transit_delivery(db, customer_user.id, driver_user.id, addr)
            with patch(
                "business_app.tasks.delivery_tasks.optimize_driver_route_task.delay"
            ):
                DeliveryService().mark_delivery_arrived(
                    delivery.id, actor_user_id=driver_user.id
                )

            db.session.expire_all()
            person = DeliveryPerson.query.filter_by(user_id=driver_user.id).first()
            # Live GPS preserved
            assert person.current_location_lat == pytest.approx(original_lat)
            assert person.current_location_lng == pytest.approx(original_lng)
            # History still stamped to the destination
            history = (
                DeliveryStatusHistory.query.filter_by(delivery_id=delivery.id)
                .order_by(DeliveryStatusHistory.changed_at.desc())
                .first()
            )
            assert history.location_lat == pytest.approx(dest_lat)
            assert history.location_lng == pytest.approx(dest_lng)

    def test_mark_arrived_enqueues_route_optimization(
        self, app, db, driver_user, customer_user, driver_with_stale_location
    ):
        """After commit, optimize_driver_route_task is enqueued with trigger='arrival'."""
        with app.app_context():
            addr = _make_address(db, customer_user.id, 41.3, 69.25)
            delivery = _make_in_transit_delivery(db, customer_user.id, driver_user.id, addr)

            with patch(
                "business_app.tasks.delivery_tasks.optimize_driver_route_task.delay"
            ) as mock_delay:
                DeliveryService().mark_delivery_arrived(
                    delivery.id, actor_user_id=driver_user.id
                )

            mock_delay.assert_called_once_with(driver_user.id, "arrival")
