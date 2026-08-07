"""Manual-override policy.

'The delivery set' means the driver's active deliveries (ASSIGNED, PICKED_UP,
IN_TRANSIT, ARRIVED), compared order-insensitively against optimized_order.
"""

from datetime import datetime, timezone
from unittest.mock import patch

from business_app.models.delivery import Delivery, DeliveryRoute
from business_app.services.route_optimization_service import RouteOptimizationService
from shared.enums import DeliveryStatus

# `driver_with_location` is a shared fixture in tests/conftest.py (used by
# both this file and tests/integration/test_staff_optimize_route_locked.py).


def make_delivery(db, driver, sample_user, sample_order, lat, lng):
    from business_app.models.user import UserAddress

    address = UserAddress(
        user_id=sample_user.id,
        full_address="x",
        city="Tashkent",
        latitude=lat,
        longitude=lng,
    )
    db.session.add(address)
    db.session.flush()
    order = sample_order.__class__(
        user_id=sample_user.id,
        order_number=f"ORD-{lat}-{lng}",
        total_amount=sample_order.total_amount,
        status=sample_order.status,
        payment_method=sample_order.payment_method,
        delivery_address_id=address.id,
        delivery_date=datetime.now(timezone.utc),
    )
    db.session.add(order)
    db.session.flush()
    delivery = Delivery(
        order_id=order.id,
        delivery_person_id=driver.id,
        status=DeliveryStatus.ASSIGNED,
        scheduled_date=datetime.now(timezone.utc),
        scheduled_time_slot="09:00-12:00",
    )
    db.session.add(delivery)
    db.session.flush()
    return delivery


def _matrix_from_points(points):
    """Build a Haversine-style matrix dict for the given (lat, lng) points.

    Mirrors `test_route_optimization_service.py`'s helper of the same name —
    keeps the "grow" test hermetic (no real Yandex/OSRM network call) while
    still exercising a real distance-based solve.
    """
    from business_app.utils.helpers import calculate_distance

    matrix = {}
    for i, pi in enumerate(points):
        for j, pj in enumerate(points):
            if i == j:
                matrix[(i, j)] = {"distance_km": 0.0, "duration_minutes": 0.0}
            else:
                km = calculate_distance(pi[0], pi[1], pj[0], pj[1])
                matrix[(i, j)] = {"distance_km": km, "duration_minutes": km * 2.4}
    return matrix


class TestOverrideSkip:
    def test_unchanged_set_is_skipped_without_touching_the_matrix(
        self, db, driver_with_location, sample_user, sample_order
    ):
        d1 = make_delivery(db, driver_with_location, sample_user, sample_order, 41.31, 69.25)
        d2 = make_delivery(db, driver_with_location, sample_user, sample_order, 41.32, 69.26)
        route = DeliveryRoute(
            name="r",
            delivery_person_id=driver_with_location.id,
            start_location_lat=41.30,
            start_location_lng=69.24,
            route_date=datetime.now(timezone.utc),
            optimized_order=[d2.id, d1.id],
            manual_override=True,
        )
        db.session.add(route)
        db.session.commit()

        svc = RouteOptimizationService()
        with patch.object(svc.maps, "get_distance_matrix") as matrix:
            result = svc.optimize_for_driver(driver_with_location.id)

        matrix.assert_not_called()
        assert result.id == route.id
        assert result.optimized_order == [d2.id, d1.id]
        assert result.manual_override is True

    def test_respect_override_false_clears_and_resolves(
        self, db, driver_with_location, sample_user, sample_order
    ):
        d1 = make_delivery(db, driver_with_location, sample_user, sample_order, 41.31, 69.25)
        d2 = make_delivery(db, driver_with_location, sample_user, sample_order, 41.32, 69.26)
        route = DeliveryRoute(
            name="r",
            delivery_person_id=driver_with_location.id,
            start_location_lat=41.30,
            start_location_lng=69.24,
            route_date=datetime.now(timezone.utc),
            optimized_order=[d2.id, d1.id],
            manual_override=True,
            pinned_stops={str(d2.id): 0},
        )
        db.session.add(route)
        db.session.commit()

        svc = RouteOptimizationService()
        with patch.object(
            svc.maps,
            "get_distance_matrix",
            side_effect=lambda points, traffic=True, use_cache=True: (
                _matrix_from_points(points),
                "haversine",
            ),
        ):
            result = svc.optimize_for_driver(driver_with_location.id, respect_override=False)

        assert result.manual_override is False
        assert result.pinned_stops == {}
        assert sorted(result.optimized_order) == sorted([d1.id, d2.id])


class TestOverrideShrink:
    def test_removed_stop_drops_and_remaining_order_is_kept(
        self, db, driver_with_location, sample_user, sample_order
    ):
        d1 = make_delivery(db, driver_with_location, sample_user, sample_order, 41.31, 69.25)
        d2 = make_delivery(db, driver_with_location, sample_user, sample_order, 41.32, 69.26)
        d3 = make_delivery(db, driver_with_location, sample_user, sample_order, 41.33, 69.27)
        route = DeliveryRoute(
            name="r",
            delivery_person_id=driver_with_location.id,
            start_location_lat=41.30,
            start_location_lng=69.24,
            route_date=datetime.now(timezone.utc),
            optimized_order=[d3.id, d1.id, d2.id],
            manual_override=True,
            pinned_stops={str(d2.id): 2},
        )
        db.session.add(route)
        d1.status = DeliveryStatus.DELIVERED
        db.session.commit()

        svc = RouteOptimizationService()
        with patch.object(svc.maps, "get_distance_matrix") as matrix:
            result = svc.optimize_for_driver(driver_with_location.id)

        matrix.assert_not_called()
        assert result.optimized_order == [d3.id, d2.id]
        assert result.pinned_stops == {str(d2.id): 1}  # clamped
        assert result.manual_override is True


class TestOverrideAllStopsGone:
    def test_all_stops_completed_clears_the_spent_override(
        self, db, driver_with_location, sample_user, sample_order
    ):
        d1 = make_delivery(db, driver_with_location, sample_user, sample_order, 41.31, 69.25)
        d2 = make_delivery(db, driver_with_location, sample_user, sample_order, 41.32, 69.26)
        route = DeliveryRoute(
            name="r",
            delivery_person_id=driver_with_location.id,
            start_location_lat=41.30,
            start_location_lng=69.24,
            route_date=datetime.now(timezone.utc),
            optimized_order=[d2.id, d1.id],
            manual_override=True,
            pinned_stops={str(d2.id): 0},
        )
        db.session.add(route)
        d1.status = DeliveryStatus.DELIVERED
        d2.status = DeliveryStatus.DELIVERED
        db.session.commit()
        route_id = route.id

        svc = RouteOptimizationService()
        with patch.object(svc.maps, "get_distance_matrix") as matrix:
            result = svc.optimize_for_driver(driver_with_location.id)

        matrix.assert_not_called()
        assert result is None

        # Force a fresh SELECT — proves the clearing was actually committed,
        # not just mutated on the in-memory object this test happens to hold.
        db.session.expire_all()
        persisted = DeliveryRoute.query.get(route_id)
        assert persisted.manual_override is False
        assert persisted.pinned_stops == {}
        assert persisted.optimized_order == []


class TestOverrideGrow:
    def test_new_stop_resolves_unpinned_and_keeps_the_pin(
        self, db, driver_with_location, sample_user, sample_order
    ):
        d1 = make_delivery(db, driver_with_location, sample_user, sample_order, 41.31, 69.25)
        d2 = make_delivery(db, driver_with_location, sample_user, sample_order, 41.34, 69.30)
        route = DeliveryRoute(
            name="r",
            delivery_person_id=driver_with_location.id,
            start_location_lat=41.30,
            start_location_lng=69.24,
            route_date=datetime.now(timezone.utc),
            optimized_order=[d2.id, d1.id],
            manual_override=True,
            pinned_stops={str(d2.id): 0},
        )
        db.session.add(route)
        db.session.commit()

        d3 = make_delivery(db, driver_with_location, sample_user, sample_order, 41.315, 69.255)
        db.session.commit()

        svc = RouteOptimizationService()
        with patch.object(
            svc.maps,
            "get_distance_matrix",
            side_effect=lambda points, traffic=True, use_cache=True: (
                _matrix_from_points(points),
                "haversine",
            ),
        ):
            result = svc.optimize_for_driver(driver_with_location.id)

        assert result.manual_override is True
        assert result.optimized_order[0] == d2.id          # pin held slot 0
        assert sorted(result.optimized_order) == sorted([d1.id, d2.id, d3.id])


class TestClampPins:
    def test_drops_missing_and_compacts_positions(self):
        clamped = RouteOptimizationService.clamp_pins({"7": 0, "9": 5, "11": 2}, [7, 11])
        assert clamped == {"7": 0, "11": 1}

    def test_empty_input_is_empty_dict(self):
        assert RouteOptimizationService.clamp_pins(None, [1, 2]) == {}
