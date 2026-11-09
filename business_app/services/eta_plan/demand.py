"""Demand: the stops the plan must place (honest-ETA Phase 2 plan, R7, R8, R9).

The planner places `Stop`s on cars and reads nothing else about orders, so what is waiting, for which window, with how
many bottles, and where a driver's route has it, is decided here, once. Three sets are demand (R7):

- the drivers' pool, exactly: `StaffService.pool_filters()`, the default branch of `get_delivery_pool`;
- the stops drivers hold, in flight ones included: `driver_held_rows_filter()`. A pickup moves the order to
  OUT_FOR_DELIVERY, so this set reaches past the pool's order statuses;
- dated orders held for release: the sweep's candidate shape (`OrderScheduleService.held_order_candidates_filter()`)
  while `is_awaiting_release(order, now=now)` still says the release is to come. They have no delivery row, or a
  RESCHEDULED one, and are planned on their date, never before `release_at`.

Not demand: PENDING orders, rows awaiting a new date (a FAILED delivery), and pool-status rows on any other order
status.

`pool_rank` is one total order over every stop that is not driver-held, pool rows and dated ones alike: the drivers'
own pool sort (`StaffService.pool_order_by()`) with the order id after it, so a stop with no delivery row has a place
too. That is SQL's ORDER BY and nothing else: no second sort exists in Python. A driver-held stop is sequenced by R8
instead: `committed` marks the stop `RouteOptimizationService.get_committed_stop` returns for its driver, and
`route_index` is its place in the driver's stored route for today (`current_route`), as
`DispatchService.sequence_active_stops` reads it.

The queries are bounded: the stops drivers hold, and the pool with the dated candidates, each with its door, its items
and their products loaded alongside; then `get_committed_stop` and `current_route` once per driver who holds a stop.
None per stop. Every rule reads the one `now` the caller passes.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import or_
from sqlalchemy.orm import contains_eager, joinedload, selectinload

from business_app.models.delivery import Delivery
from business_app.models.order import Order, OrderItem
from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.services.dispatch_service import DispatchService
from business_app.services.eta_plan.scope import driver_held_rows_filter
from business_app.services.order_schedule_service import OrderScheduleService
from business_app.services.route_optimization_service import RouteOptimizationService
from business_app.services.staff_service import StaffService
from business_app.utils.delivery_window import window_edge_utc
from business_app.utils.local_windows import local_date, work_day_of
from business_app.utils.timezone_utils import ensure_utc
from shared.enums import DeliveryStatus

Point = Tuple[float, float]


@dataclass(frozen=True)
class Stop:
    """One stop the plan must place, as the planner reads it. Every instant is aware UTC, and every id is `users.id`
    or the row's own.

    - `delivery_id`, `status`: None for a dated order that has no delivery row yet. `driver_id` is
      `Delivery.delivery_person_id`: set for the stops a driver holds, None for every other stop.
    - `point`: the door as (lat, lng), None when the address has no coordinates (the planner then adds
      ETA_UNKNOWN_LEG_KM to the previous point).
    - `plan_day`: the order's `customer_date` when that is after today's plan day, else today's.
    - `created_at`: the order's, which tells R9's two groups apart.
    - `window_start`, `window_end`: the customer's own window as instants, each None when open ("until" has no start,
      "after" has no end). `not_before`: the later of the window start and, for a dated order held for release, its
      `release_at`: the planner never places the stop earlier.
    - `bottles`: the returnable bottles the order books (R6), whole.
    - `pool_rank`: 1 for the first stop of the pool order, over every stop that is not driver-held; None for a stop a
      driver holds. `committed`: this is the driver's committed stop (R8 step 1). `route_index`: the stop's place among
      the driver's stops in their stored route for today, None when the route does not cover it or there is none.
    """

    delivery_id: Optional[int]
    order_id: int
    status: Optional[DeliveryStatus]
    driver_id: Optional[int]
    point: Optional[Point]
    plan_day: date
    customer_date: Optional[date]
    created_at: datetime
    window_start: Optional[datetime]
    window_end: Optional[datetime]
    not_before: Optional[datetime]
    bottles: int
    pool_rank: Optional[int]
    committed: bool
    route_index: Optional[int]


def plannable_stops(now: datetime) -> List[Stop]:
    """Every stop the plan must place as of `now`: first the stops drivers hold, by delivery id, then the pool and the
    dated stops in `pool_rank` order."""
    now = ensure_utc(now)
    today = work_day_of(now)
    held = _driver_held_orders()
    committed_by_driver, route_index_by_delivery = _route_facts(held, now)
    stops = [
        _stop(
            order,
            now,
            today,
            pool_rank=None,
            committed=order.delivery.id == committed_by_driver.get(order.delivery.delivery_person_id),
            route_index=route_index_by_delivery.get(order.delivery.id),
        )
        for order in held
    ]
    stops.extend(
        _stop(order, now, today, pool_rank=rank, committed=False, route_index=None)
        for rank, order in enumerate(_waiting_orders(now), start=1)
    )
    return stops


def _order_loads():
    """What a stop reads off its order, loaded with the order: the delivery (the caller joins it explicitly), the door,
    and the items with their products. The item collection is a `selectinload`, as in `DispatchService.get_snapshot`: a
    joined collection would multiply the order rows."""
    return (
        contains_eager(Order.delivery),
        joinedload(Order.delivery_address),
        selectinload(Order.order_items).joinedload(OrderItem.product),
    )


def _driver_held_orders() -> List[Order]:
    """The orders whose delivery a driver holds (R7), by delivery id."""
    return (
        Order.query.join(Delivery, Delivery.order_id == Order.id)
        .filter(driver_held_rows_filter())
        .options(*_order_loads())
        .order_by(Delivery.id.asc())
        .all()
    )


def _waiting_orders(now: datetime) -> List[Order]:
    """The pool's rows and the dated orders held for release, in the one total order of `pool_rank`.

    One statement for both, ordered by the drivers' own pool sort with the order id after it: a stop with no delivery
    row has no delivery id to break a tie, and the order id does."""
    rows = (
        Order.query.outerjoin(Delivery, Delivery.order_id == Order.id)
        .filter(or_(StaffService.pool_filters(), OrderScheduleService.held_order_candidates_filter()))
        .options(*_order_loads())
        .order_by(*StaffService.pool_order_by(), Order.id.asc())
        .all()
    )
    # A pool row stays. A held candidate stays only while its release is still to come: once it has passed, the sweep
    # gives the order a delivery row and it is a pool row from then on.
    return [
        order
        for order in rows
        if (order.delivery is not None and StaffService.is_delivery_claimable(order.delivery))
        or OrderScheduleService.is_awaiting_release(order, now=now)
    ]


def _route_facts(held: Sequence[Order], now: datetime) -> Tuple[Dict[int, int], Dict[int, int]]:
    """R8's inputs for each driver who holds a stop: the delivery id of their committed stop, by driver id, and the
    place of each stop in their stored route for today, by delivery id.

    The place is the index among the driver's stops the route still covers, through `sequence_active_stops` without its
    unsequenced tail: an id left in the stored list by a stop that has moved on is skipped, and so is a stop nothing has
    sequenced."""
    by_driver: Dict[int, List[Delivery]] = defaultdict(list)
    for order in held:
        by_driver[order.delivery.delivery_person_id].append(order.delivery)
    routes = RouteOptimizationService()
    committed: Dict[int, int] = {}
    route_index: Dict[int, int] = {}
    for driver_id, deliveries in sorted(by_driver.items()):
        stop = routes.get_committed_stop(driver_id, now=now)
        if stop is not None:
            committed[driver_id] = stop.id
        route = routes.current_route(driver_id, now=now)
        if route is not None:
            sequenced = DispatchService.sequence_active_stops(
                deliveries, route.optimized_order, include_unsequenced=False
            )
            route_index.update((delivery.id, index) for index, delivery in enumerate(sequenced))
    return committed, route_index


def _stop(
    order: Order, now: datetime, today: date, *, pool_rank: Optional[int], committed: bool, route_index: Optional[int]
) -> Stop:
    delivery = order.delivery
    created_at = ensure_utc(order.created_at)
    # A customer's window is wall-clock time on their date. An order with no date has the local calendar date it was
    # placed on, as `delivery_window.schedule_error_codes` and the visit service read it (R7).
    window_day = order.delivery_date or local_date(created_at)
    window_start = _window_edge(window_day, order.delivery_window_start)
    window_end = _window_edge(window_day, order.delivery_window_end)
    release_at = (
        OrderScheduleService.release_at(order) if OrderScheduleService.is_awaiting_release(order, now=now) else None
    )
    return Stop(
        delivery_id=delivery.id if delivery is not None else None,
        order_id=order.id,
        status=delivery.status if delivery is not None else None,
        driver_id=delivery.delivery_person_id if delivery is not None else None,
        point=_door(order),
        plan_day=order.delivery_date if order.delivery_date is not None and order.delivery_date > today else today,
        customer_date=order.delivery_date,
        created_at=created_at,
        window_start=window_start,
        window_end=window_end,
        not_before=max((edge for edge in (window_start, release_at) if edge is not None), default=None),
        bottles=int(BottleTrackingService.calculate_bottles_for_order(order)),
        pool_rank=pool_rank,
        committed=committed,
        route_index=route_index,
    )


def _window_edge(day: date, local_time: Optional[time]) -> Optional[datetime]:
    return window_edge_utc(day, local_time) if local_time is not None else None


def _door(order: Order) -> Optional[Point]:
    address = order.delivery_address
    if address is None or address.latitude is None or address.longitude is None:
        return None
    return float(address.latitude), float(address.longitude)
