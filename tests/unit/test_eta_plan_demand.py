"""Demand of the capacity-aware ETA plan (honest-ETA Phase 2, Task 3): every stop the plan must place.

Read through `plannable_stops`, as the engine reads it: the drivers' pool, the stops drivers hold, and the dated orders
held for release (R7), each with what the planner needs of it (R8, R9 and R11's inputs). The rows are the builders'
(`tests/integration/eta_plan_builders.py`) on the test's own clock, and every test hands `now` in, so none depends on
the time of day. The exceptions move a stop through the real staff route, which stamps the real clock: they read at
`get_utc_now()` and assert on status and membership only. `NOW` is 2026-11-04 06:00Z: 11:00 on a Wednesday in Tashkent.

The shared rules this task moved into one owner each are pinned here too: the pool's filter and sort
(`StaffService.pool_filters`, `pool_order_by`), the release sweep's candidate shape
(`OrderScheduleService.held_order_candidates_filter`), the public sequencer (`DispatchService.sequence_active_stops`),
and the `now=` of `is_awaiting_release`, `current_route` and `get_committed_stop`.
"""

import dataclasses
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Dict
from unittest.mock import patch

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.delivery import Delivery
from business_app.models.order import Order, OrderItem
from business_app.models.product import Product, ProductSizeEnum
from business_app.services.dispatch_service import DispatchService
from business_app.services.eta_plan.demand import Stop, plannable_stops
from business_app.services.order_schedule_service import OrderScheduleService
from business_app.services.route_optimization_service import RouteOptimizationService
from business_app.services.staff_service import StaffService
from business_app.utils.delivery_window import window_edge_utc
from business_app.utils.local_windows import work_day_of
from business_app.utils.timezone_utils import get_utc_now
from shared.enums import DeliveryStatus, OrderStatus
from staff_bot.api_client import StaffAPIClient
from tests.integration.eta_plan_builders import (
    bind,
    hold_for_release,
    make_driver,
    open_session,
    order_at_km,
    point_at_km,
    recreate_delivery,
    schedule,
    store_route,
    tap,
)
from tests.integration.eta_plan_builders import dated_order as build_dated_order

pytestmark = pytest.mark.unit

TODAY = date(2026, 11, 4)
NOW = datetime(2026, 11, 4, 6, 0, tzinfo=UTC)  # 11:00 in Tashkent
TOMORROW = TODAY + timedelta(days=1)
# A clock nowhere near the real one: a helper that still read the wall clock could not give the answers asked of it.
FAR = datetime(2031, 3, 10, 6, 0, tzinfo=UTC)
STATUS = "/api/v1/staff/delivery/{}/status"


def at(day: date, hour: int, minute: int = 0) -> datetime:
    """`hour:minute` on the Tashkent wall clock on the local date `day`, as the UTC instant a column holds."""
    return window_edge_utc(day, time(hour, minute))


def stops_of(now: datetime = NOW) -> Dict[int, Stop]:
    """The engine's demand at `now`, by order id: every stop has an order, and not every stop has a delivery."""
    return {stop.order_id: stop for stop in plannable_stops(now)}


def dated_order(db, day, **kwargs) -> Order:
    """The builders' dated order, placed an hour before `NOW` unless the test says when."""
    return build_dated_order(db, day, **{"created_at": NOW - timedelta(hours=1), **kwargs})


def offset_order_ids(db, count=3):
    """Orders that never had a delivery, so the next orders' ids run ahead of their deliveries' (SQLite reuses the
    rowids of an emptied table)."""
    for _ in range(count):
        order = order_at_km(db, 5.0, created_at=NOW - timedelta(days=2), status=OrderStatus.DELIVERED)
        hold_for_release(db, order)


def driver_taps(app, client, driver, delivery_id, status, metadata=None):
    """The staff bot's status tap through the real route, with the body `StaffAPIClient` sends. Its own fan-out (the
    customer update, the route re-sequencing, the route push to the bot) is external I/O and is faked."""
    with app.app_context():
        token = create_access_token(identity=str(driver.id))
    with patch("business_app.tasks.notification_tasks.send_delivery_update_task.delay"), patch(
        "business_app.tasks.delivery_tasks.optimize_driver_route_task.delay"
    ), patch("business_app.utils.bot_webhook._send_staff_bot_webhook", return_value=True):
        response = client.put(
            STATUS.format(delivery_id),
            json=StaffAPIClient.delivery_status_body(status, metadata),
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()["data"]["status"] == status


class TestTheStopRecord:
    def test_a_stop_has_the_fields_the_planner_reads_and_cannot_change(self):
        assert [field.name for field in dataclasses.fields(Stop)] == [
            "delivery_id",
            "order_id",
            "status",
            "driver_id",
            "point",
            "plan_day",
            "customer_date",
            "created_at",
            "window_start",
            "window_end",
            "not_before",
            "bottles",
            "pool_rank",
            "committed",
            "route_index",
        ]
        stop = Stop(None, 1, None, None, None, TODAY, None, NOW, None, None, None, 0, None, False, None)
        with pytest.raises(dataclasses.FrozenInstanceError):
            stop.bottles = 5  # type: ignore[misc]


class TestWhatIsDemand:
    def test_pool_and_assigned_stops_are_demand(self, db):
        driver = make_driver(db)
        pool = order_at_km(db, 2.0, created_at=NOW - timedelta(hours=3), bottles=2)
        preparing = order_at_km(db, 3.0, created_at=NOW - timedelta(hours=2), status=OrderStatus.PREPARING)
        pending_row = order_at_km(db, 1.0, created_at=NOW - timedelta(hours=1))
        pending_row.delivery.status = DeliveryStatus.PENDING  # the other state a pool row rests in
        db.session.commit()
        assigned = order_at_km(db, 4.0, created_at=NOW - timedelta(hours=4), driver=driver, bottles=3)

        stops = plannable_stops(NOW)
        by_order = {stop.order_id: stop for stop in stops}

        assert set(by_order) == {pool.id, preparing.id, pending_row.id, assigned.id}
        # The stops a driver holds come first, then the rest in the order the drivers see the pool.
        assert [stop.order_id for stop in stops] == [assigned.id, pool.id, preparing.id, pending_row.id]
        stop = by_order[pool.id]
        assert stop.point == pytest.approx(point_at_km(2.0))
        assert dataclasses.replace(stop, point=None) == Stop(
            delivery_id=pool.delivery.id,
            order_id=pool.id,
            status=DeliveryStatus.SCHEDULED,
            driver_id=None,
            point=None,
            plan_day=TODAY,
            customer_date=None,
            created_at=NOW - timedelta(hours=3),
            window_start=None,
            window_end=None,
            not_before=None,
            bottles=2,
            pool_rank=1,
            committed=False,
            route_index=None,
        )
        assert by_order[pending_row.id].status is DeliveryStatus.PENDING
        held = by_order[assigned.id]
        assert (held.delivery_id, held.status, held.driver_id) == (
            assigned.delivery.id,
            DeliveryStatus.ASSIGNED,
            driver.id,
        )
        # A held stop is sequenced by its driver's route, not ranked in the pool.
        assert (held.pool_rank, held.bottles, held.committed, held.route_index) == (None, 3, False, None)

    def test_in_flight_stops_on_out_for_delivery_orders_are_demand(self, app, client, db):
        driver = make_driver(db)
        order = order_at_km(db, 2.0, created_at=get_utc_now() - timedelta(hours=2), driver=driver)
        bind(db, open_session(db, driver, get_utc_now() - timedelta(hours=1)), order)
        delivery_id = order.delivery.id

        for step in ("picked_up", "in_transit", "arrived"):
            driver_taps(app, client, driver, delivery_id, step)
            db.session.expire_all()
            # The pickup moved the order out of the pool's statuses, and the stop is still the driver's to make.
            assert db.session.get(Order, order.id).status is OrderStatus.OUT_FOR_DELIVERY
            stops = plannable_stops(get_utc_now())
            assert [(stop.order_id, stop.status, stop.driver_id) for stop in stops] == [
                (order.id, DeliveryStatus(step), driver.id)
            ]

    @pytest.mark.parametrize(
        "order_status, in_the_pool, held_by_a_driver",
        [
            # R7: the pool is confirmed or preparing orders; a driver holds those and the ones already out.
            pytest.param(OrderStatus.CONFIRMED, True, True, id="confirmed"),
            pytest.param(OrderStatus.PREPARING, True, True, id="preparing"),
            pytest.param(OrderStatus.OUT_FOR_DELIVERY, False, True, id="out-for-delivery"),
            pytest.param(OrderStatus.PENDING, False, False, id="pending"),
            pytest.param(OrderStatus.DELIVERED, False, False, id="delivered"),
            pytest.param(OrderStatus.CANCELLED, False, False, id="cancelled"),
            pytest.param(OrderStatus.RETURNED, False, False, id="returned"),
        ],
    )
    def test_every_order_status_is_gated_as_r7_says(self, db, order_status, in_the_pool, held_by_a_driver):
        driver = make_driver(db)
        pool_row = order_at_km(db, 1.0, created_at=NOW - timedelta(hours=2), status=order_status)
        held_row = order_at_km(db, 2.0, created_at=NOW - timedelta(hours=1), status=order_status, driver=driver)

        planned = stops_of()

        assert (pool_row.id in planned, held_row.id in planned) == (in_the_pool, held_by_a_driver)

    @pytest.mark.parametrize("closed", [OrderStatus.DELIVERED, OrderStatus.CANCELLED, OrderStatus.RETURNED])
    def test_pool_rows_on_closed_orders_are_not_demand(self, db, closed):
        driver = make_driver(db)
        live_pool = order_at_km(db, 1.0, created_at=NOW - timedelta(hours=4))
        live_held = order_at_km(db, 2.0, created_at=NOW - timedelta(hours=3), driver=driver)
        # The dev "scheduled on delivered" phantoms: a row still in the pool, or still held, on a finished order.
        order_at_km(db, 3.0, created_at=NOW - timedelta(hours=2), status=closed)
        order_at_km(db, 4.0, created_at=NOW - timedelta(hours=1), status=closed, driver=driver)

        assert set(stops_of()) == {live_pool.id, live_held.id}

    def test_unpaid_orders_are_not_planned(self, db):
        driver = make_driver(db, roster=("08:00", "18:00"))
        # A card order waits for its payment as PENDING, whatever row it has: the pool, the sweep and the plan all
        # leave it alone.
        in_the_pool = order_at_km(db, 1.0, created_at=NOW - timedelta(hours=3), status=OrderStatus.PENDING)
        held_by_a_driver = order_at_km(
            db, 2.0, created_at=NOW - timedelta(hours=2), status=OrderStatus.PENDING, driver=driver
        )
        dated = dated_order(db, TOMORROW)
        parked = dated_order(db, TOMORROW, parked=True)
        for order in (dated, parked):
            order.status = OrderStatus.PENDING
        db.session.commit()

        assert stops_of() == {}

        # The payment lands and the order is confirmed: now it is demand, which is all that status gates.
        for order in (in_the_pool, held_by_a_driver, dated, parked):
            order.status = OrderStatus.CONFIRMED
        db.session.commit()

        assert set(stops_of()) == {in_the_pool.id, held_by_a_driver.id, dated.id, parked.id}

    def test_a_failed_stop_awaiting_a_new_date_is_not_planned(self, app, client, db):
        driver = make_driver(db)
        failed = order_at_km(db, 1.0, created_at=get_utc_now() - timedelta(hours=3), driver=driver)
        waiting = order_at_km(db, 2.0, created_at=get_utc_now() - timedelta(hours=2), driver=driver)
        bind(db, open_session(db, driver, get_utc_now() - timedelta(hours=1)), failed)
        driver_taps(app, client, driver, failed.delivery.id, "picked_up")
        driver_taps(app, client, driver, failed.delivery.id, "failed", {"fail_reason": "customer_unavailable"})
        db.session.expire_all()

        # The order is still live and the row keeps its driver: only the failure marks it as waiting for a new date.
        assert OrderScheduleService.awaiting_new_date(db.session.get(Order, failed.id))
        assert db.session.get(Delivery, failed.delivery.id).delivery_person_id == driver.id
        assert set(stops_of(get_utc_now())) == {waiting.id}

    def test_a_phone_order_is_pool_demand(
        self, client, db, operator_auth_headers, sample_user, user_address, sample_product
    ):
        # An operator keys the order in for a client: it is confirmed at once, and its delivery row is never offered.
        response = client.post(
            "/api/v1/staff/operator/orders",
            json={
                "client_id": sample_user.id,
                "items": [{"product_id": sample_product.id, "quantity": 2}],
                "delivery_address_id": user_address.id,
                "payment_method": "cash",
            },
            headers=operator_auth_headers,
        )
        assert response.status_code == 201, response.get_data(as_text=True)
        order = db.session.get(Order, response.get_json()["data"]["id"])
        assert (order.order_source, order.status) == ("phone", OrderStatus.CONFIRMED)

        stop = stops_of()[order.id]

        assert (stop.delivery_id, stop.status, stop.driver_id) == (order.delivery.id, DeliveryStatus.SCHEDULED, None)
        assert stop.pool_rank == 1
        assert stop.point == pytest.approx((user_address.latitude, user_address.longitude))


class TestDatedOrders:
    def test_a_dated_order_is_planned_on_its_day_never_before_release(self, db):
        make_driver(db, roster=("08:00", "18:00"))  # the day opens at 08:00, when dated orders are released
        no_row = dated_order(db, TOMORROW)
        parked = dated_order(db, TOMORROW, parked=True)
        late = dated_order(db, TOMORROW, window=(time(19, 0), None))  # "after 19:00", later than the release

        stops = stops_of()

        assert set(stops) == {no_row.id, parked.id, late.id}
        for order in (no_row, parked, late):
            stop = stops[order.id]
            assert (stop.driver_id, stop.plan_day, stop.customer_date) == (None, TOMORROW, TOMORROW)
            assert stop.pool_rank is not None
            assert (stop.committed, stop.route_index) == (False, None)
        # No row: nothing to record against yet. A parked row is the delivery that was released once.
        assert (stops[no_row.id].delivery_id, stops[no_row.id].status) == (None, None)
        assert (stops[parked.id].delivery_id, stops[parked.id].status) == (
            parked.delivery.id,
            DeliveryStatus.RESCHEDULED,
        )
        # Never before the release; and never before a window start that comes after it.
        assert stops[no_row.id].not_before == at(TOMORROW, 8)
        assert stops[parked.id].not_before == at(TOMORROW, 8)
        assert stops[late.id].window_start == at(TOMORROW, 19)
        assert stops[late.id].not_before == at(TOMORROW, 19)

    def test_a_dated_order_due_today_is_planned_only_until_its_release(self, db):
        make_driver(db, roster=("08:00", "18:00"))
        due_today = dated_order(db, TODAY)
        release = at(TODAY, 8)

        before = stops_of(release - timedelta(minutes=1))[due_today.id]

        # Dated for today, held until 08:00: today's plan day, and nothing earlier than the release.
        assert (before.plan_day, before.customer_date, before.not_before) == (TODAY, TODAY, release)
        # From the release on the sweep owns it: it gets its delivery row and joins the pool. The release instant is
        # already past, as `is_awaiting_release` has it.
        assert due_today.id not in stops_of(release)
        assert due_today.id not in stops_of(NOW)

    def test_an_undated_order_without_a_delivery_row_is_not_planned(self, db):
        order = order_at_km(db, 2.0, created_at=NOW - timedelta(hours=1))
        hold_for_release(db, order)  # no delivery row and no date: nothing holds it back, and nothing will release it

        assert stops_of() == {}

    def test_a_released_dated_order_is_a_pool_stop_that_waits_only_for_its_window(self, db):
        make_driver(db, roster=("08:00", "18:00"))
        released = order_at_km(db, 2.0, created_at=NOW - timedelta(hours=1))
        schedule(db, released, day=TOMORROW, window=(time(15, 0), time(18, 0)))  # an admin released it early

        stop = stops_of()[released.id]

        # Its row is in the pool, so the release is not what it waits for: only its own window start.
        assert (stop.delivery_id, stop.status) == (released.delivery.id, DeliveryStatus.SCHEDULED)
        assert (stop.plan_day, stop.not_before) == (TOMORROW, at(TOMORROW, 15))

    def test_a_dated_stop_is_ranked_with_the_pool(self, db):
        make_driver(db, roster=("08:00", "18:00"))
        pool = order_at_km(db, 1.0, created_at=NOW - timedelta(hours=3))
        dated = dated_order(db, TOMORROW, created_at=NOW - timedelta(hours=2))
        parked = dated_order(db, TOMORROW, parked=True, created_at=NOW - timedelta(hours=1))

        stops = stops_of()

        assert [stops[order.id].pool_rank for order in (pool, dated, parked)] == [1, 2, 3]


class TestTheCustomersWindow:
    def test_the_customer_window_is_read_from_the_order_not_the_slot_label(self, db):
        order = order_at_km(db, 2.0, created_at=NOW - timedelta(hours=1))
        order.delivery.scheduled_time_slot = "09:00-12:00"  # the legacy label every old row still carries
        schedule(db, order, day=TODAY, window=(time(14, 0), time(18, 0)))
        legacy = order_at_km(db, 3.0, created_at=NOW - timedelta(hours=2))
        legacy.delivery.scheduled_time_slot = "09:00-12:00"
        db.session.commit()

        stops = stops_of()

        assert (stops[order.id].window_start, stops[order.id].window_end) == (at(TODAY, 14), at(TODAY, 18))
        assert stops[order.id].not_before == at(TODAY, 14)
        assert (stops[legacy.id].window_start, stops[legacy.id].window_end) == (None, None)

    @pytest.mark.parametrize(
        "window, start_hour, end_hour",
        [
            pytest.param((None, None), None, None, id="anytime"),
            pytest.param((time(12, 0), time(18, 0)), 12, 18, id="between"),
            pytest.param((None, time(10, 0)), None, 10, id="until"),
            pytest.param((time(19, 0), None), 19, None, id="after"),
        ],
    )
    def test_until_and_after_windows_keep_one_open_edge(self, db, window, start_hour, end_hour):
        order = order_at_km(db, 2.0, created_at=NOW - timedelta(hours=1))
        schedule(db, order, day=TOMORROW, window=window)

        stop = stops_of()[order.id]

        expected_start = at(TOMORROW, start_hour) if start_hour is not None else None
        expected_end = at(TOMORROW, end_hour) if end_hour is not None else None
        assert (stop.window_start, stop.window_end) == (expected_start, expected_end)
        # Only a start edge holds a stop back; an "until" deadline does not.
        assert stop.not_before == expected_start

    def test_an_undated_windows_edges_use_the_local_calendar_date(self, db):
        # 02:00 on 5 Nov in Tashkent is still 4 Nov in UTC (21:00Z).
        placed = at(date(2026, 11, 5), 2)
        assert placed.date() == date(2026, 11, 4)
        after = order_at_km(db, 2.0, created_at=placed)
        schedule(db, after, window=(time(19, 0), None))
        until = order_at_km(db, 3.0, created_at=placed)
        schedule(db, until, window=(None, time(9, 0)))

        stops = stops_of(placed + timedelta(minutes=1))

        assert stops[after.id].window_start == at(date(2026, 11, 5), 19)  # 5 Nov 19:00, not 4 Nov
        assert stops[after.id].window_start != at(date(2026, 11, 4), 19)
        assert stops[until.id].window_end == at(date(2026, 11, 5), 9)


class TestWhatEachStopCarries:
    @pytest.mark.parametrize(
        "now, delivery_date, plan_day",
        [
            pytest.param(NOW, None, TODAY, id="undated"),
            pytest.param(NOW, TODAY, TODAY, id="dated-today"),
            pytest.param(NOW, TODAY - timedelta(days=1), TODAY, id="overdue"),
            pytest.param(NOW, TOMORROW, TOMORROW, id="dated-tomorrow"),
            pytest.param(NOW, TODAY + timedelta(days=3), TODAY + timedelta(days=3), id="dated-later"),
            # 02:00 on the 5th is still the 4th's plan day (it runs 05:00 to 05:00): the 5th is a later day.
            pytest.param(at(TOMORROW, 2), None, TODAY, id="small-hours-undated"),
            pytest.param(at(TOMORROW, 2), TODAY, TODAY, id="small-hours-the-day-before"),
            pytest.param(at(TOMORROW, 2), TOMORROW, TOMORROW, id="small-hours-the-calendar-day"),
            pytest.param(at(TOMORROW, 5), TOMORROW, TOMORROW, id="five-opens-the-new-day"),
            pytest.param(FAR, None, work_day_of(FAR), id="a-clock-far-from-the-real-one"),
        ],
    )
    def test_a_stop_is_on_its_customers_date_when_that_is_after_todays_plan_day(self, db, now, delivery_date, plan_day):
        order = order_at_km(db, 2.0, created_at=now - timedelta(hours=1))
        schedule(db, order, day=delivery_date)

        stop = stops_of(now)[order.id]

        assert (stop.plan_day, stop.customer_date) == (plan_day, delivery_date)

    def test_a_held_stop_with_a_future_date_is_planned_on_that_date_too(self, db):
        driver = make_driver(db)
        order = order_at_km(db, 2.0, created_at=NOW - timedelta(hours=1), driver=driver)
        schedule(db, order, day=TOMORROW)  # an admin gave a dated order to a driver ahead of its day

        stop = stops_of()[order.id]

        assert (stop.plan_day, stop.customer_date, stop.driver_id) == (TOMORROW, TOMORROW, driver.id)

    def test_a_stop_without_coordinates_has_no_point(self, db):
        no_door = order_at_km(db, 2.0, created_at=NOW - timedelta(hours=3))
        no_door.delivery_address.latitude = None
        no_door.delivery_address.longitude = None
        no_address = order_at_km(db, 3.0, created_at=NOW - timedelta(hours=2))
        no_address.delivery_address_id = None
        db.session.commit()
        with_door = order_at_km(db, 1.0, created_at=NOW - timedelta(hours=1))

        stops = stops_of()

        assert stops[no_door.id].point is None
        assert stops[no_address.id].point is None
        assert all(isinstance(value, float) for value in stops[with_door.id].point)

    def test_a_stops_created_at_is_the_orders_in_utc(self, db):
        order = order_at_km(db, 2.0, created_at=at(TODAY, 8, 30))

        created_at = stops_of()[order.id].created_at

        assert created_at == at(TODAY, 8, 30)
        assert created_at.utcoffset() == timedelta(0)

    def test_a_stops_bottles_count_only_returnable_bottles(self, db):
        order = order_at_km(db, 2.0, created_at=NOW - timedelta(hours=1), bottles=3)  # 3 x 19 L, returnable
        ten_litre = Product(
            name="Water 10L",
            sku="ETA-PLAN-WATER-10L",
            category_id=order.order_items[0].product.category_id,
            size=ProductSizeEnum.SIZE_10L,
            base_price=Decimal("9000.00"),
            tracks_returnable_bottles=False,
        )
        db.session.add(ten_litre)
        db.session.flush()
        db.session.add(
            OrderItem(
                order_id=order.id,
                product_id=ten_litre.id,
                quantity=4,
                unit_price=Decimal("9000.00"),
                total_price=Decimal("36000.00"),
            )
        )
        db.session.commit()

        stop = stops_of()[order.id]

        assert (stop.bottles, type(stop.bottles)) == (3, int)  # not 7: the four 10 L bottles stay with the customer

    def test_the_committed_stop_is_the_one_the_optimiser_puts_first(self, db):
        driver = make_driver(db)
        other = make_driver(db)
        older = order_at_km(db, 1.0, created_at=NOW - timedelta(hours=3), driver=driver)
        newer = order_at_km(db, 2.0, created_at=NOW - timedelta(hours=3), driver=driver)
        not_started = order_at_km(db, 3.0, created_at=NOW - timedelta(hours=3), driver=driver)
        elsewhere = order_at_km(db, 4.0, created_at=NOW - timedelta(hours=3), driver=other)
        tap(db, older.delivery, DeliveryStatus.PICKED_UP, NOW - timedelta(minutes=40))
        tap(db, older.delivery, DeliveryStatus.IN_TRANSIT, NOW - timedelta(minutes=30))
        tap(db, newer.delivery, DeliveryStatus.PICKED_UP, NOW - timedelta(minutes=20))
        tap(db, newer.delivery, DeliveryStatus.IN_TRANSIT, NOW - timedelta(minutes=10))
        tap(db, elsewhere.delivery, DeliveryStatus.PICKED_UP, NOW - timedelta(minutes=50))
        tap(db, elsewhere.delivery, DeliveryStatus.ARRIVED, NOW - timedelta(minutes=45))

        stops = stops_of()

        # Of a driver's two in-transit stops only the newer tap is committed; each driver has their own.
        assert {order.id: stops[order.id].committed for order in (older, newer, not_started, elsewhere)} == {
            older.id: False,
            newer.id: True,
            not_started.id: False,
            elsewhere.id: True,
        }
        assert RouteOptimizationService().get_committed_stop(driver.id, now=NOW).id == newer.delivery.id
        # A tap older than the committed-stop age commits nothing, however the stop is held.
        assert not any(stop.committed for stop in plannable_stops(NOW + timedelta(hours=49)))

    def test_route_index_follows_the_stored_delivery_ids(self, db):
        offset_order_ids(db)
        driver = make_driver(db)
        no_route = make_driver(db)
        yesterday = make_driver(db)
        first, second, unsequenced = (
            order_at_km(db, km, created_at=NOW - timedelta(hours=3), driver=driver) for km in (1.0, 2.0, 3.0)
        )
        alone = order_at_km(db, 4.0, created_at=NOW - timedelta(hours=3), driver=no_route)
        stale = order_at_km(db, 5.0, created_at=NOW - timedelta(hours=3), driver=yesterday)
        # The ids of the orders run ahead of their deliveries': a route read as order ids would not find these stops.
        assert all(o.id != o.delivery.id for o in (first, second, unsequenced, alone, stale))
        gone = 10_000  # a stop that left the route (delivered, or taken to the pool) and was not struck from the list
        store_route(db, driver, [second.delivery.id, gone, first.delivery.id], NOW - timedelta(hours=1))
        store_route(db, yesterday, [stale.delivery.id], NOW - timedelta(hours=20))  # 3 Nov 15:00 in Tashkent

        stops = stops_of()

        # The route's position among the stops it still covers: the id of the stop that left is skipped.
        assert stops[second.id].route_index == 0
        assert stops[first.id].route_index == 1
        assert stops[unsequenced.id].route_index is None
        assert stops[alone.id].route_index is None
        assert stops[stale.id].route_index is None  # a route of an earlier day is no plan for today


class TestThePoolOrder:
    def test_the_engine_and_the_driver_pool_share_one_sort(self, db):
        make_driver(db, roster=("08:00", "18:00"))
        base = NOW - timedelta(hours=6)
        old = order_at_km(db, 1.0, created_at=base)
        new = order_at_km(db, 1.1, created_at=base + timedelta(hours=3))
        later = order_at_km(db, 1.2, created_at=base + timedelta(hours=1))
        schedule(db, later, window=(None, time(15, 0)))
        soon = order_at_km(db, 1.3, created_at=base + timedelta(hours=2))  # newer, but its deadline is earlier
        schedule(db, soon, window=(None, time(12, 0)))
        tie_a = order_at_km(db, 1.4, created_at=base + timedelta(minutes=90))
        tie_b = order_at_km(db, 1.5, created_at=base + timedelta(minutes=90))  # the same moment and no window
        recreate_delivery(db, tie_a)  # so the tie is not already in the order of the orders' ids
        assert tie_a.delivery.id > tie_b.delivery.id and tie_a.id < tie_b.id
        dated = dated_order(db, TOMORROW, created_at=base + timedelta(minutes=30))

        drivers_pool = [delivery.id for delivery in StaffService.get_delivery_pool({"per_page": 100})["items"]]
        ranked = sorted(
            (stop for stop in plannable_stops(NOW) if stop.pool_rank is not None), key=lambda s: s.pool_rank
        )

        # Deadlines first, then age, then the delivery id (R18). The dated order is placed among them by the same key.
        expected = [soon, later, old, dated, tie_b, tie_a, new]
        assert [stop.order_id for stop in ranked] == [order.id for order in expected]
        assert [stop.pool_rank for stop in ranked] == list(range(1, 8))
        assert [stop.delivery_id for stop in ranked if stop.delivery_id is not None] == drivers_pool
        assert drivers_pool == [order.delivery.id for order in expected if order is not dated]

    def test_two_dated_orders_with_no_row_are_ranked_by_order_id(self, db):
        make_driver(db, roster=("08:00", "18:00"))
        moment = NOW - timedelta(hours=1)
        first = dated_order(db, TOMORROW, created_at=moment)
        second = dated_order(db, TOMORROW, created_at=moment)
        # Neither has a delivery id to settle the tie. The older order is preparing, and a scan by status and age would
        # reach the confirmed one first.
        first.status = OrderStatus.PREPARING
        db.session.commit()

        stops = stops_of()

        assert (stops[first.id].pool_rank, stops[second.id].pool_rank) == (1, 2)

    def test_the_engines_pool_stops_are_exactly_the_drivers_pool(self, db):
        driver = make_driver(db, roster=("08:00", "18:00"))
        moment = NOW - timedelta(hours=2)
        in_the_pool = [
            order_at_km(db, 1.0, created_at=moment),
            order_at_km(db, 1.1, created_at=moment + timedelta(minutes=1), status=OrderStatus.PREPARING),
        ]
        in_the_pool[1].delivery.status = DeliveryStatus.PENDING
        # None of these is in the pool, though each has a delivery row: a driver holds it, the order is not
        # confirmed, finished or already out, the delivery failed, or it waits for another day.
        order_at_km(db, 2.0, created_at=moment, driver=driver)
        order_at_km(db, 2.1, created_at=moment, status=OrderStatus.PENDING)
        order_at_km(db, 2.2, created_at=moment, status=OrderStatus.DELIVERED)
        order_at_km(db, 2.3, created_at=moment, status=OrderStatus.OUT_FOR_DELIVERY)
        failed = order_at_km(db, 2.4, created_at=moment, driver=driver)
        failed.delivery.status = DeliveryStatus.FAILED
        dated_order(db, TOMORROW, parked=True)
        dated_order(db, TOMORROW)
        db.session.commit()

        drivers_pool = {delivery.id for delivery in StaffService.get_delivery_pool({"per_page": 100})["items"]}
        engine = {
            stop.delivery_id
            for stop in plannable_stops(NOW)
            if stop.status in (DeliveryStatus.SCHEDULED, DeliveryStatus.PENDING)
        }

        assert drivers_pool == {order.delivery.id for order in in_the_pool}
        assert engine == drivers_pool

    def test_the_pool_lookups_keep_only_the_order_gate(self, db):
        driver = make_driver(db)
        in_the_pool = order_at_km(db, 1.0, created_at=NOW - timedelta(hours=3))
        mine = order_at_km(db, 2.0, created_at=NOW - timedelta(hours=2), driver=driver)
        out = order_at_km(
            db, 3.0, created_at=NOW - timedelta(hours=1), status=OrderStatus.OUT_FOR_DELIVERY, driver=driver
        )

        def ids(**filters):
            return [delivery.id for delivery in StaffService.get_delivery_pool(filters)["items"]]

        assert ids() == [in_the_pool.delivery.id]
        # A card looked up by id is the pool's only unless the caller asks for the assigned too...
        assert ids(delivery_id=mine.delivery.id) == []
        assert ids(delivery_id=mine.delivery.id, include_assigned=True) == [mine.delivery.id]
        assert ids(order_id=mine.id, include_assigned=True) == [mine.delivery.id]
        assert ids(order_id=in_the_pool.id) == [in_the_pool.delivery.id]
        # ...and the order gate applies to a lookup as it does to the list.
        assert ids(delivery_id=out.delivery.id, include_assigned=True) == []
        assert ids(order_id=out.id, include_assigned=True) == []


class TestTheSharedRules:
    @pytest.mark.parametrize(
        "order_status, row, dated, is_a_candidate",
        [
            pytest.param(OrderStatus.CONFIRMED, "none", True, True, id="confirmed-no-row"),
            pytest.param(OrderStatus.PREPARING, "none", True, True, id="preparing-no-row"),
            pytest.param(OrderStatus.CONFIRMED, "rescheduled", True, True, id="confirmed-rescheduled"),
            pytest.param(OrderStatus.CONFIRMED, "none", False, False, id="no-date"),
            pytest.param(OrderStatus.CONFIRMED, "scheduled", True, False, id="already-in-the-pool"),
            pytest.param(OrderStatus.CONFIRMED, "assigned", True, False, id="held-by-a-driver"),
            pytest.param(OrderStatus.CONFIRMED, "failed", True, False, id="failed"),
            pytest.param(OrderStatus.PENDING, "none", True, False, id="unpaid"),
            pytest.param(OrderStatus.OUT_FOR_DELIVERY, "none", True, False, id="out-for-delivery"),
            pytest.param(OrderStatus.CANCELLED, "rescheduled", True, False, id="cancelled"),
        ],
    )
    def test_held_order_candidates_are_the_shape_the_release_sweep_selects(
        self, db, order_status, row, dated, is_a_candidate
    ):
        driver = make_driver(db)
        order = order_at_km(db, 2.0, created_at=NOW - timedelta(hours=1), status=order_status)
        schedule(db, order, day=TOMORROW if dated else None)
        if row == "none":
            hold_for_release(db, order)
        elif row == "rescheduled":
            hold_for_release(db, order, parked=True)
        elif row == "assigned":
            order.delivery.delivery_person_id = driver.id
            order.delivery.status = DeliveryStatus.ASSIGNED
        elif row == "failed":
            order.delivery.delivery_person_id = driver.id
            order.delivery.status = DeliveryStatus.FAILED
        db.session.commit()

        candidates = (
            Order.query.outerjoin(Delivery, Delivery.order_id == Order.id)
            .filter(OrderScheduleService.held_order_candidates_filter())
            .all()
        )

        assert (order in candidates) is is_a_candidate

    def test_is_awaiting_release_reads_the_clock_it_is_given(self, db):
        make_driver(db, roster=("08:00", "18:00"))
        order = dated_order(db, TOMORROW)
        release = at(TOMORROW, 8)

        assert OrderScheduleService.is_awaiting_release(order, now=release - timedelta(seconds=1)) is True
        assert OrderScheduleService.is_awaiting_release(order, now=release) is False
        # Without one, the module's clock governs, as the release sweep's own tests have it.
        with patch(
            "business_app.services.order_schedule_service.get_utc_now", return_value=release - timedelta(minutes=1)
        ):
            assert OrderScheduleService.is_awaiting_release(order) is True
        with patch(
            "business_app.services.order_schedule_service.get_utc_now", return_value=release + timedelta(minutes=1)
        ):
            assert OrderScheduleService.is_awaiting_release(order) is False

    def test_the_route_helpers_read_the_clock_they_are_given(self, db):
        driver = make_driver(db)
        order = order_at_km(db, 2.0, created_at=FAR - timedelta(hours=4), driver=driver)
        tap(db, order.delivery, DeliveryStatus.PICKED_UP, FAR - timedelta(minutes=20))
        tap(db, order.delivery, DeliveryStatus.IN_TRANSIT, FAR - timedelta(minutes=10))
        route = store_route(db, driver, [order.delivery.id], FAR - timedelta(hours=1))
        service = RouteOptimizationService()

        # Today's route and the committed stop are as of the instant asked about...
        assert service.current_route(driver.id, now=FAR).id == route.id
        assert service.get_committed_stop(driver.id, now=FAR).id == order.delivery.id
        # ...so a day on the route is yesterday's, and two days on the tap is too old to commit the stop.
        assert service.current_route(driver.id, now=FAR + timedelta(days=1)) is None
        assert service.get_committed_stop(driver.id, now=FAR + timedelta(hours=49)) is None

    def test_the_public_sequencer_orders_a_drivers_stops_by_the_stored_route(self, db):
        driver = make_driver(db)
        a, b, c = (order_at_km(db, km, created_at=NOW - timedelta(hours=3), driver=driver).delivery for km in (1, 2, 3))
        stored = [c.id, 9999, "junk", a.id]  # a stop that left, and whatever the legacy writer put in the column

        assert DispatchService.sequence_active_stops([a, b, c], stored) == [c, a, b]
        assert DispatchService.sequence_active_stops([a, b, c], stored, include_unsequenced=False) == [c, a]
        assert DispatchService.sequence_active_stops([c, b, a], None) == [a, b, c]


class TestQueryDiscipline:
    def test_demand_costs_a_bounded_number_of_queries(self, db, count_queries):
        driver = make_driver(db, roster=("08:00", "18:00"))

        def add_stops(count):
            for index in range(count):
                moment = NOW - timedelta(hours=5, minutes=index)
                order_at_km(db, 1.0 + index / 10, created_at=moment, bottles=2)
                order_at_km(db, 1.0 + index / 10, created_at=moment, bottles=2, driver=driver)
                dated_order(db, TOMORROW, parked=index % 2 == 0, created_at=moment)

        add_stops(2)
        plannable_stops(NOW)  # the roster the release reads is cached for the app context: warm it
        with count_queries() as few:
            small = plannable_stops(NOW)
        add_stops(10)
        with count_queries() as many:
            large = plannable_stops(NOW)

        assert (len(small), len(large)) == (6, 36)
        # The driver is the same one: no query grows with the stops (the items and products, the doors and the
        # deliveries all come with the orders).
        assert many.count == few.count
