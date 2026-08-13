"""Manual-override policy.

'The delivery set' means the driver's active deliveries (ASSIGNED, PICKED_UP,
IN_TRANSIT, ARRIVED), compared order-insensitively against optimized_order.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from business_app.models.delivery import Delivery, DeliveryRoute, DeliveryStatusHistory
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


def _fixed_matrix_factory(minutes_by_leg):
    """Scripted-duration matrix stub (mirrors
    test_route_hysteresis.py's helper of the same name). `minutes_by_leg`
    maps (i, j) -> minutes; km mirrors minutes so both metrics move
    together. This file patches the `svc.maps` INSTANCE (not the class), so
    unlike test_route_hysteresis.py's version this callable takes no `self`."""

    def fake(points, traffic=True, use_cache=True):
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

    def test_unchanged_set_still_refreshes_committed_delivery_id_when_it_shifts(
        self, db, driver_with_location, sample_user, sample_order
    ):
        """The delivery SET can stay identical while WHICH stop is committed
        changes — the driver starts a different stop than the one that was
        committed at the last real solve. `_apply_override_policy`'s
        'unchanged' branch skips the solve entirely (matrix not called,
        admin's sequence stands untouched), but
        extra_data['committed_delivery_id'] must still track the CURRENT
        committed stop, not whatever was true last time — Task 5/Plan 3's
        card and Task 6's hysteresis both read this field."""
        d1 = make_delivery(db, driver_with_location, sample_user, sample_order, 41.31, 69.25)
        d1.status = DeliveryStatus.IN_TRANSIT
        db.session.add(
            DeliveryStatusHistory(
                delivery_id=d1.id,
                old_status=DeliveryStatus.PICKED_UP,
                new_status=DeliveryStatus.IN_TRANSIT,
                changed_by=driver_with_location.id,
                changed_at=datetime.now(timezone.utc) - timedelta(minutes=30),
            )
        )
        d2 = make_delivery(db, driver_with_location, sample_user, sample_order, 41.32, 69.26)
        route = DeliveryRoute(
            name="r",
            delivery_person_id=driver_with_location.id,
            start_location_lat=41.30,
            start_location_lng=69.24,
            route_date=datetime.now(timezone.utc),
            optimized_order=[d1.id, d2.id],
            manual_override=True,
            pinned_stops={},
            extra_data={"committed_delivery_id": d1.id, "start_source": "committed_stop"},
        )
        db.session.add(route)
        db.session.commit()

        # The driver starts a DIFFERENT stop. The active SET is unchanged
        # (still {d1, d2}) -- only which one is currently "committed" shifts.
        d2.status = DeliveryStatus.IN_TRANSIT
        db.session.add(
            DeliveryStatusHistory(
                delivery_id=d2.id,
                old_status=DeliveryStatus.PICKED_UP,
                new_status=DeliveryStatus.IN_TRANSIT,
                changed_by=driver_with_location.id,
                changed_at=datetime.now(timezone.utc),
            )
        )
        db.session.commit()

        svc = RouteOptimizationService()
        with patch.object(svc.maps, "get_distance_matrix") as matrix:
            result = svc.optimize_for_driver(driver_with_location.id)

        matrix.assert_not_called()
        assert result.optimized_order == [d1.id, d2.id]  # admin sequence untouched
        assert (result.extra_data or {}).get("committed_delivery_id") == d2.id

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

    def test_shrink_refreshes_stale_committed_delivery_id(
        self, db, driver_with_location, sample_user, sample_order
    ):
        """extra_data['committed_delivery_id'] must never outlive the stop it
        names. A route anchored on d1 (committed IN_TRANSIT) whose set then
        SHRINKS because d1 itself completes must not keep pointing
        `committed_delivery_id` at the delivery that just finished — Task
        5/Plan 3's card and Task 6's hysteresis both read this field, and
        `_apply_override_policy`'s shrink branch does not re-solve, so
        nothing else would refresh it."""
        d1 = make_delivery(db, driver_with_location, sample_user, sample_order, 41.31, 69.25)
        d1.status = DeliveryStatus.IN_TRANSIT
        db.session.add(
            DeliveryStatusHistory(
                delivery_id=d1.id,
                old_status=DeliveryStatus.PICKED_UP,
                new_status=DeliveryStatus.IN_TRANSIT,
                changed_by=driver_with_location.id,
                changed_at=datetime.now(timezone.utc),
            )
        )
        d2 = make_delivery(db, driver_with_location, sample_user, sample_order, 41.32, 69.26)
        route = DeliveryRoute(
            name="r",
            delivery_person_id=driver_with_location.id,
            start_location_lat=41.30,
            start_location_lng=69.24,
            route_date=datetime.now(timezone.utc),
            optimized_order=[d1.id, d2.id],
            manual_override=True,
            pinned_stops={},
            extra_data={"committed_delivery_id": d1.id, "start_source": "committed_stop"},
        )
        db.session.add(route)
        db.session.commit()

        # d1 (the committed stop) itself completes -> the set SHRINKS.
        d1.status = DeliveryStatus.DELIVERED
        db.session.commit()

        svc = RouteOptimizationService()
        with patch.object(svc.maps, "get_distance_matrix") as matrix:
            result = svc.optimize_for_driver(driver_with_location.id)

        matrix.assert_not_called()
        assert result.optimized_order == [d2.id]
        assert (result.extra_data or {}).get("committed_delivery_id") is None


class TestOverrideGrowWithCommittedStop:
    def test_admin_pin_at_slot_zero_wins_over_the_committed_stop(
        self, db, driver_with_location, sample_user, sample_order
    ):
        """The admin-pin-vs-committed-pin collision: the admin explicitly
        pinned d2 to slot 0. d1 is a committed (IN_TRANSIT) stop. Per the
        spec's tie-break (admin intent always wins a collision at slot 0),
        d2 must hold position 0 in the re-solve, and the persisted
        `pinned_stops` must equal the admin's own pins alone — the derived
        committed-stop pin must never appear in it, collision or not."""
        d1 = make_delivery(db, driver_with_location, sample_user, sample_order, 41.35, 69.32)
        d1.status = DeliveryStatus.IN_TRANSIT
        db.session.add(
            DeliveryStatusHistory(
                delivery_id=d1.id,
                old_status=DeliveryStatus.PICKED_UP,
                new_status=DeliveryStatus.IN_TRANSIT,
                changed_by=driver_with_location.id,
                changed_at=datetime.now(timezone.utc),
            )
        )
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

        # Grows the active set -> forces a real re-solve under the override.
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
        assert result.optimized_order[0] == d2.id  # admin pin wins the collision
        assert result.pinned_stops == {str(d2.id): 0}  # admin's pins alone — no committed leak
        assert sorted(result.optimized_order) == sorted([d1.id, d2.id, d3.id])


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


class TestOverrideGrowHysteresisInteraction:
    """Route-UX plan 2026-08-11 review round: admin pins are a load-bearing
    product feature (dispatch pins urgent stops to the front of the queue),
    and Task 6's hysteresis gate must never weaken that. A GROWN override
    already skips the gate because the delivery SETS differ (proven
    structurally: `_apply_override_policy` only falls through to a real
    re-solve when the set strictly grew, and the hysteresis gate only
    activates when the set is unchanged — those two conditions cannot both
    hold). This test locks that in with a real, adversarial scenario rather
    than only a comment."""

    def test_grown_set_publishes_despite_a_negative_naive_gain_and_keeps_the_pin(
        self, db, driver_with_location, sample_user, sample_order
    ):
        """d1 (created first) and d2 (created second) give matrix indices
        1 and 2 respectively (id-ascending); d3 (added below) is index 3.
        The admin pinned d2 to slot 0. Distances are scripted so that the
        OLD 2-stop order [d2, d1], RE-COSTED ON THE NEW (post-grow) matrix,
        looks *cheaper* than the real 3-stop re-solve: prev [d2,d1] costs
        8+6=14; the actual re-solve [d2,d3,d1] costs 8+5+4=17. A naive
        `prev_min - total_min` would be 14-17 = -3 -- decisively below both
        ROUTE_RESEQUENCE_MIN_GAIN_MINUTES and ROUTE_RESEQUENCE_MIN_GAIN_RATIO.
        A gate that compared them without first checking whether the SET
        changed would suppress this and silently drop d3 from the published
        route. It must not: the set changed, so the gate never runs at all,
        and the admin's pin on d2 must still hold slot 0 afterwards."""
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

        d3 = make_delivery(db, driver_with_location, sample_user, sample_order, 41.315, 69.255)
        db.session.commit()

        # Matrix indices follow `deliveries` (id-ascending): 0=start, 1=d1,
        # 2=d2, 3=d3.
        matrix_fn = _fixed_matrix_factory({
            (0, 1): 10.0, (0, 2): 8.0, (0, 3): 9.0,
            (1, 0): 10.0, (1, 2): 6.0, (1, 3): 4.0,
            (2, 0): 8.0, (2, 1): 6.0, (2, 3): 5.0,
            (3, 0): 9.0, (3, 1): 4.0, (3, 2): 5.0,
        })

        svc = RouteOptimizationService()
        with patch.object(svc.maps, "get_distance_matrix", side_effect=matrix_fn):
            result = svc.optimize_for_driver(driver_with_location.id)

        assert result.optimized_order[0] == d2.id  # admin pin still holds slot 0
        assert d3.id in result.optimized_order      # the grown set published
        assert sorted(result.optimized_order) == sorted([d1.id, d2.id, d3.id])
        assert result.manual_override is True
        assert result.pinned_stops == {str(d2.id): 0}  # pin untouched, unclamped-away
        m = (result.extra_data or {})["materiality"]
        assert m["set_changed"] is True
        assert m["sequence_changed"] is True


class TestClampPins:
    def test_drops_missing_and_compacts_positions(self):
        clamped = RouteOptimizationService.clamp_pins({"7": 0, "9": 5, "11": 2}, [7, 11])
        assert clamped == {"7": 0, "11": 1}

    def test_empty_input_is_empty_dict(self):
        assert RouteOptimizationService.clamp_pins(None, [1, 2]) == {}
