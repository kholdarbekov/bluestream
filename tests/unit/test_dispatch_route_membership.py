"""Who is on a driver's dispatch route — ownership decides, not the JSON list.

`DeliveryRoute.optimized_order` is a plain JSON id list with no foreign key and
no write trigger. Historically the dispatch read model treated it as the answer
to BOTH questions it appears to answer:

  * WHICH stops are this driver's        (membership)
  * in WHAT sequence they should be run  (ordering)

Only the second is safe. Membership already has a single source of truth —
`Delivery.delivery_person_id` plus an active status — and it is the one the
WRITE guard uses (`RouteEditService.set_stop_order` validates against
`RouteOptimizationService.active_deliveries`). With the read model deriving
membership from the JSON list instead, the two disagreed the moment ANY of the
~10 ownership write paths that don't maintain the list moved a delivery: the
old driver kept the stop on their panel and their polyline forever, the same
delivery rendered under two drivers at once, and every Save on the affected
route 409'd with a conflict the "Reload route" button could not clear.

These tests pin the split: membership from ownership, sequence from
`optimized_order`.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from business_app.models.delivery import Delivery, DeliveryRoute
from business_app.models.user import UserAddress
from business_app.services.dispatch_service import DispatchService
from shared.enums import DeliveryStatus, OrderStatus


def _delivery(db, sample_user, sample_order, *, driver_id, status=DeliveryStatus.ASSIGNED, lat=41.31, lng=69.25):
    """One geocoded, dispatchable delivery owned by `driver_id` (may be None)."""
    address = UserAddress(
        user_id=sample_user.id,
        full_address=f"Chilonzor {lat}",
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
        status=OrderStatus.OUT_FOR_DELIVERY,
        payment_method=sample_order.payment_method,
        delivery_address_id=address.id,
        delivery_date=datetime.now(timezone.utc),
    )
    db.session.add(order)
    db.session.flush()
    delivery = Delivery(
        order_id=order.id,
        delivery_person_id=driver_id,
        status=status,
        scheduled_date=datetime.now(timezone.utc),
        scheduled_time_slot="09:00-12:00",
    )
    db.session.add(delivery)
    db.session.flush()
    return delivery


def _route(db, driver_id, delivery_ids, **kwargs):
    route = DeliveryRoute(
        name="r",
        delivery_person_id=driver_id,
        start_location_lat=41.30,
        start_location_lng=69.24,
        route_date=datetime.now(timezone.utc),
        optimized_order=list(delivery_ids),
        **kwargs,
    )
    db.session.add(route)
    db.session.flush()
    return route


def _stops_for(driver_id, snapshot):
    for route in snapshot["routes"]:
        if route["driver_id"] == driver_id:
            return [s["delivery_id"] for s in route["stops"]]
    return None


class TestMembershipComesFromOwnership:
    def test_stop_reassigned_to_another_driver_leaves_the_old_drivers_route(
        self, db, sample_user, sample_order, delivery_driver, second_delivery_driver
    ):
        """The reported bug, minimal.

        Reassignment through anything other than the dispatch map's own move
        button (the admin Delivery page, a bulk action, a bot claim) rewrites
        `delivery_person_id` and nothing else. The old driver's route panel
        and polyline must not keep advertising work they no longer have.
        """
        delivery = _delivery(db, sample_user, sample_order, driver_id=second_delivery_driver.id)
        _route(db, delivery_driver.id, [delivery.id])
        db.session.commit()

        snapshot = DispatchService.get_snapshot(date.today())

        assert _stops_for(delivery_driver.id, snapshot) == []

    def test_a_delivery_is_never_listed_under_two_drivers_at_once(
        self, db, sample_user, sample_order, delivery_driver, second_delivery_driver
    ):
        """Exactly the screenshot: the same order number under both panels.

        Once the id is in A's stale list AND has been spliced into B's, both
        route dicts rendered it and the map drew two polylines through it.
        """
        delivery = _delivery(db, sample_user, sample_order, driver_id=second_delivery_driver.id)
        _route(db, delivery_driver.id, [delivery.id])
        _route(db, second_delivery_driver.id, [delivery.id])
        db.session.commit()

        snapshot = DispatchService.get_snapshot(date.today())

        assert _stops_for(delivery_driver.id, snapshot) == []
        assert _stops_for(second_delivery_driver.id, snapshot) == [delivery.id]

    def test_stop_returned_to_the_pool_leaves_the_route_it_came_from(
        self, db, sample_user, sample_order, delivery_driver
    ):
        """`StaffService.return_delivery_to_pool` nulls the owner and sets
        SCHEDULED, which makes the delivery pool-eligible. Only ONE of its
        callers prunes the route, so the same delivery could be offered in the
        unassigned pool while still numbered as a stop on a driver's route —
        assigning it from the pool then read as a duplicate rather than a move.
        """
        delivery = _delivery(
            db, sample_user, sample_order, driver_id=None, status=DeliveryStatus.SCHEDULED
        )
        _route(db, delivery_driver.id, [delivery.id])
        db.session.commit()

        snapshot = DispatchService.get_snapshot(date.today())

        assert _stops_for(delivery_driver.id, snapshot) == []
        assert [p["delivery_id"] for p in snapshot["pool"]] == [delivery.id]

    @pytest.mark.parametrize(
        "status", [DeliveryStatus.DELIVERED, DeliveryStatus.CANCELLED, DeliveryStatus.FAILED]
    )
    def test_finished_stop_drops_off_the_route(
        self, db, sample_user, sample_order, delivery_driver, status
    ):
        """The route panel is remaining work. A finished stop kept a numbered
        slot and a polyline vertex until some unrelated re-solve happened to
        rebuild the list — in the dev DB, a delivery cancelled at 13:49 stayed
        on the route until the next morning's sweep.

        This is also what keeps the read model equal to the write guard: a
        DELIVERED stop is not in `active_deliveries`, so leaving it in `stops`
        guaranteed a 409 on the next Save.
        """
        delivery = _delivery(db, sample_user, sample_order, driver_id=delivery_driver.id, status=status)
        _route(db, delivery_driver.id, [delivery.id])
        db.session.commit()

        snapshot = DispatchService.get_snapshot(date.today())

        assert _stops_for(delivery_driver.id, snapshot) == []


class TestSequenceStillComesFromOptimizedOrder:
    def test_optimized_order_decides_the_sequence_of_the_stops_the_driver_owns(
        self, db, sample_user, sample_order, delivery_driver
    ):
        """Narrowing membership must not cost the hand-authored sequence."""
        first = _delivery(db, sample_user, sample_order, driver_id=delivery_driver.id, lat=41.31)
        second = _delivery(db, sample_user, sample_order, driver_id=delivery_driver.id, lat=41.32)
        _route(db, delivery_driver.id, [second.id, first.id])
        db.session.commit()

        snapshot = DispatchService.get_snapshot(date.today())

        assert _stops_for(delivery_driver.id, snapshot) == [second.id, first.id]

    def test_owned_stop_missing_from_optimized_order_is_appended_not_dropped(
        self, db, sample_user, sample_order, delivery_driver
    ):
        """The mirror of the reported bug, and just as damaging.

        Three of the four assign paths never splice the new stop into the
        receiving driver's route, so a freshly claimed order was invisible on
        the dispatch board until that driver's next location ping. Ownership
        decides membership in BOTH directions: an unsequenced stop is shown at
        the end rather than hidden.
        """
        sequenced = _delivery(db, sample_user, sample_order, driver_id=delivery_driver.id, lat=41.31)
        unsequenced = _delivery(db, sample_user, sample_order, driver_id=delivery_driver.id, lat=41.32)
        _route(db, delivery_driver.id, [sequenced.id])
        db.session.commit()

        snapshot = DispatchService.get_snapshot(date.today())

        assert _stops_for(delivery_driver.id, snapshot) == [sequenced.id, unsequenced.id]

    def test_positions_are_contiguous_after_a_stop_is_filtered_out(
        self, db, sample_user, sample_order, delivery_driver, second_delivery_driver
    ):
        """`position` is what the map numbers its pins with (the panel
        re-indexes independently). A gap here shows a pin labelled "3" beside
        a panel row labelled "2".
        """
        kept = _delivery(db, sample_user, sample_order, driver_id=delivery_driver.id, lat=41.31)
        moved = _delivery(db, sample_user, sample_order, driver_id=second_delivery_driver.id, lat=41.32)
        tail = _delivery(db, sample_user, sample_order, driver_id=delivery_driver.id, lat=41.33)
        _route(db, delivery_driver.id, [kept.id, moved.id, tail.id])
        db.session.commit()

        routes = DispatchService.get_snapshot(date.today())["routes"]
        stops = next(r for r in routes if r["driver_id"] == delivery_driver.id)["stops"]

        assert [s["delivery_id"] for s in stops] == [kept.id, tail.id]
        assert [s["position"] for s in stops] == [0, 1]


class TestGeometryUsesTheSameMembership:
    def test_route_stop_points_excludes_stops_the_driver_no_longer_owns(
        self, db, sample_user, sample_order, delivery_driver, second_delivery_driver
    ):
        """The polyline, `distance_km`, `duration_minutes` and every per-leg
        figure are computed over whatever this returns. Left unfiltered, the
        map drew a solid, real-road path for driver A through driver B's stops.
        """
        kept = _delivery(db, sample_user, sample_order, driver_id=delivery_driver.id, lat=41.31)
        moved = _delivery(db, sample_user, sample_order, driver_id=second_delivery_driver.id, lat=41.32)
        route = _route(db, delivery_driver.id, [kept.id, moved.id])
        db.session.commit()

        points = DispatchService.route_stop_points(route)

        assert [delivery_id for delivery_id, _point in points] == [kept.id]

    def test_route_stop_points_appends_an_owned_stop_absent_from_the_sequence(
        self, db, sample_user, sample_order, delivery_driver
    ):
        """`leg_delivery_ids` must describe the same stop set the panel shows,
        or the per-leg distances are attributed to the wrong rows.
        """
        sequenced = _delivery(db, sample_user, sample_order, driver_id=delivery_driver.id, lat=41.31)
        unsequenced = _delivery(db, sample_user, sample_order, driver_id=delivery_driver.id, lat=41.32)
        route = _route(db, delivery_driver.id, [sequenced.id])
        db.session.commit()

        points = DispatchService.route_stop_points(route)

        assert [delivery_id for delivery_id, _point in points] == [sequenced.id, unsequenced.id]


class TestTheSelectedDayAppliesToRoutesToo:
    """`get_snapshot(target_date)` honoured the date for orders, the pool and
    the unmapped list, then called the route builder with no argument at all.
    `_routes()` derived its own window from *now*, so picking any other day
    swapped the order markers underneath an unchanged set of route panels and
    polylines — today's plan, presented as that day's.
    """

    def test_todays_route_is_not_shown_on_another_days_board(
        self, db, sample_user, sample_order, delivery_driver
    ):
        delivery = _delivery(db, sample_user, sample_order, driver_id=delivery_driver.id)
        _route(db, delivery_driver.id, [delivery.id])
        db.session.commit()

        snapshot = DispatchService.get_snapshot(date.today() - timedelta(days=1))

        assert snapshot["routes"] == []

    def test_a_past_days_route_is_shown_on_that_day_and_not_on_today(
        self, db, sample_user, sample_order, delivery_driver
    ):
        delivery = _delivery(db, sample_user, sample_order, driver_id=delivery_driver.id)
        route = _route(db, delivery_driver.id, [delivery.id])
        route.route_date = datetime.now(timezone.utc) - timedelta(days=1)
        db.session.commit()

        yesterday = DispatchService.get_snapshot(date.today() - timedelta(days=1))["routes"]
        today = DispatchService.get_snapshot(date.today())["routes"]

        assert [r["driver_id"] for r in yesterday] == [delivery_driver.id]
        assert today == []

    def test_a_stop_the_driver_picked_up_today_is_not_appended_to_an_older_route(
        self, db, sample_user, sample_order, delivery_driver
    ):
        """Appending an owned-but-unsequenced stop is a statement about the
        CURRENT plan — three of the four assign paths lag the sequence, and the
        board must not hide live work. A historical route row carries no such
        promise, and quietly growing it would be a fresh way of showing one
        day's work under another day's heading.
        """
        old_stop = _delivery(db, sample_user, sample_order, driver_id=delivery_driver.id, lat=41.31)
        todays_stop = _delivery(db, sample_user, sample_order, driver_id=delivery_driver.id, lat=41.32)
        route = _route(db, delivery_driver.id, [old_stop.id])
        route.route_date = datetime.now(timezone.utc) - timedelta(days=1)
        db.session.commit()

        routes = DispatchService.get_snapshot(date.today() - timedelta(days=1))["routes"]

        assert [s["delivery_id"] for s in routes[0]["stops"]] == [old_stop.id]
        assert todays_stop.id not in [s["delivery_id"] for s in routes[0]["stops"]]
