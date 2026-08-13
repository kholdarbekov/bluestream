"""Anchor rule (spec §4.2): with a committed stop the tail is solved FROM the
committed stop's coordinates with the committed stop pinned at position 0.
A new order can then never jump ahead of the stop the driver is driving to."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from business_app.models.delivery import Delivery, DeliveryPerson, DeliveryStatusHistory
from business_app.models.order import Order
from business_app.models.user import User, UserAddress
from business_app.services.route_optimization_service import RouteOptimizationService
from shared.enums import DeliveryStatus, OrderStatus, UserRole, UserType


@pytest.fixture
def driver(db):
    user = User(
        email="anchor-driver@example.com",
        phone="+998900000041",
        password_hash="x",
        first_name="Anchor",
        last_name="Driver",
        user_type=UserType.STAFF,
        role=UserRole.DELIVERY_DRIVER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    person = DeliveryPerson(
        user_id=user.id,
        full_name="Anchor Driver",
        phone="+998900000041",
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
        email="anchor-cust@example.com",
        phone="+998900000042",
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


def _start_history(db, delivery, when):
    db.session.add(
        DeliveryStatusHistory(
            delivery_id=delivery.id,
            old_status=DeliveryStatus.PICKED_UP,
            new_status=DeliveryStatus.IN_TRANSIT,
            changed_by=delivery.delivery_person_id,
            changed_at=when,
        )
    )
    db.session.commit()


def _haversine_matrix_capture(calls):
    from business_app.utils.helpers import calculate_distance

    def fake(self, points, traffic=True, use_cache=True):
        calls.append(list(points))
        matrix = {}
        for i, pi in enumerate(points):
            for j, pj in enumerate(points):
                if i == j:
                    matrix[(i, j)] = {"distance_km": 0.0, "duration_minutes": 0.0}
                else:
                    km = calculate_distance(pi[0], pi[1], pj[0], pj[1])
                    matrix[(i, j)] = {"distance_km": km, "duration_minutes": km * 2.4}
        return matrix, "haversine"

    return fake


@pytest.mark.unit
@pytest.mark.delivery
class TestCommittedAnchor:
    def test_committed_stop_pins_first_and_anchors_matrix_origin(
        self, app, db, driver, customer, monkeypatch
    ):
        """Driver is IN_TRANSIT to a FAR stop A. A new ASSIGNED order lands
        practically next to the driver's GPS. Without the anchor, the new
        order would win position 0. With it: A stays first, and the matrix
        origin is A's coordinates — NOT the driver's GPS (spec §11
        'Committed prefix' test)."""
        committed = _make_delivery(
            db, customer.id, driver.id, "ORD-ANCH-A", 41.300, 69.330, DeliveryStatus.IN_TRANSIT
        )
        _start_history(db, committed, datetime.now(UTC))
        near_new = _make_delivery(
            db, customer.id, driver.id, "ORD-ANCH-B", 41.3005, 69.2505, DeliveryStatus.ASSIGNED
        )

        calls = []
        monkeypatch.setattr(
            "business_app.services.maps_service.MapsService.get_distance_matrix",
            _haversine_matrix_capture(calls),
        )

        with app.app_context():
            route = RouteOptimizationService().optimize_for_driver(driver.id, trigger="auto")

            assert route is not None
            assert route.optimized_order[0] == committed.id
            assert route.optimized_order == [committed.id, near_new.id]
            # Matrix origin (index 0) is the COMMITTED stop's coordinates.
            origin = calls[0][0]
            assert origin == pytest.approx((41.300, 69.330))
            assert (route.extra_data or {}).get("start_source") == "committed_stop"
            assert (route.extra_data or {}).get("committed_delivery_id") == committed.id

    def test_no_committed_stop_uses_driver_gps_as_today(
        self, app, db, driver, customer, monkeypatch
    ):
        _make_delivery(
            db, customer.id, driver.id, "ORD-ANCH-C", 41.300, 69.330, DeliveryStatus.ASSIGNED
        )
        calls = []
        monkeypatch.setattr(
            "business_app.services.maps_service.MapsService.get_distance_matrix",
            _haversine_matrix_capture(calls),
        )
        with app.app_context():
            route = RouteOptimizationService().optimize_for_driver(driver.id, trigger="auto")
            assert route is not None
            origin = calls[0][0]
            assert origin == pytest.approx((41.3000, 69.2500))
            assert (route.extra_data or {}).get("start_source") == "driver_live"
            assert (route.extra_data or {}).get("committed_delivery_id") is None

    def test_committed_pin_is_not_persisted_into_pinned_stops(
        self, app, db, driver, customer, monkeypatch
    ):
        """pinned_stops stays the ADMIN's override state. The committed pin is
        derived per solve; leaking it into pinned_stops would make the next
        admin edit treat it as a dispatch lock.

        Exercised under an ACTIVE manual_override whose delivery set GREW —
        the only path where `_upsert_route` writes `route.pinned_stops` from
        anything other than a hard `{}` (see `keep_override` in
        `_upsert_route`), so it is the only path where a leak of the
        solve-local committed pin could actually occur. A plain (no
        override) solve always resets `pinned_stops` to `{}` regardless of
        the anchor, which is why the original version of this test passed
        even before the anchor rule existed — it never reached the code that
        could leak."""
        from business_app.models.delivery import DeliveryRoute

        committed = _make_delivery(
            db, customer.id, driver.id, "ORD-ANCH-D", 41.300, 69.330, DeliveryStatus.IN_TRANSIT
        )
        _start_history(db, committed, datetime.now(UTC))

        # Admin override with NO pins of its own — the committed stop is
        # free to auto-pin slot 0 in `solving_pins` once the set grows.
        route = DeliveryRoute(
            name="admin route",
            delivery_person_id=driver.id,
            start_location_lat=41.3000,
            start_location_lng=69.2500,
            route_date=datetime.now(UTC),
            optimized_order=[committed.id],
            manual_override=True,
            pinned_stops={},
        )
        db.session.add(route)
        db.session.commit()

        # Grows the active set -> forces a real re-solve under the override.
        new_stop = _make_delivery(
            db, customer.id, driver.id, "ORD-ANCH-E", 41.301, 69.251, DeliveryStatus.ASSIGNED
        )

        calls = []
        monkeypatch.setattr(
            "business_app.services.maps_service.MapsService.get_distance_matrix",
            _haversine_matrix_capture(calls),
        )
        with app.app_context():
            result = RouteOptimizationService().optimize_for_driver(driver.id, trigger="auto")

            assert result.manual_override is True
            assert result.optimized_order[0] == committed.id  # committed pin held DURING the solve
            assert sorted(result.optimized_order) == sorted([committed.id, new_stop.id])
            # ...but never persisted as if the admin had pinned it.
            assert (result.pinned_stops or {}) == {}

    def test_persisted_metrics_stay_driver_anchored(
        self, app, db, driver, customer, monkeypatch
    ):
        """Spec §4.2 (amended): the anchor governs the SOLVE only. The
        persisted route still means 'driving left from where the driver
        actually is': start_location_lat/lng stays the driver's own GPS
        (never the committed stop's — matrix[(0, committed_idx)] is a
        zero-length leg, not the driver's position), and total_distance_km /
        estimated_duration_minutes are the tail total PLUS the driver ->
        committed-stop leg, priced by its own small 2-point matrix call."""
        from business_app.utils.helpers import calculate_distance

        committed = _make_delivery(
            db, customer.id, driver.id, "ORD-ANCH-F", 41.300, 69.330, DeliveryStatus.IN_TRANSIT
        )
        _start_history(db, committed, datetime.now(UTC))
        tail_stop = _make_delivery(
            db, customer.id, driver.id, "ORD-ANCH-G", 41.305, 69.335, DeliveryStatus.ASSIGNED
        )

        calls = []
        monkeypatch.setattr(
            "business_app.services.maps_service.MapsService.get_distance_matrix",
            _haversine_matrix_capture(calls),
        )

        with app.app_context():
            route = RouteOptimizationService().optimize_for_driver(driver.id, trigger="auto")

            assert route is not None
            # start_location_* is the DRIVER's GPS fixture position, not the
            # committed stop's coordinates (41.300, 69.330).
            assert route.start_location_lat == pytest.approx(41.3000)
            assert route.start_location_lng == pytest.approx(69.2500)
            assert (route.start_location_lat, route.start_location_lng) != pytest.approx(
                (41.300, 69.330)
            )

            # Tail-only total (what a matrix-origin-at-the-committed-stop
            # solve alone would sum to): committed -> committed (0, since the
            # committed stop is both the matrix origin AND a delivery node)
            # + committed -> tail_stop.
            tail_only_km = calculate_distance(41.300, 69.330, 41.300, 69.330) + calculate_distance(
                41.300, 69.330, 41.305, 69.335
            )
            # The driver -> committed leg the persisted total must ADD.
            leg_km = calculate_distance(41.3000, 69.2500, 41.300, 69.330)

            assert route.total_distance_km > tail_only_km
            assert route.total_distance_km == pytest.approx(tail_only_km + leg_km)
            # Route-UX plan Task 5: totals also carry flat per-stop service
            # time (ROUTE_SERVICE_TIME_MINUTES, default 4) for the 2 stops
            # in this tail (committed + tail_stop) — folded in exactly once
            # by `_sum_route_metrics`, unaffected by the separate driver ->
            # committed leg added afterward (that leg is travel only, no new
            # stop is being serviced).
            service_minutes = 2 * app.config.get("ROUTE_SERVICE_TIME_MINUTES", 4.0)
            expected_minutes = round((tail_only_km + leg_km) * 2.4 + service_minutes)
            assert route.estimated_duration_minutes == expected_minutes

    def test_stale_committed_stop_falls_back_to_gps_and_routes_normally(
        self, app, db, driver, customer, monkeypatch
    ):
        """A delivery that entered IN_TRANSIT more than
        COMMITTED_STOP_MAX_AGE_HOURS ago is still an ACTIVE delivery (Task 3),
        it just stops anchoring the route. The anchor rule must fall back to
        GPS — not to the stale stop — and the stale stop must still be routed
        as an ordinary, unpinned tail stop, never dropped."""
        max_age_hours = app.config.get("COMMITTED_STOP_MAX_AGE_HOURS", 12)
        stale = _make_delivery(
            db, customer.id, driver.id, "ORD-ANCH-H", 41.310, 69.260, DeliveryStatus.IN_TRANSIT
        )
        _start_history(db, stale, datetime.now(UTC) - timedelta(hours=max_age_hours, minutes=5))
        other = _make_delivery(
            db, customer.id, driver.id, "ORD-ANCH-I", 41.305, 69.255, DeliveryStatus.ASSIGNED
        )

        calls = []
        monkeypatch.setattr(
            "business_app.services.maps_service.MapsService.get_distance_matrix",
            _haversine_matrix_capture(calls),
        )

        with app.app_context():
            route = RouteOptimizationService().optimize_for_driver(driver.id, trigger="auto")

            assert route is not None
            assert (route.extra_data or {}).get("start_source") == "driver_live"
            assert (route.extra_data or {}).get("committed_delivery_id") is None
            assert set(route.optimized_order) == {stale.id, other.id}
