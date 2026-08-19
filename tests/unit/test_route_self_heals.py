"""A stale `optimized_order` must be able to fix itself.

Write-side bookkeeping (`DeliveryAssignmentService` / `StaffService`) keeps the
stored sequence honest for ownership changes from now on. It cannot help two
other cases:

  * rows that are ALREADY wrong, written before that bookkeeping existed;
  * a delivery leaving the active set without changing hands — cancelled by the
    customer, or failed at the door.

Both used to persist for the rest of the working day, because every path that
rewrites `optimized_order` needed the driver to still have active deliveries and
the daily sweep only enumerated drivers who did. A driver stripped of every stop
was, precisely and only because they had been stripped, the one driver nothing
would ever revisit.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from business_app.models.delivery import Delivery, DeliveryRoute
from business_app.models.user import UserAddress
from business_app.services.route_optimization_service import RouteOptimizationService
from shared.enums import DeliveryStatus, OrderStatus


def _delivery(db, user, sample_order, *, order_number, driver_id, status=DeliveryStatus.ASSIGNED):
    address = UserAddress(user_id=user.id, full_address=order_number, city="Tashkent",
                          latitude=41.31, longitude=69.25)
    db.session.add(address)
    db.session.flush()
    order = sample_order.__class__(
        user_id=user.id, order_number=order_number, total_amount=sample_order.total_amount,
        status=OrderStatus.OUT_FOR_DELIVERY, payment_method=sample_order.payment_method,
        delivery_address_id=address.id, delivery_date=datetime.now(timezone.utc),
    )
    db.session.add(order)
    db.session.flush()
    delivery = Delivery(order_id=order.id, delivery_person_id=driver_id, status=status,
                        scheduled_date=datetime.now(timezone.utc), scheduled_time_slot="09:00-12:00")
    db.session.add(delivery)
    db.session.flush()
    return delivery


def _route(db, driver_id, delivery_ids, **kwargs):
    route = DeliveryRoute(
        name="r", delivery_person_id=driver_id,
        start_location_lat=41.30, start_location_lng=69.24,
        route_date=datetime.now(timezone.utc), optimized_order=list(delivery_ids), **kwargs,
    )
    db.session.add(route)
    db.session.flush()
    return route


class TestAnEmptiedRouteClearsItself:
    def test_a_plain_route_is_cleared_when_the_driver_has_no_active_deliveries(
        self, db, delivery_driver, sample_user, sample_order,
    ):
        """The clearing branch used to run only for `manual_override` routes.
        Every route row in the dev database has `manual_override = false`, so in
        practice it never ran at all: a driver whose stops were all reassigned
        kept the full original list, and the panel kept drawing it.
        """
        gone = _delivery(db, sample_user, sample_order, order_number="ORD-SH-1",
                         driver_id=delivery_driver.id, status=DeliveryStatus.DELIVERED)
        route = _route(db, delivery_driver.id, [gone.id])
        db.session.commit()

        assert RouteOptimizationService().optimize_for_driver(delivery_driver.id) is None

        db.session.refresh(route)
        assert route.optimized_order == []

    def test_clearing_also_drops_the_pins_that_pointed_into_it(
        self, db, delivery_driver, sample_user, sample_order,
    ):
        gone = _delivery(db, sample_user, sample_order, order_number="ORD-SH-2",
                         driver_id=delivery_driver.id, status=DeliveryStatus.DELIVERED)
        route = _route(db, delivery_driver.id, [gone.id], pinned_stops={str(gone.id): 0})
        db.session.commit()

        RouteOptimizationService().optimize_for_driver(delivery_driver.id)

        db.session.refresh(route)
        assert route.pinned_stops == {}

    def test_an_already_empty_route_is_left_alone(
        self, db, delivery_driver,
    ):
        """No stops and nothing stored is not a repair job — it must not churn
        the row (and with it `metrics_stale`) on every location ping."""
        route = _route(db, delivery_driver.id, [], extra_data={"metrics_stale": False})
        db.session.commit()

        RouteOptimizationService().optimize_for_driver(delivery_driver.id)

        db.session.refresh(route)
        assert route.extra_data.get("metrics_stale") is False


class TestTheDailySweepReachesEveryDriverThatNeedsIt:
    def test_a_driver_whose_only_stop_is_arrived_is_still_enumerated(
        self, db, delivery_driver, sample_user, sample_order,
    ):
        """ARRIVED is in `ACTIVE_DELIVERY_STATUSES`, so such a driver has a real
        route to optimise — but the sweep's own status list omitted it.
        """
        from business_app.tasks.delivery_tasks import optimize_daily_delivery_routes

        _delivery(db, sample_user, sample_order, order_number="ORD-SW-1",
                  driver_id=delivery_driver.id, status=DeliveryStatus.ARRIVED)
        db.session.commit()

        with patch("business_app.tasks.delivery_tasks.optimize_driver_route_task") as task:
            optimize_daily_delivery_routes()

        assert [c.args[0] for c in task.delay.call_args_list] == [delivery_driver.id]

    def test_a_driver_stripped_of_every_stop_is_enumerated_so_their_route_can_clear(
        self, db, delivery_driver,
    ):
        """The gap that made the bug permanent. The sweep started from the
        DELIVERIES, so the one driver guaranteed to need repair — the one with
        a route row and nothing left on it — was the one it could never find.
        """
        from business_app.tasks.delivery_tasks import optimize_daily_delivery_routes

        _route(db, delivery_driver.id, [999])
        db.session.commit()

        with patch("business_app.tasks.delivery_tasks.optimize_driver_route_task") as task:
            optimize_daily_delivery_routes()

        assert [c.args[0] for c in task.delay.call_args_list] == [delivery_driver.id]

    def test_a_driver_is_enumerated_once_even_with_several_active_stops(
        self, db, delivery_driver, sample_user, sample_order,
    ):
        from business_app.tasks.delivery_tasks import optimize_daily_delivery_routes

        _delivery(db, sample_user, sample_order, order_number="ORD-SW-3A",
                  driver_id=delivery_driver.id, status=DeliveryStatus.ASSIGNED)
        _delivery(db, sample_user, sample_order, order_number="ORD-SW-3B",
                  driver_id=delivery_driver.id, status=DeliveryStatus.ARRIVED)
        _route(db, delivery_driver.id, [1])
        db.session.commit()

        with patch("business_app.tasks.delivery_tasks.optimize_driver_route_task") as task:
            optimize_daily_delivery_routes()

        assert [c.args[0] for c in task.delay.call_args_list] == [delivery_driver.id]

    def test_yesterdays_route_row_does_not_drag_a_driver_into_todays_sweep(
        self, db, delivery_driver,
    ):
        """Route rows accumulate one per driver per day. Only a route inside the
        current day window can be the one a reader is showing, so only that one
        is worth repairing.
        """
        from business_app.tasks.delivery_tasks import optimize_daily_delivery_routes

        stale = _route(db, delivery_driver.id, [999])
        stale.route_date = datetime.now(timezone.utc) - timedelta(days=3)
        db.session.commit()

        with patch("business_app.tasks.delivery_tasks.optimize_driver_route_task") as task:
            optimize_daily_delivery_routes()

        assert task.delay.call_args_list == []


class TestAStopThatEndsAtTheDoorLeavesTheSequence:
    """FAILED and CANCELLED take a delivery out of the active set without
    changing hands, so neither assignment SSOT ever sees them. They are the
    other exit from a driver's route, and it was unguarded: the stop kept its
    numbered slot on the panel and its vertex on the polyline. The dev database
    holds a worked example — delivery 78 was cancelled at 13:49 on 2026-08-14
    and stayed on route 105 until the next morning's sweep rebuilt the list.
    """

    def test_a_failed_stop_leaves_the_drivers_stored_sequence(
        self, db, delivery_driver, sample_user, sample_order, admin_user,
    ):
        from business_app.services.staff_service import StaffService

        kept = _delivery(db, sample_user, sample_order, order_number="ORD-T-FAIL-A",
                         driver_id=delivery_driver.id, status=DeliveryStatus.IN_TRANSIT)
        ending = _delivery(db, sample_user, sample_order, order_number="ORD-T-FAIL-B",
                           driver_id=delivery_driver.id, status=DeliveryStatus.IN_TRANSIT)
        route = _route(db, delivery_driver.id, [kept.id, ending.id])
        db.session.commit()

        StaffService.update_delivery_status(
            ending.id, "failed", admin_user.id, metadata={"fail_reason": "customer_unavailable"}
        )

        db.session.refresh(route)
        assert route.optimized_order == [kept.id]

    def test_a_stop_cancelled_with_its_order_leaves_the_drivers_stored_sequence(
        self, db, delivery_driver, sample_user, sample_order,
    ):
        from business_app.services.order_service import OrderService

        kept = _delivery(db, sample_user, sample_order, order_number="ORD-T-CANC-A",
                         driver_id=delivery_driver.id)
        ending = _delivery(db, sample_user, sample_order, order_number="ORD-T-CANC-B",
                           driver_id=delivery_driver.id)
        route = _route(db, delivery_driver.id, [kept.id, ending.id])
        db.session.commit()

        OrderService()._cancel_delivery_for_cancelled_order(ending.order)
        db.session.commit()

        db.session.refresh(route)
        assert route.optimized_order == [kept.id]


class TestRawOwnershipWritesAlsoClearTheSequence:
    """Two paths write `delivery_person_id` directly instead of going through
    an assignment SSOT. Bookkeeping placed only in the SSOTs cannot reach them,
    so each is its own way of stranding a stop on a route nobody drives.
    """

    def test_marking_a_delivery_returned_from_the_admin_panel_clears_its_slot(
        self, db, delivery_driver, sample_user, sample_order, admin_user,
    ):
        """`AdminDeliveryService._apply_status_update` nulls the owner itself
        for RETURNED / SCHEDULED / PENDING. The parent order also leaves
        `ACTIVE_ORDER_STATUSES`, so the order vanished from the board's order
        layer while remaining a numbered stop on a driver's route.
        """
        from business_app.services.admin_delivery_service import AdminDeliveryService

        kept = _delivery(db, sample_user, sample_order, order_number="ORD-RAW-1A",
                         driver_id=delivery_driver.id)
        returned = _delivery(db, sample_user, sample_order, order_number="ORD-RAW-1B",
                             driver_id=delivery_driver.id)
        route = _route(db, delivery_driver.id, [kept.id, returned.id])
        db.session.commit()

        AdminDeliveryService.update_delivery(returned.id, {"status": "returned"}, actor_id=admin_user.id)

        db.session.refresh(route)
        assert route.optimized_order == [kept.id]

    def test_auto_rescheduling_a_failed_delivery_clears_its_slot(
        self, db, delivery_driver, sample_user, sample_order,
    ):
        """`reschedule_failed_delivery_task` clears the driver and re-enqueues
        auto-assign. Left on the old route, the delivery is drawn on two
        drivers' routes as soon as the next one picks it up.
        """
        from business_app.tasks.delivery_tasks import reschedule_failed_delivery_task

        kept = _delivery(db, sample_user, sample_order, order_number="ORD-RAW-2A",
                         driver_id=delivery_driver.id)
        failed = _delivery(db, sample_user, sample_order, order_number="ORD-RAW-2B",
                           driver_id=delivery_driver.id, status=DeliveryStatus.FAILED)
        # The task reads `estimated_delivery_time` to rebuild tomorrow's slot.
        failed.estimated_delivery_time = datetime.now(timezone.utc)
        route = _route(db, delivery_driver.id, [kept.id, failed.id])
        db.session.commit()

        with patch("business_app.tasks.delivery_tasks.auto_assign_delivery_task"):
            reschedule_failed_delivery_task(failed.id)

        db.session.refresh(route)
        assert route.optimized_order == [kept.id]
