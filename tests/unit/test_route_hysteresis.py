"""Hysteresis (spec §4.4): with an unchanged delivery set, a re-sequence is
published only when it beats the current sequence by BOTH the absolute floor
(ROUTE_RESEQUENCE_MIN_GAIN_MINUTES) and the relative margin
(ROUTE_RESEQUENCE_MIN_GAIN_RATIO). Set changes always publish. Both orders
are costed on the SAME matrix snapshot."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.order import Order
from business_app.models.user import User, UserAddress
from business_app.services.route_optimization_service import RouteOptimizationService
from shared.enums import DeliveryStatus, OrderStatus, UserRole, UserType


@pytest.fixture
def driver(db):
    user = User(
        email="hys-driver@example.com",
        phone="+998900000061",
        password_hash="x",
        first_name="Hys",
        last_name="Driver",
        user_type=UserType.STAFF,
        role=UserRole.DELIVERY_DRIVER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    person = DeliveryPerson(
        user_id=user.id,
        full_name="Hys Driver",
        phone="+998900000061",
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
        email="hys-cust@example.com",
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


def _fixed_matrix_factory(minutes_by_leg):
    """Matrix stub returning scripted durations. `minutes_by_leg` maps
    (i, j) -> minutes; km mirrors minutes so both metrics move together."""

    def fake(self, points, traffic=True, use_cache=True):
        n = len(points)
        matrix = {}
        for i in range(n):
            for j in range(n):
                if i == j:
                    matrix[(i, j)] = {"distance_km": 0.0, "duration_minutes": 0.0}
                else:
                    mins = minutes_by_leg[(i, j)]
                    matrix[(i, j)] = {"distance_km": mins / 2.4, "duration_minutes": mins}
        return matrix, "haversine"

    return fake


def _fixed_matrix_with_leg_factory(minutes_by_leg, leg_minutes=15.0):
    """Like `_fixed_matrix_factory`, but also serves the driver->committed
    2-point leg call the anchor rule (Task 4) issues as a SEPARATE
    `get_distance_matrix` call once a route is anchored (the tail matrix
    never contains the driver's live point, so that leg is priced
    independently). A 2-point request gets a fixed `leg_minutes` duration;
    any other point count falls back to `minutes_by_leg`, exactly like
    `_fixed_matrix_factory`."""

    def fake(self, points, traffic=True, use_cache=True):
        n = len(points)
        if n == 2:
            return (
                {
                    (0, 0): {"distance_km": 0.0, "duration_minutes": 0.0},
                    (0, 1): {"distance_km": leg_minutes / 2.4, "duration_minutes": leg_minutes},
                    (1, 0): {"distance_km": leg_minutes / 2.4, "duration_minutes": leg_minutes},
                    (1, 1): {"distance_km": 0.0, "duration_minutes": 0.0},
                },
                "haversine",
            )
        matrix = {}
        for i in range(n):
            for j in range(n):
                if i == j:
                    matrix[(i, j)] = {"distance_km": 0.0, "duration_minutes": 0.0}
                else:
                    mins = minutes_by_leg[(i, j)]
                    matrix[(i, j)] = {"distance_km": mins / 2.4, "duration_minutes": mins}
        return matrix, "haversine"

    return fake


@pytest.mark.unit
@pytest.mark.delivery
class TestHysteresis:
    def _two_stop_setup(self, db, driver, customer):
        d1 = _make_delivery(db, customer.id, driver.id, "ORD-H-1", 41.30, 69.26)
        d2 = _make_delivery(db, customer.id, driver.id, "ORD-H-2", 41.30, 69.33)
        return d1, d2

    def test_sub_threshold_gain_keeps_previous_sequence(
        self, app, db, driver, customer, monkeypatch
    ):
        """Second solve says swapping saves 2 min on a 40-min route — below
        both the 4-min floor and the 8% ratio. The previous order stands."""
        d1, d2 = self._two_stop_setup(db, driver, customer)
        with app.app_context():
            svc = RouteOptimizationService()
            # First solve: [d1, d2] is clearly better.
            first = _fixed_matrix_factory({
                (0, 1): 10.0, (0, 2): 40.0, (1, 0): 10.0,
                (1, 2): 30.0, (2, 0): 40.0, (2, 1): 30.0,
            })
            monkeypatch.setattr(
                "business_app.services.maps_service.MapsService.get_distance_matrix", first
            )
            r1 = svc.optimize_for_driver(driver.id, trigger="accept")
            assert r1.optimized_order == [d1.id, d2.id]
            # Second solve: provider jitter now claims [d2, d1] saves 2 min
            # (prev [d1,d2] re-costed on the NEW matrix = 12+28 = 40;
            # new best [d2,d1] = 24+14 = 38).
            second = _fixed_matrix_factory({
                (0, 1): 12.0, (0, 2): 24.0, (1, 0): 12.0,
                (1, 2): 28.0, (2, 0): 24.0, (2, 1): 14.0,
            })
            monkeypatch.setattr(
                "business_app.services.maps_service.MapsService.get_distance_matrix", second
            )
            r2 = svc.optimize_for_driver(driver.id, trigger="auto")
            assert r2.optimized_order == [d1.id, d2.id]
            # Suppression must never leave stray override/pin state behind —
            # this is a plain auto-solve, so both stay at their zero value.
            assert r2.manual_override is False
            assert r2.pinned_stops == {}
            m = (r2.extra_data or {})["materiality"]
            assert m["sequence_changed"] is False
            assert m["head_changed"] is False

    def test_material_gain_publishes_new_sequence(
        self, app, db, driver, customer, monkeypatch
    ):
        """Swapping saves 20 min on a 40-min route — over both thresholds."""
        d1, d2 = self._two_stop_setup(db, driver, customer)
        with app.app_context():
            svc = RouteOptimizationService()
            first = _fixed_matrix_factory({
                (0, 1): 10.0, (0, 2): 40.0, (1, 0): 10.0,
                (1, 2): 30.0, (2, 0): 40.0, (2, 1): 30.0,
            })
            monkeypatch.setattr(
                "business_app.services.maps_service.MapsService.get_distance_matrix", first
            )
            svc.optimize_for_driver(driver.id, trigger="accept")
            # Road closure: prev order now costs 10+50=60, new-first costs 25+15=40.
            second = _fixed_matrix_factory({
                (0, 1): 10.0, (0, 2): 25.0, (1, 0): 10.0,
                (1, 2): 50.0, (2, 0): 25.0, (2, 1): 15.0,
            })
            monkeypatch.setattr(
                "business_app.services.maps_service.MapsService.get_distance_matrix", second
            )
            r2 = svc.optimize_for_driver(driver.id, trigger="auto")
            assert r2.optimized_order == [d2.id, d1.id]
            m = (r2.extra_data or {})["materiality"]
            assert m["sequence_changed"] is True
            assert m["head_changed"] is True

    def test_set_change_always_publishes(self, app, db, driver, customer, monkeypatch):
        d1, d2 = self._two_stop_setup(db, driver, customer)
        with app.app_context():
            svc = RouteOptimizationService()
            first = _fixed_matrix_factory({
                (0, 1): 10.0, (0, 2): 40.0, (1, 0): 10.0,
                (1, 2): 30.0, (2, 0): 40.0, (2, 1): 30.0,
            })
            monkeypatch.setattr(
                "business_app.services.maps_service.MapsService.get_distance_matrix", first
            )
            svc.optimize_for_driver(driver.id, trigger="accept")
            d3 = _make_delivery(db, customer.id, driver.id, "ORD-H-3", 41.30, 69.29)
            def haversine(self, points, traffic=True, use_cache=True):
                from business_app.utils.helpers import calculate_distance
                matrix = {}
                for i, pi in enumerate(points):
                    for j, pj in enumerate(points):
                        km = 0.0 if i == j else calculate_distance(pi[0], pi[1], pj[0], pj[1])
                        matrix[(i, j)] = {"distance_km": km, "duration_minutes": km * 2.4}
                return matrix, "haversine"
            monkeypatch.setattr(
                "business_app.services.maps_service.MapsService.get_distance_matrix", haversine
            )
            r2 = svc.optimize_for_driver(driver.id, trigger="auto")
            assert d3.id in r2.optimized_order
            m = (r2.extra_data or {})["materiality"]
            assert m["set_changed"] is True
            assert m["sequence_changed"] is True

    def test_travel_only_gain_publishes_despite_service_time_penalty(
        self, app, db, driver, customer, monkeypatch
    ):
        """Bug fix (review round, C1): the gate must compare travel-only
        minutes on both sides. `_sum_route_metrics` folds
        `ROUTE_SERVICE_TIME_MINUTES * stops` into its `total_min`; if that
        service-inclusive figure is compared against a travel-only
        `prev_min`, the fold becomes a constant penalty subtracted from every
        gain (2 stops * 4.0 min/stop = 8 min here), silently retuning the
        `ROUTE_RESEQUENCE_MIN_GAIN_MINUTES` floor.

        Real travel gain is scripted at 10 min: prev [d1,d2] = 20+30=50,
        best [d2,d1] = 25+15=40. That clears the 4-min floor and the 8%
        ratio (10/50=20%) on its own — it must publish. Before the fix the
        gate computes gain = prev_min(50, travel) - total_min(48, travel+8
        service) = 2, under the 4-min floor, so the previous order wrongly
        stands."""
        d1, d2 = self._two_stop_setup(db, driver, customer)
        with app.app_context():
            svc = RouteOptimizationService()
            first = _fixed_matrix_factory({
                (0, 1): 10.0, (0, 2): 40.0, (1, 0): 10.0,
                (1, 2): 30.0, (2, 0): 40.0, (2, 1): 30.0,
            })
            monkeypatch.setattr(
                "business_app.services.maps_service.MapsService.get_distance_matrix", first
            )
            r1 = svc.optimize_for_driver(driver.id, trigger="accept")
            assert r1.optimized_order == [d1.id, d2.id]

            second = _fixed_matrix_factory({
                (0, 1): 20.0, (0, 2): 25.0, (1, 0): 20.0,
                (1, 2): 30.0, (2, 0): 25.0, (2, 1): 15.0,
            })
            monkeypatch.setattr(
                "business_app.services.maps_service.MapsService.get_distance_matrix", second
            )
            r2 = svc.optimize_for_driver(driver.id, trigger="auto")
            assert r2.optimized_order == [d2.id, d1.id]
            m = (r2.extra_data or {})["materiality"]
            assert m["sequence_changed"] is True
            assert m["head_changed"] is True

    def test_gain_above_minutes_floor_but_below_ratio_is_suppressed(
        self, app, db, driver, customer, monkeypatch
    ):
        """Swapping saves 5 min on a 100-min route: 5 >= the 4-min floor, but
        5/100 = 5% is under the 8% ratio. BOTH are required, so this must
        still be suppressed."""
        d1, d2 = self._two_stop_setup(db, driver, customer)
        with app.app_context():
            svc = RouteOptimizationService()
            first = _fixed_matrix_factory({
                (0, 1): 10.0, (0, 2): 40.0, (1, 0): 10.0,
                (1, 2): 30.0, (2, 0): 40.0, (2, 1): 30.0,
            })
            monkeypatch.setattr(
                "business_app.services.maps_service.MapsService.get_distance_matrix", first
            )
            r1 = svc.optimize_for_driver(driver.id, trigger="accept")
            assert r1.optimized_order == [d1.id, d2.id]
            # prev [d1,d2] = 50+50 = 100; best [d2,d1] = 50+45 = 95; gain=5.
            second = _fixed_matrix_factory({
                (0, 1): 50.0, (0, 2): 50.0, (1, 0): 50.0,
                (1, 2): 50.0, (2, 0): 50.0, (2, 1): 45.0,
            })
            monkeypatch.setattr(
                "business_app.services.maps_service.MapsService.get_distance_matrix", second
            )
            r2 = svc.optimize_for_driver(driver.id, trigger="auto")
            assert r2.optimized_order == [d1.id, d2.id]
            assert r2.manual_override is False
            assert r2.pinned_stops == {}
            m = (r2.extra_data or {})["materiality"]
            assert m["sequence_changed"] is False
            assert m["head_changed"] is False

    def test_gain_above_ratio_but_below_minutes_floor_is_suppressed(
        self, app, db, driver, customer, monkeypatch
    ):
        """Swapping saves 3 min on a 20-min route: 3/20 = 15% clears the 8%
        ratio, but 3 min is under the 4-min floor. BOTH are required, so this
        must still be suppressed."""
        d1, d2 = self._two_stop_setup(db, driver, customer)
        with app.app_context():
            svc = RouteOptimizationService()
            first = _fixed_matrix_factory({
                (0, 1): 10.0, (0, 2): 40.0, (1, 0): 10.0,
                (1, 2): 30.0, (2, 0): 40.0, (2, 1): 30.0,
            })
            monkeypatch.setattr(
                "business_app.services.maps_service.MapsService.get_distance_matrix", first
            )
            r1 = svc.optimize_for_driver(driver.id, trigger="accept")
            assert r1.optimized_order == [d1.id, d2.id]
            # prev [d1,d2] = 10+10 = 20; best [d2,d1] = 9+8 = 17; gain=3.
            second = _fixed_matrix_factory({
                (0, 1): 10.0, (0, 2): 9.0, (1, 0): 10.0,
                (1, 2): 10.0, (2, 0): 9.0, (2, 1): 8.0,
            })
            monkeypatch.setattr(
                "business_app.services.maps_service.MapsService.get_distance_matrix", second
            )
            r2 = svc.optimize_for_driver(driver.id, trigger="auto")
            assert r2.optimized_order == [d1.id, d2.id]
            assert r2.manual_override is False
            assert r2.pinned_stops == {}
            m = (r2.extra_data or {})["materiality"]
            assert m["sequence_changed"] is False
            assert m["head_changed"] is False


@pytest.mark.unit
@pytest.mark.delivery
class TestExplicitRequestBypass:
    """Spec §4.4 (amended): a human who explicitly asked for a fresh optimum
    right now — 'Reset to optimal' (admin_dispatch_reset) / 'Optimize routes'
    (manual) — bypasses the hysteresis gate entirely, even on an unchanged
    set with a sub-threshold gain. This is deliberately narrower than
    DRIVER_INITIATED_TRIGGERS: a driver-caused trigger like location_update
    is not a re-optimize REQUEST and must stay gated (proven by the control
    test below, using the identical scripted gain)."""

    def _two_stop_setup(self, db, driver, customer):
        d1 = _make_delivery(db, customer.id, driver.id, "ORD-HB-1", 41.30, 69.26)
        d2 = _make_delivery(db, customer.id, driver.id, "ORD-HB-2", 41.30, 69.33)
        return d1, d2

    def _sub_threshold_second_matrix(self):
        # Identical scripted scenario to test_sub_threshold_gain_keeps_previous_sequence:
        # prev [d1,d2] re-costed = 12+28 = 40; new best [d2,d1] = 24+14 = 38.
        # Gain=2min: below both the 4-min floor and the 8% ratio.
        return _fixed_matrix_factory({
            (0, 1): 12.0, (0, 2): 24.0, (1, 0): 12.0,
            (1, 2): 28.0, (2, 0): 24.0, (2, 1): 14.0,
        })

    def test_admin_dispatch_reset_bypasses_gate_and_publishes(
        self, app, db, driver, customer, monkeypatch
    ):
        d1, d2 = self._two_stop_setup(db, driver, customer)
        with app.app_context():
            svc = RouteOptimizationService()
            first = _fixed_matrix_factory({
                (0, 1): 10.0, (0, 2): 40.0, (1, 0): 10.0,
                (1, 2): 30.0, (2, 0): 40.0, (2, 1): 30.0,
            })
            monkeypatch.setattr(
                "business_app.services.maps_service.MapsService.get_distance_matrix", first
            )
            r1 = svc.optimize_for_driver(driver.id, trigger="accept")
            assert r1.optimized_order == [d1.id, d2.id]

            second = self._sub_threshold_second_matrix()
            monkeypatch.setattr(
                "business_app.services.maps_service.MapsService.get_distance_matrix", second
            )
            # Mirrors RouteEditService.reoptimize()'s actual call shape.
            r2 = svc.optimize_for_driver(
                driver.id, trigger="admin_dispatch_reset", respect_override=False
            )
            assert r2.optimized_order == [d2.id, d1.id]  # published despite the 2-min gain
            m = (r2.extra_data or {})["materiality"]
            assert m["sequence_changed"] is True
            assert m["head_changed"] is True

    def test_manual_trigger_bypasses_gate_and_publishes(
        self, app, db, driver, customer, monkeypatch
    ):
        d1, d2 = self._two_stop_setup(db, driver, customer)
        with app.app_context():
            svc = RouteOptimizationService()
            first = _fixed_matrix_factory({
                (0, 1): 10.0, (0, 2): 40.0, (1, 0): 10.0,
                (1, 2): 30.0, (2, 0): 40.0, (2, 1): 30.0,
            })
            monkeypatch.setattr(
                "business_app.services.maps_service.MapsService.get_distance_matrix", first
            )
            r1 = svc.optimize_for_driver(driver.id, trigger="accept")
            assert r1.optimized_order == [d1.id, d2.id]

            second = self._sub_threshold_second_matrix()
            monkeypatch.setattr(
                "business_app.services.maps_service.MapsService.get_distance_matrix", second
            )
            # Mirrors the staff-bot "Optimize routes" tap (staff.py:533).
            r2 = svc.optimize_for_driver(driver.id, trigger="manual")
            assert r2.optimized_order == [d2.id, d1.id]  # published despite the 2-min gain
            m = (r2.extra_data or {})["materiality"]
            assert m["sequence_changed"] is True
            assert m["head_changed"] is True

    def test_location_update_trigger_is_still_gated_control(
        self, app, db, driver, customer, monkeypatch
    ):
        """Control: location_update is driver-CAUSED (it's in
        DRIVER_INITIATED_TRIGGERS) but it is not an explicit re-optimize
        REQUEST, so it must stay gated. Identical scripted scenario to the
        two bypass tests above — only the trigger differs — and the
        outcome must differ too.

        Task 11 note: a location_update solve is ALSO subject to the
        per-driver debounce (tests/unit/test_route_debounce.py) — skip when
        the last solve is fresh or the driver barely moved. This test is
        about the HYSTERESIS gate downstream of that, so the second call
        first clears the debounce gate (backdate last_optimized_at past the
        window, move the driver past the min-move threshold) to make sure
        it actually reaches the solve instead of being debounced away.
        """
        d1, d2 = self._two_stop_setup(db, driver, customer)
        with app.app_context():
            svc = RouteOptimizationService()
            first = _fixed_matrix_factory({
                (0, 1): 10.0, (0, 2): 40.0, (1, 0): 10.0,
                (1, 2): 30.0, (2, 0): 40.0, (2, 1): 30.0,
            })
            monkeypatch.setattr(
                "business_app.services.maps_service.MapsService.get_distance_matrix", first
            )
            r1 = svc.optimize_for_driver(driver.id, trigger="accept")
            assert r1.optimized_order == [d1.id, d2.id]

            # Clear the Task 11 debounce gate so this call reaches the
            # hysteresis logic under test.
            extra = dict(r1.extra_data or {})
            extra["last_optimized_at"] = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
            r1.extra_data = extra
            db.session.commit()
            person = DeliveryPerson.query.filter_by(user_id=driver.id).first()
            person.current_location_lat = 41.3200  # well past ROUTE_OPTIMIZE_MIN_MOVE_METERS
            person.last_location_update = datetime.now(UTC)
            db.session.commit()

            second = self._sub_threshold_second_matrix()
            monkeypatch.setattr(
                "business_app.services.maps_service.MapsService.get_distance_matrix", second
            )
            r2 = svc.optimize_for_driver(driver.id, trigger="location_update")
            assert r2.optimized_order == [d1.id, d2.id]  # suppressed — still gated
            m = (r2.extra_data or {})["materiality"]
            assert m["sequence_changed"] is False
            assert m["head_changed"] is False


@pytest.mark.unit
@pytest.mark.delivery
class TestHysteresisWithCommittedAnchor:
    """Fix 2 (route-UX plan 2026-08-11 review round): the gate runs BEFORE the
    driver->committed-stop leg is priced (Task 4's anchor). This test locks
    that ordering in with a real assertion instead of only a comment: on a
    suppressed, anchored solve, `total_distance_km` must be the tail-only
    total for the (unchanged) published order PLUS the driver->committed
    leg — never a total that silently dropped or double-counted that leg."""

    def _committed_plus_two_tail_setup(self, db, driver, customer):
        from business_app.models.delivery import DeliveryStatusHistory

        committed = _make_delivery(db, customer.id, driver.id, "ORD-HC-0", 41.300, 69.330)
        committed.status = DeliveryStatus.IN_TRANSIT
        db.session.add(
            DeliveryStatusHistory(
                delivery_id=committed.id,
                old_status=DeliveryStatus.PICKED_UP,
                new_status=DeliveryStatus.IN_TRANSIT,
                changed_by=driver.id,
                changed_at=datetime.now(UTC),
            )
        )
        tail_a = _make_delivery(db, customer.id, driver.id, "ORD-HC-1", 41.305, 69.335)
        tail_b = _make_delivery(db, customer.id, driver.id, "ORD-HC-2", 41.310, 69.340)
        db.session.commit()
        return committed, tail_a, tail_b

    def test_suppressed_anchored_solve_keeps_order_and_totals_tail_plus_leg(
        self, app, db, driver, customer, monkeypatch
    ):
        """Deliveries are [committed, tail_a, tail_b] (id-ascending), so the
        tail matrix carries FOUR points, not three: index 0 is the matrix
        origin (the committed stop's coordinates, per the anchor rule) and
        index 1 is that SAME committed delivery appearing again as a normal
        delivery node (pinned to slot 0). (0,1)/(1,0) are therefore a
        zero-length self-leg. This mirrors
        test_committed_anchor.py::test_persisted_metrics_stay_driver_anchored's
        own tail-total formula."""
        committed, tail_a, tail_b = self._committed_plus_two_tail_setup(db, driver, customer)
        with app.app_context():
            svc = RouteOptimizationService()
            # First solve: [tail_a, tail_b] wins clearly (40-min tail).
            first = _fixed_matrix_with_leg_factory(
                {
                    (0, 1): 0.0, (1, 0): 0.0,
                    (0, 2): 10.0, (1, 2): 10.0,
                    (0, 3): 40.0, (1, 3): 40.0,
                    (2, 0): 10.0, (2, 1): 10.0,
                    (2, 3): 30.0,
                    (3, 0): 40.0, (3, 1): 40.0,
                    (3, 2): 30.0,
                },
                leg_minutes=15.0,
            )
            monkeypatch.setattr(
                "business_app.services.maps_service.MapsService.get_distance_matrix", first
            )
            r1 = svc.optimize_for_driver(driver.id, trigger="accept")
            assert r1.optimized_order == [committed.id, tail_a.id, tail_b.id]

            # Second solve: provider jitter claims swapping the tail
            # (committed->tail_a=12, tail_a->tail_b=28 -> committed->tail_b=24,
            # tail_b->tail_a=14) saves 2 min on the 40-min tail — below both
            # thresholds, so the tail order must NOT change.
            second = _fixed_matrix_with_leg_factory(
                {
                    (0, 1): 0.0, (1, 0): 0.0,
                    (0, 2): 12.0, (1, 2): 12.0,
                    (0, 3): 24.0, (1, 3): 24.0,
                    (2, 0): 12.0, (2, 1): 12.0,
                    (2, 3): 28.0,
                    (3, 0): 24.0, (3, 1): 24.0,
                    (3, 2): 14.0,
                },
                leg_minutes=15.0,
            )
            monkeypatch.setattr(
                "business_app.services.maps_service.MapsService.get_distance_matrix", second
            )
            r2 = svc.optimize_for_driver(driver.id, trigger="auto")

            assert r2.optimized_order == [committed.id, tail_a.id, tail_b.id]
            m = (r2.extra_data or {})["materiality"]
            assert m["sequence_changed"] is False

            # Tail-only total for the PUBLISHED (unchanged) order, on the
            # SECOND (current) matrix: committed->committed (0, index 0->1
            # self-leg) + committed->tail_a (0->2) + tail_a->tail_b (2->3).
            prev_tail_min = 0.0 + 12.0 + 28.0
            prev_tail_km = prev_tail_min / 2.4
            # Driver -> committed leg, priced by its own small 2-point call,
            # fixed at 15 minutes by `_fixed_matrix_with_leg_factory`.
            leg_km = 15.0 / 2.4
            assert r2.total_distance_km == pytest.approx(prev_tail_km + leg_km)


@pytest.mark.unit
@pytest.mark.delivery
class TestPrevRespectsPinsBranch:
    """Fix 2 (review round): the `prev_respects_pins` branch (a previously
    published order is only eligible to stand if it satisfies the CURRENT
    solve's pins) was untested. Exercised here via the committed-stop pin:
    a delivery becomes newly committed (pinned to slot 0 by this solve), but
    the previously published order does NOT have it at slot 0 — so the old
    order cannot stand, and the re-sequence forced by the new pin must
    publish unconditionally, even though the raw minute/ratio numbers alone
    would otherwise look sub-threshold."""

    def test_previous_order_not_respecting_the_new_pin_publishes_unconditionally(
        self, app, db, driver, customer, monkeypatch
    ):
        from business_app.models.delivery import DeliveryStatusHistory

        d1 = _make_delivery(db, customer.id, driver.id, "ORD-HP-1", 41.300, 69.260)
        d2 = _make_delivery(db, customer.id, driver.id, "ORD-HP-2", 41.300, 69.330)
        with app.app_context():
            svc = RouteOptimizationService()
            # First solve (neither stop committed yet): [d1, d2] wins.
            first = _fixed_matrix_factory({
                (0, 1): 10.0, (0, 2): 40.0, (1, 0): 10.0,
                (1, 2): 30.0, (2, 0): 40.0, (2, 1): 30.0,
            })
            monkeypatch.setattr(
                "business_app.services.maps_service.MapsService.get_distance_matrix", first
            )
            r1 = svc.optimize_for_driver(driver.id, trigger="accept")
            assert r1.optimized_order == [d1.id, d2.id]

            # d2 — currently at slot 1, NOT slot 0 — now becomes the
            # committed stop. The anchor rule (Task 4) forces it to slot 0
            # on the next solve. The previous order [d1, d2] does not
            # respect that pin (d2 sits at index 1, not 0).
            #
            # Mutate a FRESHLY-fetched row, not the `d2` reference from
            # before this `with app.app_context()` block: that reference is
            # bound to the fixture's (outer) scoped session, and a
            # `db.session.commit()` issued from INSIDE this nested context
            # silently drops changes made through it (only newly `add`ed
            # objects, like the history row below, are the inner session's
            # own and actually persist) — no error, just a status mutation
            # that quietly never reaches the database.
            d2_row = Delivery.query.get(d2.id)
            d2_row.status = DeliveryStatus.IN_TRANSIT
            db.session.add(
                DeliveryStatusHistory(
                    delivery_id=d2.id,
                    old_status=DeliveryStatus.PICKED_UP,
                    new_status=DeliveryStatus.IN_TRANSIT,
                    changed_by=driver.id,
                    changed_at=datetime.now(UTC),
                )
            )
            db.session.commit()

            # Second solve is anchored at d2: matrix indices 0=d2 (matrix
            # origin), 1=d1, 2=d2 (again — it's ALSO a delivery node, pinned
            # to slot 0 by the anchor rule; (0,2)/(2,0) are its zero-length
            # self-leg). Also triggers the driver->committed 2-point leg
            # call, handled by `_fixed_matrix_with_leg_factory`. With only
            # ONE free node (d1), the re-solve has no alternative sequence to
            # pick — it is FORCED to [d2, d1] regardless of cost. (2,1) is
            # deliberately scripted asymmetric to (0,1) (9.5 vs 5.0, not a
            # realistic "same two points" distance) so that prev_min (10.0,
            # via 0->1->2) barely beats total_min (9.5, via 0->2->1): gain=
            # 0.5min, decisively below BOTH thresholds. If prev_respects_pins
            # were skipped (the bug this test guards against), the gate would
            # find that sub-threshold gain and incorrectly suppress back to
            # the previous [d1, d2] — the assertion below would then fail.
            second = _fixed_matrix_with_leg_factory({
                (0, 1): 5.0, (0, 2): 0.0, (1, 0): 5.0,
                (1, 2): 5.0, (2, 0): 0.0, (2, 1): 9.5,
            })
            monkeypatch.setattr(
                "business_app.services.maps_service.MapsService.get_distance_matrix", second
            )
            r2 = svc.optimize_for_driver(driver.id, trigger="auto")

            # d2 (now committed) is forced to slot 0 — the set is unchanged
            # but the sequence MUST publish because the pin makes it forced.
            assert r2.optimized_order == [d2.id, d1.id]
            m = (r2.extra_data or {})["materiality"]
            assert m["sequence_changed"] is True
