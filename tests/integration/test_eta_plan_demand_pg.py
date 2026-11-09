"""Demand on the real database (honest-ETA Phase 2, Task 3): the pool's SQL as Postgres runs it.

`tests/unit/test_eta_plan_demand.py` runs the same rules on SQLite. This file runs them on a fully migrated Postgres,
where the enum columns, the CHECK constraints and the ordering of NULLs are production's: the drivers' pool
(`StaffService.get_delivery_pool`, whose filter and sort moved into `pool_filters` and `pool_order_by`) and the ETA
engine's demand (`plannable_stops`, which selects the pool and the dated orders in one outer-joined statement and ranks
them by that same sort) have to read the same rows in the same order. The rows are the builders' on a fixed `NOW`.
"""

from datetime import UTC, date, datetime, time, timedelta

import pytest

from business_app.services.eta_plan.demand import plannable_stops
from business_app.services.staff_service import StaffService
from shared.enums import DeliveryStatus, OrderStatus
from tests.integration.eta_plan_builders import (
    dated_order,
    make_driver,
    order_at_km,
    recreate_delivery,
    schedule,
    store_route,
    tap,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 4, 6, 0, tzinfo=UTC)  # 11:00 in Tashkent
TOMORROW = date(2026, 11, 5)


def _drivers_pool(**filters):
    """The delivery ids the drivers' pool returns, in its order."""
    return [delivery.id for delivery in StaffService.get_delivery_pool({"per_page": 100, **filters})["items"]]


def test_the_engine_ranks_the_pool_in_the_order_the_drivers_see_it(pg_app, pg_db):
    make_driver(pg_db, roster=("08:00", "18:00"))
    base = NOW - timedelta(hours=6)
    old = order_at_km(pg_db, 1.0, created_at=base)
    new = order_at_km(pg_db, 1.1, created_at=base + timedelta(hours=3))
    soon = order_at_km(pg_db, 1.2, created_at=base + timedelta(hours=2))  # newer, but its deadline is earlier
    schedule(pg_db, soon, window=(None, time(12, 0)))
    later = order_at_km(pg_db, 1.3, created_at=base + timedelta(hours=1))
    schedule(pg_db, later, window=(None, time(15, 0)))
    tie_a = order_at_km(pg_db, 1.4, created_at=base + timedelta(minutes=90))
    tie_b = order_at_km(pg_db, 1.5, created_at=base + timedelta(minutes=90))  # the same moment, no window
    recreate_delivery(pg_db, tie_a)  # so the tie is not already in the order of the orders' ids
    dated = dated_order(pg_db, TOMORROW, created_at=base + timedelta(minutes=30))
    # None of these is in the pool, though each has an order and most have a delivery row.
    order_at_km(pg_db, 2.0, created_at=base, status=OrderStatus.PENDING)
    order_at_km(pg_db, 2.1, created_at=base, status=OrderStatus.DELIVERED)
    order_at_km(pg_db, 2.2, created_at=base, status=OrderStatus.OUT_FOR_DELIVERY)

    ranked = sorted((s for s in plannable_stops(NOW) if s.pool_rank is not None), key=lambda s: s.pool_rank)

    # Deadlines first, then age, then the delivery id; the dated order is placed among them by the same key.
    expected = [soon, later, old, dated, tie_b, tie_a, new]
    assert [stop.order_id for stop in ranked] == [order.id for order in expected]
    assert [stop.pool_rank for stop in ranked] == list(range(1, 8))
    pool = _drivers_pool()
    assert pool == [order.delivery.id for order in expected if order is not dated]
    assert [stop.delivery_id for stop in ranked if stop.delivery_id is not None] == pool


def test_the_pool_lookups_and_the_engine_agree_on_postgres_about_who_holds_what(pg_app, pg_db):
    driver = make_driver(pg_db, roster=("08:00", "18:00"))
    in_the_pool = order_at_km(pg_db, 1.0, created_at=NOW - timedelta(hours=4))
    mine = order_at_km(pg_db, 2.0, created_at=NOW - timedelta(hours=3), driver=driver)
    on_the_road = order_at_km(pg_db, 3.0, created_at=NOW - timedelta(hours=3), driver=driver)
    tap(pg_db, on_the_road.delivery, DeliveryStatus.PICKED_UP, NOW - timedelta(minutes=30))
    tap(pg_db, on_the_road.delivery, DeliveryStatus.IN_TRANSIT, NOW - timedelta(minutes=20))
    store_route(pg_db, driver, [on_the_road.delivery.id, mine.delivery.id], NOW - timedelta(hours=1))
    parked = dated_order(pg_db, TOMORROW, parked=True, created_at=NOW - timedelta(hours=2))
    failed = order_at_km(pg_db, 4.0, created_at=NOW - timedelta(hours=3), driver=driver)
    tap(pg_db, failed.delivery, DeliveryStatus.FAILED, NOW - timedelta(hours=1))

    stops = {stop.order_id: stop for stop in plannable_stops(NOW)}

    # The lookups keep the order gate alone: an assigned card appears only on request, and an order already out for
    # delivery is not in the pool at all.
    assert _drivers_pool() == [in_the_pool.delivery.id]
    assert _drivers_pool(delivery_id=mine.delivery.id) == []
    assert _drivers_pool(delivery_id=mine.delivery.id, include_assigned=True) == [mine.delivery.id]
    assert _drivers_pool(order_id=on_the_road.id, include_assigned=True) == []
    # The engine plans what the pool offers, what drivers hold (the stop on the road included) and the order that
    # waits for its day; a failed delivery waits for a new date and is not planned.
    assert set(stops) == {in_the_pool.id, mine.id, on_the_road.id, parked.id}
    assert (stops[on_the_road.id].status, stops[on_the_road.id].committed) == (DeliveryStatus.IN_TRANSIT, True)
    assert (stops[on_the_road.id].route_index, stops[mine.id].route_index) == (0, 1)
    assert (stops[parked.id].status, stops[parked.id].plan_day) == (DeliveryStatus.RESCHEDULED, TOMORROW)
    assert stops[in_the_pool.id].pool_rank == 1
    assert failed.id not in stops
