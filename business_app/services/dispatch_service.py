"""Read model for the admin dispatch map.

One call returns everything the map draws for a single day: the active orders,
the drivers and where they are, each driver's planned stop sequence, the
unassigned pool, and — deliberately — the orders that CANNOT be drawn, split
into WHY: no resolved schedule at all (`not_scheduled`) or a schedule but no
address coordinates (`no_coordinates`). A map that silently omits those
implies it is showing all the work, and an order with neither reason surfaced
is exactly the one that gets forgotten.

An order's schedule is resolved as `order.delivery_date` if set, else its
`delivery.scheduled_date` — `order.delivery_date` is nullable and, in
production, NULL on every active order; `Delivery.scheduled_date` is the
field that is actually populated (`nullable=False` by schema) and the one the
rest of the delivery system already runs on.

Read-only: no writes, no external API calls. Route geometry is a separate,
cached endpoint precisely so this stays cheap enough to poll.
"""

from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from flask import current_app
from sqlalchemy import func, or_
from sqlalchemy.orm import joinedload, selectinload

from business_app.models.delivery import Delivery, DeliveryPerson, DeliveryRoute
from business_app.models.order import Order, OrderItem
from business_app.models.user import User, UserAddress
from business_app.serializers.order_serializers import summarize_order_items
from business_app.services.route_optimization_service import (
    RouteOptimizationService,
    _driver_day_start_utc,
)
from business_app.utils.state_validators import DELIVERY_POOL_UNASSIGNED_STATES
from shared.enums import OrderStatus, PaymentMethod


class DispatchService:
    """Snapshot builder for `GET /admin/dispatch/snapshot`."""

    # Everything between "the customer placed it" and "it is off the board".
    # `PREPARING` is included on purpose: omitting it would make an order
    # disappear from the map mid-pipeline and reappear once it went out.
    ACTIVE_ORDER_STATUSES = (
        OrderStatus.PENDING,
        OrderStatus.CONFIRMED,
        OrderStatus.PREPARING,
        OrderStatus.OUT_FOR_DELIVERY,
    )

    # Allowlist, not a blocklist: the same set staff_service.get_delivery_pool
    # and assert_unassigned_for_pool_status use for "this delivery is actually
    # sitting in the pool". A blocklist (e.g. "not terminal") would admit
    # anything nobody thought to exclude — an ASSIGNED delivery that somehow
    # lost its driver would still read as claimable. The allowlist only ever
    # admits the two states a pooled delivery is allowed to be in, and keeps
    # the map's notion of "pool" identical to the one the rest of the system
    # enforces instead of a second definition that can drift.
    _POOL_DELIVERY_STATUS_VALUES = {s.value for s in DELIVERY_POOL_UNASSIGNED_STATES}

    # ----- public API -------------------------------------------------------

    @classmethod
    def get_snapshot(cls, target_date: date) -> Dict[str, Any]:
        end_of_day = cls._day_bounds(target_date)[1]
        today_start = cls._day_bounds(cls.today())[0]

        # `order.delivery_date` if set, else `delivery.scheduled_date` — see
        # module docstring. COALESCE at the SQL level (not a Python filter
        # after a narrower query) so a row whose `orders.delivery_date` is
        # NULL but whose `deliveries.scheduled_date` is set is never dropped
        # before it reaches Python.
        resolved_schedule = func.coalesce(Order.delivery_date, Delivery.scheduled_date)

        rows = (
            Order.query.options(
                joinedload(Order.user),
                joinedload(Order.delivery_address),
                joinedload(Order.delivery),
                # `selectinload`, not `joinedload`: this query already carries
                # three outer joins and calls `.all()` without `.unique()`, and
                # fanning a collection into that row set multiplies the parent
                # rows. Two extra round-trips total (items, then products) —
                # versus two PER ORDER if these load lazily, on an endpoint the
                # dispatch board polls every 30 seconds.
                selectinload(Order.order_items).joinedload(OrderItem.product),
            )
            # `orders` carries more than one FK to `users`; an unpinned
            # join(User) either raises or silently resolves the wrong one.
            .join(User, Order.user_id == User.id)
            .outerjoin(UserAddress, Order.delivery_address_id == UserAddress.id)
            .outerjoin(Delivery, Delivery.order_id == Order.id)
            .filter(
                Order.status.in_(cls.ACTIVE_ORDER_STATUSES),
                # SQL `NULL <= x` is falsy, so a bare `resolved_schedule <=
                # end_of_day` silently drops any active order with no
                # resolved schedule at all — it would never reach `orders` OR
                # `unmapped`. Fetch it regardless of the selected day; the
                # loop below routes it to `unmapped` (reason `not_scheduled`)
                # instead of pretending it doesn't exist.
                or_(resolved_schedule.is_(None), resolved_schedule <= end_of_day),
            )
            .order_by(resolved_schedule.asc(), Order.id.asc())
            .all()
        )

        orders: List[Dict[str, Any]] = []
        unmapped: List[Dict[str, Any]] = []
        for order in rows:
            address = order.delivery_address
            schedule = cls._resolved_schedule(order)
            if schedule is None:
                # Precedence: an unscheduled order is not on any day's board,
                # regardless of whether its address happens to be geocoded.
                unmapped.append(cls._unmapped_entry(order, address, "not_scheduled"))
                continue
            entry = cls._order_entry(order, today_start, schedule)
            if address is None or address.latitude is None or address.longitude is None:
                unmapped.append(cls._unmapped_entry(order, address, "no_coordinates"))
                continue
            orders.append(entry)

        pool = [
            o
            for o in orders
            if o["delivery_id"] is not None
            and o["driver_id"] is None
            and o["delivery_status"] in cls._POOL_DELIVERY_STATUS_VALUES
        ]

        return {
            "date": target_date.isoformat(),
            "orders": orders,
            "unmapped": unmapped,
            "pool": pool,
            "drivers": cls._drivers(),
            "routes": cls._routes(),
        }

    @classmethod
    def route_stop_coordinates(cls, route: DeliveryRoute) -> List[Tuple[float, float]]:
        """Ordered `(lat, lng)` for one route's planned stops, in
        `optimized_order` sequence, skipping any stop whose address has no
        coordinates.

        Public — used by the cached geometry endpoint, which resolves stops
        for exactly ONE selected driver's route. Deliberately NOT routed
        through `get_snapshot()`: geometry is polled per selected driver (and
        on a timer), so rebuilding every order/driver/route for one polyline
        would be a lot of query for nothing. Keeps the API layer free of any
        direct `Delivery`/`Order` model access (service-layer-first).
        """
        return [point for _delivery_id, point in cls.route_stop_points(route)]

    @classmethod
    def route_stop_points(cls, route: DeliveryRoute) -> List[Tuple[int, Tuple[float, float]]]:
        """The same resolved stops as `route_stop_coordinates`, but each one
        still carrying the delivery it belongs to.

        This exists because the filter above is lossy: a stop with no
        coordinates is silently absent from the returned sequence, so the
        Nth point is NOT necessarily the Nth entry of `optimized_order`. Any
        caller that wants to attribute a per-point result — the geometry
        endpoint's per-leg distances, for instance — back to a stop has to be
        told which stops actually made it in. Re-deriving that by zipping
        against `optimized_order` gets it silently wrong on exactly the routes
        that have an ungeocoded stop.
        """
        if not route.optimized_order:
            return []

        deliveries = (
            Delivery.query.options(joinedload(Delivery.order).joinedload(Order.delivery_address))
            .filter(Delivery.id.in_(route.optimized_order))
            .all()
        )
        by_id = {d.id: d for d in deliveries}
        stops: List[Tuple[int, Tuple[float, float]]] = []
        for delivery_id in route.optimized_order:
            delivery = by_id.get(delivery_id)
            address = delivery.order.delivery_address if delivery and delivery.order else None
            if address and address.latitude is not None and address.longitude is not None:
                stops.append((delivery_id, (float(address.latitude), float(address.longitude))))
        return stops

    # ----- internals --------------------------------------------------------

    @staticmethod
    def _value(enum_or_str):
        return enum_or_str.value if hasattr(enum_or_str, "value") else enum_or_str

    @staticmethod
    def _tz() -> ZoneInfo:
        return ZoneInfo(current_app.config.get("DISPLAY_TIMEZONE", "Asia/Tashkent"))

    @classmethod
    def today(cls) -> date:
        """Operator-local today. Public: the API layer calls this to default the date parameter."""
        return datetime.now(cls._tz()).date()

    @classmethod
    def _day_bounds(cls, day: date):
        """Operator-local day boundaries as UTC-aware datetimes.

        The admin picks a calendar day in Tashkent, not in UTC; computing this
        in UTC would slice the day five hours off.
        """
        tz = cls._tz()
        start = datetime.combine(day, time.min, tzinfo=tz)
        return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)

    @staticmethod
    def _address_label(address: Optional[UserAddress]) -> str:
        if address is None:
            return ""
        return address.full_address or address.street_address or address.district or ""

    @staticmethod
    def _resolved_schedule(order: Order) -> Optional[datetime]:
        """`order.delivery_date` if set, else `order.delivery.scheduled_date`.

        `Order.delivery` is an existing one-to-one relationship, already
        eager-loaded by `get_snapshot`'s query — this does not issue a query
        of its own.
        """
        if order.delivery_date is not None:
            return order.delivery_date
        delivery = order.delivery
        return delivery.scheduled_date if delivery else None

    @classmethod
    def _unmapped_entry(cls, order: Order, address: Optional[UserAddress], reason: str) -> Dict[str, Any]:
        return {
            "order_id": order.id,
            "order_number": order.order_number,
            "status": cls._value(order.status),
            "customer_name": order.user.full_name if order.user else "",
            "customer_phone": order.user.phone if order.user else "",
            "address_label": cls._address_label(address),
            "reason": reason,
        }

    @classmethod
    def _order_entry(cls, order: Order, today_start: datetime, schedule: datetime) -> Dict[str, Any]:
        address = order.delivery_address
        delivery = order.delivery
        payment_method = cls._value(order.payment_method)
        if schedule.tzinfo is None:
            schedule = schedule.replace(tzinfo=timezone.utc)
        return {
            "order_id": order.id,
            "order_number": order.order_number,
            "status": cls._value(order.status),
            "delivery_id": delivery.id if delivery else None,
            "delivery_status": cls._value(delivery.status) if delivery else None,
            "driver_id": delivery.delivery_person_id if delivery else None,
            "lat": float(address.latitude) if address and address.latitude is not None else None,
            "lng": float(address.longitude) if address and address.longitude is not None else None,
            "address_label": cls._address_label(address),
            "customer_name": order.user.full_name if order.user else "",
            "customer_phone": order.user.phone if order.user else "",
            "user_id": order.user_id,
            "total_amount": float(order.total_amount or 0),
            "payment_method": payment_method,
            "is_cod": payment_method == PaymentMethod.CASH.value,
            "time_slot": order.delivery_time_slot,
            "delivery_date": schedule.isoformat(),
            "is_overdue": bool(schedule < today_start),
            **cls._items_fields(order),
        }

    @staticmethod
    def _items_fields(order: Optional[Order]) -> Dict[str, Any]:
        """What's in the order, for the stop and pool cards.

        Shared by both builders on purpose. `pool[]` is a filtered view over
        the dicts `_order_entry` produces while route stops are assembled
        separately, so a dispatcher can see the same order on either panel —
        if only one of them carried items, the same delivery would list its
        contents in the pool and lose them the moment it was assigned.

        Truncation is not decided here: `summarize_order_items` owns the limit
        and the hidden count (see order_serializers.py). Flattened into the
        parent dict rather than nested under `items_summary`, because that name
        already means a STRING on the delivery rows and an ARRAY on the Orders
        page — a third meaning on a third screen is how those two drifted.
        """
        summary = summarize_order_items(order)
        return {
            "items": summary["items"],
            "items_total_count": summary["total_count"],
            "items_hidden_count": summary["hidden_count"],
        }

    @classmethod
    def _drivers(cls) -> List[Dict[str, Any]]:
        people = (
            DeliveryPerson.query.options(joinedload(DeliveryPerson.user))
            .filter(DeliveryPerson.is_active.is_(True))
            .all()
        )
        drivers = []
        for person in people:
            user = person.user
            drivers.append(
                {
                    "driver_id": person.user_id,
                    "full_name": user.full_name if user else "",
                    "phone": user.phone if user else "",
                    "is_available": bool(person.is_available),
                    "is_working_now": bool(person.is_working_now),
                    "lat": person.current_location_lat,
                    "lng": person.current_location_lng,
                    "last_location_update": (
                        person.last_location_update.isoformat() if person.last_location_update else None
                    ),
                    # Derived from the `person` row already loaded above — NOT
                    # RouteOptimizationService.location_status(driver_id), which
                    # re-queries DeliveryPerson by id. That extra query, run once
                    # per driver, is a real N+1 on a snapshot the admin UI polls
                    # every 30s. location_status_for_person shares the exact same
                    # rule (it's what `_location_status` itself delegates to now)
                    # so this can't silently drift from what the driver's own bot
                    # reports.
                    "location_status": RouteOptimizationService.location_status_for_person(person),
                    "active_count": person.current_active_deliveries or 0,
                }
            )
        return drivers

    @staticmethod
    def _route_window_start_utc() -> datetime:
        """Boundary for "today's route" — delegated, never re-derived.

        The load-bearing invariant is that READER and WRITER agree on which row
        is "today's": this method is the dispatch map's read side, and
        RouteOptimizationService.current_route()/_upsert_route() are the write
        side an admin edits routes through. If the two disagree, an admin can
        edit a route the map isn't displaying, or see one the editor can no
        longer find.

        Both used to hard-code UTC midnight, and this docstring used to warn
        against "fixing" it to the Tashkent boundary — because at the time that
        would have introduced the very disagreement described above. That
        reasoning was right about the invariant and wrong about the value: UTC
        midnight is 05:00 in Tashkent, so BOTH sides were wrong together for
        five hours every morning (see _driver_day_start_utc for what that broke
        for drivers). The boundary now resolves to local midnight on both
        sides, which also lines the route layer up with `_day_bounds` — the
        order layer's Tashkent-local calendar day — instead of splitting from it.

        Delegating rather than copying is the point: the two can no longer
        drift apart. Do not re-derive the boundary here.
        """
        return _driver_day_start_utc()

    @classmethod
    def _routes(cls) -> List[Dict[str, Any]]:
        route_window_start = cls._route_window_start_utc()
        route_rows = (
            DeliveryRoute.query.options(joinedload(DeliveryRoute.overridden_by_user))
            .filter(DeliveryRoute.route_date >= route_window_start)
            .order_by(DeliveryRoute.delivery_person_id, DeliveryRoute.created_at.desc())
            .all()
        )
        # One route per driver: the newest row for the day wins, matching
        # RouteOptimizationService.current_route.
        newest: Dict[int, DeliveryRoute] = {}
        for row in route_rows:
            newest.setdefault(row.delivery_person_id, row)

        all_ids = [did for row in newest.values() for did in (row.optimized_order or [])]
        deliveries = (
            Delivery.query.options(
                joinedload(Delivery.order).joinedload(Order.user),
                joinedload(Delivery.order).joinedload(Order.delivery_address),
                # Route stops render the same item lines as the pool rows, so
                # they need the same eager load — see the sibling options in
                # `get_snapshot`.
                #
                # Chained off `joinedload(Delivery.order)` deliberately: the
                # two options above already declare that path as a joinedload,
                # and declaring it a second time with a different strategy
                # raises "Loader strategies ... conflict" at query-compile
                # time. Only the collection hop switches to `selectinload`.
                joinedload(Delivery.order).selectinload(Order.order_items).joinedload(OrderItem.product),
            )
            .filter(Delivery.id.in_(all_ids))
            .all()
            if all_ids
            else []
        )
        by_id = {d.id: d for d in deliveries}

        routes = []
        for driver_id, row in newest.items():
            pinned = {str(k): int(v) for k, v in (row.pinned_stops or {}).items()}
            stops = []
            for position, delivery_id in enumerate(row.optimized_order or []):
                delivery = by_id.get(delivery_id)
                if delivery is None:
                    continue
                order = delivery.order
                address = order.delivery_address if order else None
                stops.append(
                    {
                        "delivery_id": delivery.id,
                        "order_id": delivery.order_id,
                        "order_number": order.order_number if order else None,
                        "position": position,
                        "pinned": str(delivery.id) in pinned,
                        "lat": float(address.latitude) if address and address.latitude is not None else None,
                        "lng": float(address.longitude) if address and address.longitude is not None else None,
                        "address_label": cls._address_label(address),
                        "customer_name": order.user.full_name if order and order.user else "",
                        "delivery_status": cls._value(delivery.status),
                        **cls._items_fields(order),
                    }
                )
            routes.append(
                {
                    "route_id": row.id,
                    "driver_id": driver_id,
                    "manual_override": bool(row.manual_override),
                    "overridden_by_name": (row.overridden_by_user.full_name if row.overridden_by_user else None),
                    "overridden_at": row.overridden_at.isoformat() if row.overridden_at else None,
                    "total_distance_km": row.total_distance_km,
                    "estimated_duration_minutes": row.estimated_duration_minutes,
                    # `RouteEditService` sets `extra_data["metrics_stale"]`
                    # whenever a stop moves on/off this route or the sequence
                    # is hand-edited without a fresh matrix figure (see
                    # route_edit_service.py `_refresh_metrics` /
                    # `_mark_metrics_stale`) — the distance/duration above can
                    # describe a route that no longer matches `stops`. This
                    # must reach the admin panel so it can qualify the number
                    # instead of showing it as confidently current.
                    "metrics_stale": bool((row.extra_data or {}).get("metrics_stale", False)),
                    "start_lat": row.start_location_lat,
                    "start_lng": row.start_location_lng,
                    "stops": stops,
                }
            )
        return routes
