"""Unit tests for the route-optimization Celery tasks.

The conftest `block_external_side_effects` fixture replaces `Task.delay` with
a mock; we don't need eager Celery here. Instead we call the task body
directly (the function under `@shared_task` is still callable as a regular
function, with `self` substituted by a `MagicMock`).
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from business_app.models.delivery import Delivery, DeliveryPerson, DeliveryRoute
from business_app.models.order import Order
from business_app.models.user import User, UserAddress
from business_app.tasks.delivery_tasks import (
    evaluate_pool_insertion_suggestions_task,
    optimize_driver_route_task,
)
from shared.enums import DeliveryStatus, OrderStatus, UserRole, UserType


def _haversine_matrix(self, points, traffic=True, use_cache=True):
    from business_app.utils.helpers import calculate_distance

    matrix = {}
    for i, pi in enumerate(points):
        for j, pj in enumerate(points):
            if i == j:
                matrix[(i, j)] = {"distance_km": 0.0, "duration_minutes": 0.0}
            else:
                km = calculate_distance(pi[0], pi[1], pj[0], pj[1])
                matrix[(i, j)] = {"distance_km": km, "duration_minutes": km * 2.4}
    return matrix, "haversine"


@pytest.fixture
def driver(db):
    user = User(
        email="task-driver@example.com",
        phone="+998901111100",
        password_hash="x",
        first_name="Task",
        last_name="Driver",
        user_type=UserType.STAFF,
        role=UserRole.DELIVERY_DRIVER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def customer(db):
    user = User(
        email="task-cust@example.com",
        phone="+998907700099",
        password_hash="x",
        first_name="C",
        last_name="ust",
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def driver_with_location(db, driver):
    # Set working_hours to 24-hour coverage so `is_working_now` is True
    # regardless of when the test runs in UTC.
    person = DeliveryPerson(
        user_id=driver.id,
        full_name="x",
        phone="x",
        current_location_lat=41.300,
        current_location_lng=69.250,
        last_location_update=datetime.now(UTC),
        is_active=True,
        is_available=True,
        working_hours_start="00:00",
        working_hours_end="23:59",
    )
    db.session.add(person)
    db.session.commit()
    return person


def _add_assigned_delivery(db, customer_id, driver_id, lat, lng, order_no):
    addr = UserAddress(
        user_id=customer_id,
        title="x",
        full_address="x",
        street_address="x",
        latitude=lat,
        longitude=lng,
    )
    db.session.add(addr)
    db.session.flush()
    order = Order(
        user_id=customer_id,
        order_number=order_no,
        status=OrderStatus.CONFIRMED,
        subtotal=Decimal("0"),
        total_amount=Decimal("0"),
        delivery_address_id=addr.id,
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


def _add_pool_delivery(db, customer_id, lat, lng, order_no):
    addr = UserAddress(
        user_id=customer_id,
        title="pool",
        full_address="pool",
        street_address="pool",
        latitude=lat,
        longitude=lng,
    )
    db.session.add(addr)
    db.session.flush()
    order = Order(
        user_id=customer_id,
        order_number=order_no,
        status=OrderStatus.CONFIRMED,
        subtotal=Decimal("0"),
        total_amount=Decimal("0"),
        delivery_address_id=addr.id,
    )
    db.session.add(order)
    db.session.flush()
    delivery = Delivery(
        order_id=order.id,
        status=DeliveryStatus.SCHEDULED,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot="09:00-12:00",
    )
    db.session.add(delivery)
    db.session.commit()
    return delivery


# ---------------------------------------------------------------------------
# optimize_driver_route_task
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.delivery
class TestOptimizeDriverRouteTask:
    def test_returns_no_active_deliveries_when_driver_has_none(self, app, db, driver):
        with app.app_context():
            with patch("business_app.utils.bot_webhook.notify_route_updated") as nru:
                result = optimize_driver_route_task.run(driver.id)
            assert result == {"optimized": False, "reason": "no_active_deliveries"}
            nru.assert_not_called()

    def test_optimizes_and_pushes_route_updated_webhook(
        self, app, db, driver, driver_with_location, customer, monkeypatch
    ):
        with app.app_context():
            d_far = _add_assigned_delivery(db, customer.id, driver.id, 41.300, 69.330, "ORD-far")
            d_close = _add_assigned_delivery(db, customer.id, driver.id, 41.300, 69.260, "ORD-close")

            monkeypatch.setattr(
                "business_app.services.maps_service.MapsService.get_distance_matrix",
                _haversine_matrix,
            )
            # Helpers are imported lazily inside the task body — patch at the
            # source module, not at the task module.
            with patch("business_app.utils.bot_webhook.notify_route_updated", return_value=True) as nru:
                result = optimize_driver_route_task.run(driver.id, trigger="accept")

            assert result["optimized"] is True
            assert result["delivery_count"] == 2
            assert result["trigger"] == "accept"
            assert result["matrix_source"] == "haversine"
            nru.assert_called_once_with(driver.id)

            # Persisted route reflects the closest-first sequence.
            route = (
                DeliveryRoute.query.filter_by(delivery_person_id=driver.id)
                .order_by(DeliveryRoute.created_at.desc())
                .first()
            )
            assert route.optimized_order == [d_close.id, d_far.id]


# ---------------------------------------------------------------------------
# evaluate_pool_insertion_suggestions_task
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.delivery
class TestEvaluatePoolInsertionSuggestionsTask:
    def test_skips_when_delivery_already_assigned(
        self, app, db, driver, driver_with_location, customer
    ):
        with app.app_context():
            d = _add_assigned_delivery(db, customer.id, driver.id, 41.31, 69.27, "ORD-A")
            with patch("business_app.utils.bot_webhook.notify_pool_insertion_suggestion") as npis:
                result = evaluate_pool_insertion_suggestions_task.run(d.id)
            assert result == {"suggested": False, "reason": "already_assigned"}
            npis.assert_not_called()

    def test_skips_when_no_active_drivers(self, app, db, customer):
        with app.app_context():
            d = _add_pool_delivery(db, customer.id, 41.31, 69.27, "ORD-POOL-1")
            with patch("business_app.utils.bot_webhook.notify_pool_insertion_suggestion") as npis:
                result = evaluate_pool_insertion_suggestions_task.run(d.id)
            assert result == {"suggested": False, "reason": "no_active_drivers"}
            npis.assert_not_called()

    def test_skips_when_detour_exceeds_threshold(
        self, app, db, driver, driver_with_location, customer, monkeypatch
    ):
        with app.app_context():
            # Driver has one stop close by (near lng 69.25); the pool delivery is
            # across town (lng 69.33, ~6-7 km east) — still inside the delivery
            # zone (TASHKENT_POLYGON), but far enough to blow the tight detour budget.
            _add_assigned_delivery(db, customer.id, driver.id, 41.301, 69.251, "ORD-close")
            far_delivery = _add_pool_delivery(db, customer.id, 41.300, 69.330, "ORD-far")

            monkeypatch.setattr(
                "business_app.services.maps_service.MapsService.get_distance_matrix",
                _haversine_matrix,
            )
            # Tighten the detour budget so we're sure the far stop won't fit.
            app.config["ROUTE_INSERTION_MAX_DETOUR_KM"] = 1.0
            app.config["ROUTE_INSERTION_MAX_DETOUR_MIN"] = 5.0

            with patch("business_app.utils.bot_webhook.notify_pool_insertion_suggestion") as npis:
                result = evaluate_pool_insertion_suggestions_task.run(far_delivery.id)
            assert result == {"suggested": False, "reason": "no_fit_within_thresholds"}
            npis.assert_not_called()

    def test_pushes_suggestion_when_pool_order_fits_route(
        self, app, db, driver, driver_with_location, customer, monkeypatch
    ):
        with app.app_context():
            # Driver headed east: existing stop at 69.330. New pool stop at 69.255
            # is right next to driver — well under the default 5 km / 15 min cap.
            _add_assigned_delivery(db, customer.id, driver.id, 41.300, 69.330, "ORD-east")
            close_pool = _add_pool_delivery(db, customer.id, 41.301, 69.255, "ORD-near-driver")

            monkeypatch.setattr(
                "business_app.services.maps_service.MapsService.get_distance_matrix",
                _haversine_matrix,
            )
            # Generous detour budget so the close stop fits comfortably.
            app.config["ROUTE_INSERTION_MAX_DETOUR_KM"] = 50.0
            app.config["ROUTE_INSERTION_MAX_DETOUR_MIN"] = 120.0

            with patch(
                "business_app.utils.bot_webhook.notify_pool_insertion_suggestion",
                return_value=True,
            ) as npis:
                result = evaluate_pool_insertion_suggestions_task.run(close_pool.id)

            assert result["suggested"] is True
            assert result["driver_id"] == driver.id
            assert result["delta_km"] >= 0
            assert result["position"] >= 1
            npis.assert_called_once()
            # Verify the bot helper was called with the right payload shape.
            kwargs = npis.call_args.kwargs
            assert kwargs["driver_id"] == driver.id
            assert kwargs["delivery_id"] == close_pool.id
            assert kwargs["order_no"] == "ORD-near-driver"
            assert "detour_km" in kwargs
            assert "detour_minutes" in kwargs
