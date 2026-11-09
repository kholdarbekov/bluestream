"""Builders for the capacity-aware ETA plan's suites (honest-ETA Phase 2 plan, Task 2 onwards).

THE builders module of the plan (Global Constraints, Tests): drivers with rosters, bottle sessions, orders at a known
distance from the depot, status-history taps, and `rebase_clock`. Later tasks import and extend it; they never copy a
builder into a test file or start a second module.

A builder writes the rows the real writers leave, at the instant the test names:

* a driver is a `users` row and its `delivery_persons` row; every driver id is `users.id`;
* a session opened with a load anchors its driver at the depot, as `open_bottle_session` does, and so does a close;
  a session opened by a hand-off does not (`_open_session_for_transfer`);
* a tap is the `delivery_status_history` row of one status change, with the stop's door on an arrival or a delivery
  (`_capture_arrival_position`). The delivery and its order move as the staff status route moves them
  (`DELIVERY_TO_ORDER_STATUS_SYNC`). A tap leaves the driver's live position alone: `locate` sets it;
* every instant is WRITTEN in UTC, as the real writers write it. SQLite drops the offset and hands the column back
  naive, which every reader takes as UTC (`ensure_utc`); Postgres stores either form alike;
* a customer's day and window are the order's own columns (`schedule`), and a dated order is held for its day with no
  delivery row, or with a RESCHEDULED one (`hold_for_release`); a stored route is the optimiser's `optimized_order`
  (`store_route`).

`rebase_clock` serves the tests that drive real routes, whose rows carry the real clock (Global Constraints, Tests).

The recompute's own seams (Task 6): `eta_redis` fakes the Redis its queued marker and run lock live in,
`recompute_requests` records each run published through `_publish_recompute`, and `published` records each Celery
publish, for a test that drives the real seam down to `Task.apply_async` (`real_eta_publish`). `shown_leg` builds a
finished trip that teaches Phase 1's headroom, and `shown_promise` the shown Google promise of a tap, as
`record_on_the_way` writes them, and `shadow_promise` its hidden shadow row. `returning_customer` and `orders_water`
place a returning customer's cash order through the route the clients call, and `add_fit` writes a fit as the weekly
job writes it. `engine_promise` writes one of the engine's own rows as the recorder writes it, and `REPORT_*` and
`report_day_at` are the accuracy report's scenario (Task 9).
"""

import inspect
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from decimal import Decimal
from itertools import count
from typing import Callable, Dict, List, Optional, Tuple
from unittest import mock

import pytest
from celery.app.task import Task
from geopy.distance import geodesic
from sqlalchemy import or_, select

from business_app.models.bottle import (
    DriverBottleSession,
    DriverBottleSessionOrder,
    DriverBottleTransfer,
    DriverSessionMembership,
)
from business_app.models.delivery import Delivery, DeliveryPerson, DeliveryRoute, DeliveryStatusHistory
from business_app.models.delivery_promise import MODEL_ENGINE_S1, MODEL_GOOGLE_TAP, MODEL_OSRM_SHADOW, DeliveryPromise
from business_app.models.eta_model_fit import EtaModelFit
from business_app.models.order import Order, OrderItem
from business_app.models.product import Product, ProductCategory, ProductSizeEnum
from business_app.models.user import User, UserAddress
from business_app.services.delivery_eta_service import SOURCE_GOOGLE_TRAFFIC, SOURCE_STALE_ESTIMATE
from business_app.services.eta_plan import windows
from business_app.services.eta_plan.recorder import _LOCAL_NOON_MINUTES
from business_app.utils.delivery_window import window_edge_utc
from business_app.utils.helpers import get_warehouse_coordinates
from business_app.utils.local_windows import local_date, local_instant_at_minutes
from business_app.utils.timezone_utils import ensure_utc
from shared import business_config
from shared.constants import ETA_STAGE_ON_THE_WAY, ETA_STAGE_WAITING
from shared.enums import (
    DeliveryStatus,
    DriverBottleSessionStatus,
    DriverBottleTransferStatus,
    DriverSessionMembershipStatus,
    OrderStatus,
    UserRole,
    UserStatus,
    UserType,
)
from shared.staff_constants import DELIVERY_TO_ORDER_STATUS_SYNC
from tests.integration import reorder_reminder_builders
from tests.integration.contract_pricing_factory import customer_headers
from tests.integration.tier_discount_factory import post_order, verify_phone

Point = Tuple[float, float]

_SEQ = count(1)
_AUTO = object()
_WATER_SKU = "ETA-PLAN-WATER-19L"
# The statuses whose history row carries the stop's door (`_capture_arrival_position`).
_AT_THE_DOOR = (DeliveryStatus.ARRIVED, DeliveryStatus.DELIVERED)


def point_at_km(km: float) -> Point:
    """The point `km` straight-line (geodesic, as `calculate_distance` measures) km due north of the depot. North of
    the default depot stays inside the delivery zone the address model enforces for about 14 km."""
    destination = geodesic(kilometers=km).destination(get_warehouse_coordinates(), 0)
    return destination.latitude, destination.longitude


def make_driver(
    db,
    *,
    roster: Tuple[Optional[str], Optional[str]] = ("09:00", "18:00"),
    active: bool = True,
    available: bool = True,
    status: UserStatus = UserStatus.ACTIVE,
    working_days=None,
) -> User:
    """A delivery driver: the `users` row and its `delivery_persons` row, rostered `roster` ("HH:MM" local, or
    (None, None) for no roster). `active` is the profile's `is_active`, `status` the account's."""
    n = next(_SEQ)
    phone = f"+99894{n:07d}"
    user = User(
        first_name="Driver",
        last_name=f"No{n}",
        phone=phone,
        password_hash="not-a-real-hash",
        role=UserRole.DELIVERY_DRIVER,
        user_type=UserType.STAFF,
        status=status,
    )
    db.session.add(user)
    db.session.flush()
    person = DeliveryPerson(
        user_id=user.id,
        full_name=f"Driver No{n}",
        phone=phone,
        is_active=active,
        is_available=available,
        working_hours_start=roster[0],
        working_hours_end=roster[1],
    )
    if working_days is not None:
        person.working_days = list(working_days)
    db.session.add(person)
    db.session.flush()
    # The ORM leaves a None out of an INSERT when the column has a default ("09:00" / "18:00"), so a missing roster
    # is written by an UPDATE, as a legacy row holds it.
    person.working_hours_start, person.working_hours_end = roster
    db.session.commit()
    return user


def person_of(driver: User) -> DeliveryPerson:
    return DeliveryPerson.query.filter_by(user_id=driver.id).one()


def locate(db, driver: User, point: Point, at: datetime) -> None:
    """A live position: the fix the driver's bot shares, stamped `at`."""
    person = person_of(driver)
    person.current_location_lat, person.current_location_lng = point
    person.location_accuracy_m = None
    person.last_location_update = ensure_utc(at)
    db.session.commit()


def _anchor_at_depot(driver_id: int, at: datetime) -> None:
    """The fix `StaffService.anchor_driver_at_warehouse` writes when the driver loads or returns bottles."""
    person = DeliveryPerson.query.filter_by(user_id=driver_id).one()
    person.current_location_lat, person.current_location_lng = get_warehouse_coordinates()
    person.location_accuracy_m = None
    person.last_location_update = ensure_utc(at)


def open_session(
    db,
    driver: User,
    started_at: datetime,
    *,
    loaded: int = 30,
    delivered: int = 0,
    collected: int = 0,
) -> DriverBottleSession:
    """A bottle session the driver opened with a load of `loaded` at the depot at `started_at`, with the tallies its
    deliveries left so far."""
    session = DriverBottleSession(
        driver_user_id=driver.id,
        bottles_loaded=loaded,
        bottles_delivered=delivered,
        bottles_collected_from_customers=collected,
        status=DriverBottleSessionStatus.OPEN,
        loaded_by_user_id=driver.id,
        started_at=ensure_utc(started_at),
    )
    db.session.add(session)
    _anchor_at_depot(driver.id, started_at)
    db.session.commit()
    return session


def close_session(db, session: DriverBottleSession, at: datetime, *, returned: int = 0) -> DriverBottleSession:
    """The driver closes the session at the depot at `at`."""
    session.status = DriverBottleSessionStatus.CLOSED
    session.closed_at = ensure_utc(at)
    session.closed_by_user_id = session.driver_user_id
    session.bottles_returned_to_warehouse = returned
    session.compute_discrepancy()
    _anchor_at_depot(session.driver_user_id, at)
    db.session.commit()
    return session


def reopen_session(db, session: DriverBottleSession, at: datetime) -> DriverBottleSession:
    """An admin reopens a closed session at `at` to correct a count (`BottleTrackingService.reopen_session`)."""
    session.status = DriverBottleSessionStatus.OPEN
    session.reopened_at = ensure_utc(at)
    session.reopened_reason = "count correction"
    session.reopen_count = (session.reopen_count or 0) + 1
    session.bottles_returned_to_warehouse = None
    session.closed_at = None
    session.closed_by_user_id = None
    session.discrepancy = None
    db.session.commit()
    return session


def join_session(db, member: User, session: DriverBottleSession, at: datetime) -> DriverSessionMembership:
    """`member` rides along in the session's car from `at`: an ACTIVE co-driver membership."""
    membership = DriverSessionMembership(
        session_id=session.id,
        session_owner_id=session.driver_user_id,
        member_driver_id=member.id,
        status=DriverSessionMembershipStatus.ACTIVE,
        joined_at=ensure_utc(at),
    )
    db.session.add(membership)
    db.session.commit()
    return membership


def hand_over(
    db, sender_session: DriverBottleSession, receiver: User, at: datetime, *, quantity: int
) -> DriverBottleSession:
    """A colleague hands `quantity` full bottles to `receiver`, who had no session: sent and confirmed at `at`. The
    confirm opens the receiver a session with nothing loaded (`_open_session_for_transfer`), which is returned."""
    sender_session.bottles_transferred_out = (sender_session.bottles_transferred_out or 0) + quantity
    received = DriverBottleSession(
        driver_user_id=receiver.id,
        bottles_loaded=0,
        bottles_transferred_in=quantity,
        status=DriverBottleSessionStatus.OPEN,
        loaded_by_user_id=receiver.id,
        started_at=ensure_utc(at),
        notes="Opened by a transfer",
    )
    db.session.add(received)
    db.session.flush()
    db.session.add(
        DriverBottleTransfer(
            sender_session_id=sender_session.id,
            sender_driver_id=sender_session.driver_user_id,
            receiver_driver_id=receiver.id,
            receiver_session_id=received.id,
            declared_quantity=quantity,
            confirmed_quantity=quantity,
            status=DriverBottleTransferStatus.CONFIRMED,
            sent_at=ensure_utc(at),
            confirmed_at=ensure_utc(at),
        )
    )
    db.session.commit()
    return received


def _water(db) -> Product:
    """The returnable 19 L bottle every builder order carries, one per test database."""
    product = Product.query.filter_by(sku=_WATER_SKU).first()
    if product is None:
        category = ProductCategory(name="Water")
        db.session.add(category)
        db.session.flush()
        product = Product(
            name="Water 19L",
            sku=_WATER_SKU,
            category_id=category.id,
            size=ProductSizeEnum.SIZE_19L,
            base_price=Decimal("15000.00"),
            tracks_returnable_bottles=True,
            returnable_bottles_per_unit=Decimal("1.00"),
        )
        db.session.add(product)
        db.session.flush()
    return product


def order_at_km(
    db,
    km: float,
    *,
    created_at: datetime,
    driver: Optional[User] = None,
    bottles: int = 1,
    status: OrderStatus = OrderStatus.CONFIRMED,
) -> Order:
    """An order of `bottles` returnable 19 L bottles to a door `km` km due north of the depot (`point_at_km`), placed at
    `created_at`, with its delivery: in the pool, or ASSIGNED to `driver`. No history row is written: a test taps the
    accept it needs."""
    n = next(_SEQ)
    customer = User(
        first_name="Customer",
        last_name=f"No{n}",
        phone=f"+99895{n:07d}",
        password_hash="not-a-real-hash",
        role=UserRole.CUSTOMER,
        status=UserStatus.ACTIVE,
    )
    db.session.add(customer)
    db.session.flush()
    latitude, longitude = point_at_km(km)
    address = UserAddress(
        user_id=customer.id,
        title="home",
        full_address=f"{n} North St, Tashkent",
        street_address=f"{n} North St",
        city="Tashkent",
        latitude=latitude,
        longitude=longitude,
    )
    db.session.add(address)
    db.session.flush()
    price = Decimal("15000.00")
    order = Order(
        order_number=f"ETA-{n:06d}",
        user_id=customer.id,
        status=status,
        subtotal=price * bottles,
        total_amount=price * bottles,
        delivery_address_id=address.id,
        order_source="telegram",
        created_at=ensure_utc(created_at),
    )
    db.session.add(order)
    db.session.flush()
    db.session.add(
        OrderItem(
            order_id=order.id,
            product_id=_water(db).id,
            quantity=bottles,
            unit_price=price,
            total_price=price * bottles,
        )
    )
    db.session.add(
        Delivery(
            order_id=order.id,
            delivery_person_id=driver.id if driver is not None else None,
            status=DeliveryStatus.ASSIGNED if driver is not None else DeliveryStatus.SCHEDULED,
            scheduled_date=ensure_utc(created_at),
            scheduled_time_slot="anytime",
            created_at=ensure_utc(created_at),
            updated_at=ensure_utc(created_at),
        )
    )
    db.session.commit()
    return order


def bind(db, session: DriverBottleSession, order: Order) -> DriverBottleSessionOrder:
    """The order rides on the session (`bind_order_to_session`), accepted by its delivery's driver."""
    binding = DriverBottleSessionOrder(
        session_id=session.id,
        order_id=order.id,
        accepted_by_driver_id=order.delivery.delivery_person_id,
    )
    db.session.add(binding)
    db.session.commit()
    return binding


def schedule(
    db, order: Order, *, day: Optional[date] = None, window: Tuple[Optional[time], Optional[time]] = (None, None)
) -> Order:
    """The customer's choice on `order`: the delivery date `day` and the window as local (start, end) times, either side
    open (`delivery_date`, `delivery_window_start`/`end`). The delivery row keeps the label its writer left."""
    order.delivery_date = day
    order.delivery_window_start, order.delivery_window_end = window
    db.session.commit()
    return order


def hold_for_release(db, order: Order, *, parked: bool = False) -> Order:
    """`order` becomes a dated order held for its day: with no delivery row, the shape a future-dated order has until
    the release sweep (`ensure_delivery_if_due`) creates one; or, with `parked`, a RESCHEDULED row, the shape of an
    order released once and then moved to a later day. Give it a day with `schedule`."""
    delivery = order.delivery
    if parked:
        delivery.delivery_person_id = None
        delivery.status = DeliveryStatus.RESCHEDULED
    else:
        db.session.delete(delivery)
    db.session.commit()
    return order


def dated_order(
    db,
    day: date,
    *,
    created_at: datetime,
    parked: bool = False,
    window: Tuple[Optional[time], Optional[time]] = (None, None),
    km: float = 2.0,
) -> Order:
    """A confirmed order the customer dated `day`, with `window`, held for its release (`hold_for_release`): with no
    delivery row, or with `parked`, a RESCHEDULED one."""
    order = order_at_km(db, km, created_at=created_at)
    schedule(db, order, day=day, window=window)
    return hold_for_release(db, order, parked=parked)


def recreate_delivery(db, order: Order) -> Delivery:
    """`order`'s delivery row, written again: the same row with a new id, higher than that of every row made so far.
    Nothing else about it changes."""
    old = order.delivery
    kept = {
        name: getattr(old, name)
        for name in (
            "order_id",
            "delivery_person_id",
            "status",
            "scheduled_date",
            "scheduled_time_slot",
            "created_at",
            "updated_at",
        )
    }
    db.session.delete(old)
    db.session.flush()
    row = Delivery(**kept)
    db.session.add(row)
    db.session.commit()
    return row


def store_route(db, driver: User, delivery_ids, at: datetime) -> DeliveryRoute:
    """The route the optimiser stored for `driver` at `at`: delivery ids in sequence, from the depot (`optimized_order`
    holds delivery ids, whatever its column comment says)."""
    depot_lat, depot_lng = get_warehouse_coordinates()
    route = DeliveryRoute(
        name="eta-plan",
        delivery_person_id=driver.id,
        start_location_lat=depot_lat,
        start_location_lng=depot_lng,
        route_date=ensure_utc(at),
        optimized_order=list(delivery_ids),
    )
    db.session.add(route)
    db.session.commit()
    return route


def tap(
    db, delivery: Delivery, status: DeliveryStatus, at: datetime, *, by=_AUTO, point=_AUTO
) -> DeliveryStatusHistory:
    """`delivery` moves to `status` at `at`, tapped by `by` (a User; None for no actor; by default the delivery's
    driver). An accept out of the pool gives the stop to `by`. An arrival or a delivery carries the door unless `point`
    says otherwise."""
    actor = delivery.delivery_person_id if by is _AUTO else (by.id if by is not None else None)
    if status is DeliveryStatus.ASSIGNED and delivery.delivery_person_id is None:
        delivery.delivery_person_id = actor
    if point is _AUTO:
        address = delivery.order.delivery_address
        point = (address.latitude, address.longitude) if status in _AT_THE_DOOR and address is not None else None
    row = DeliveryStatusHistory(
        delivery_id=delivery.id,
        old_status=delivery.status,
        new_status=status,
        changed_by=actor,
        changed_at=ensure_utc(at),
        location_lat=point[0] if point is not None else None,
        location_lng=point[1] if point is not None else None,
    )
    db.session.add(row)
    delivery.status = status
    if status is DeliveryStatus.DELIVERED:
        delivery.delivered_at = delivery.actual_delivery_time = ensure_utc(at)
    synced = DELIVERY_TO_ORDER_STATUS_SYNC.get(status.value)
    if synced is not None:
        delivery.order.status = OrderStatus(synced)
    db.session.commit()
    return row


def reassign(db, delivery: Delivery, driver: User, at: datetime, *, by: User) -> DeliveryStatusHistory:
    """An admin hands the held stop to `driver` at `at`: the stop keeps its status, so its history row moves from a
    status into the same one (`DeliveryAssignmentService.assign_driver`)."""
    row = DeliveryStatusHistory(
        delivery_id=delivery.id,
        old_status=delivery.status,
        new_status=delivery.status,
        changed_by=by.id,
        changed_at=ensure_utc(at),
    )
    db.session.add(row)
    delivery.delivery_person_id = driver.id
    db.session.commit()
    return row


def worked_day(db, driver: User, day: date, *, first_pickup: float, last_drop: float) -> Order:
    """One of `driver`'s past plan days as R3 reads it: a stop the driver picked up `first_pickup` and delivered
    `last_drop` minutes past `day`'s local midnight (`local_instant_at_minutes`; past 1440 is after that midnight)."""
    picked_up = local_instant_at_minutes(day, first_pickup)
    order = order_at_km(db, 2.0, created_at=picked_up - timedelta(hours=1), driver=driver)
    tap(db, order.delivery, DeliveryStatus.PICKED_UP, picked_up)
    tap(db, order.delivery, DeliveryStatus.DELIVERED, local_instant_at_minutes(day, last_drop))
    return order


def returning_customer(db, customer: User, address: UserAddress) -> None:
    """`customer` has had water delivered before, so a cash order of theirs is confirmed at once (instant COD
    confirmation), and has verified their phone, as ordering requires."""
    db.session.add(
        Order(
            order_number="ETA-PAST-0001",
            user_id=customer.id,
            status=OrderStatus.DELIVERED,
            subtotal=Decimal("15000"),
            delivery_fee=Decimal("0"),
            total_amount=Decimal("15000"),
            delivery_address_id=address.id,
            order_source="telegram",
        )
    )
    db.session.commit()
    verify_phone(db, customer)


def orders_water(app, db, customer: User, address: UserAddress, product: Product) -> Order:
    """The customer's cash order of two bottles (enough for the minimum order amount), placed through
    `POST /api/v1/orders/` with the body the bot and the website send: confirmed at once (`returning_customer`), its
    delivery in the drivers' pool."""
    response = post_order(
        app, customer_headers(app, customer), product_id=product.id, address_id=address.id, payment_method="cash"
    )
    assert response.status_code == 201, response.get_data(as_text=True)
    db.session.expire_all()
    order = db.session.get(Order, response.get_json()["data"]["order"]["id"])
    assert (order.status, order.delivery.status) == (OrderStatus.CONFIRMED, DeliveryStatus.SCHEDULED)
    return order


# Every timestamp column the engine reads. A route stamps them on the real clock, and pinning `get_utc_now` does not
# move the column defaults that stamp them (`models/base.py`, `models/delivery.py`, `models/bottle.py`).
CLOCK_COLUMNS = (
    (Order, ("created_at",)),
    (Delivery, ("created_at", "updated_at")),
    (DeliveryStatusHistory, ("changed_at",)),
    (DriverBottleSession, ("started_at", "closed_at", "reopened_at")),
    (DriverBottleTransfer, ("sent_at", "confirmed_at", "resolved_at")),
    (DeliveryPerson, ("last_location_update",)),
    (DeliveryRoute, ("route_date", "created_at")),
)


def eleven_yesterday() -> datetime:
    """11:00 local on the day before the real run, in UTC: the `to` of a route-driven test. It is always in the past
    and always daytime, whenever the suite runs."""
    return window_edge_utc(local_date() - timedelta(days=1), time(11, 0))


@pytest.fixture(name="rebase_clock")
def rebase_clock_fixture(db) -> Callable[[datetime], None]:
    """`rebase_clock(to)`: move what the real clock stamped since the previous call (or since this fixture was set
    up) so that the real present maps to `to`; the engine then runs with `now = to`. A later phase of the same test
    drives its routes and calls `rebase_clock(to + 1 min)`.

    Each timestamp of `CLOCK_COLUMNS` at or after the watermark moves by `to - present`, and the watermark becomes that
    present. A row already rebased sits before it, so it never moves again; nor does a builder's row, written on the
    test's clock. Ask for this fixture before any fixture that stamps the real clock.
    """
    watermark = [datetime.now(timezone.utc)]

    def rebase_clock(to: datetime) -> None:
        present = datetime.now(timezone.utc)
        shift = ensure_utc(to) - present
        for model, names in CLOCK_COLUMNS:
            table = model.__table__
            columns = [table.c[name] for name in names]
            stamped = db.session.execute(
                select(table.c.id, *columns).where(or_(*(column >= watermark[0] for column in columns)))
            ).all()
            for row in stamped:
                values = {
                    name: ensure_utc(row._mapping[name]) + shift
                    for name in names
                    if row._mapping[name] is not None and ensure_utc(row._mapping[name]) >= watermark[0]
                }
                # An UPDATE that leaves `updated_at` out of its SET restamps it (TimestampMixin's `onupdate`).
                values.setdefault("updated_at", table.c.updated_at)
                db.session.execute(table.update().where(table.c.id == row.id).values(values))
        db.session.commit()
        watermark[0] = present

    return rebase_clock


# ---------------------------------------------------------------------------------------------------------------------
# The recompute's seams (Task 6): its Redis, its one publish seam and its kill switch.
# ---------------------------------------------------------------------------------------------------------------------


class FakeRedis(reorder_reminder_builders.FakeRedis):
    """What the recompute's queued marker and run lock call: SET NX EX, GET, DELETE and TTL, with Redis' semantics and
    nothing more. Values come back as bytes, as they do from Redis. Time does not pass: `expiries` holds the seconds
    each key has left, and a test that needs less left sets it."""

    def ttl(self, key):
        if key not in self.store:
            return -2
        remaining = self.expiries.get(key)
        return -1 if remaining is None else remaining


@pytest.fixture(name="eta_redis")
def eta_redis_fixture(monkeypatch) -> FakeRedis:
    """The Redis of the recompute's queued marker and run lock, faked in both modules that reach it (the engine and
    the request side): the suite's lanes share the real Redis DBs."""
    from business_app.services.eta_plan import engine
    from business_app.utils import eta_recompute_dispatch

    fake = FakeRedis()
    monkeypatch.setattr(engine, "redis_client", fake)
    monkeypatch.setattr(eta_recompute_dispatch, "redis_client", fake)
    return fake


@pytest.fixture(name="recompute_requests")
def recompute_requests_fixture(monkeypatch) -> List[int]:
    """The countdown of each recompute published through the one seam, `_publish_recompute`, in order. Nothing reaches
    Celery."""
    from business_app.utils import eta_recompute_dispatch

    published: List[int] = []

    def publish(countdown: int) -> None:  # `_publish_recompute`'s own signature
        published.append(countdown)

    monkeypatch.setattr(eta_recompute_dispatch, "_publish_recompute", publish)
    return published


@pytest.fixture(name="published")
def published_fixture(monkeypatch) -> List[Tuple[str, Dict]]:
    """Every Celery publish, as `(task name, options)`. Each is bound against the task's own `run` signature first, so
    a publish the worker could not run fails here. For a test that drives the real seam, `_publish_recompute`, down to
    `Task.apply_async` (it carries `real_eta_publish`)."""
    runs: List[Tuple[str, Dict]] = []

    def apply_async(task, args=None, kwargs=None, **options):
        inspect.signature(task.run).bind(*(args or ()), **(kwargs or {}))
        runs.append((task.name, options))

    monkeypatch.setattr(Task, "apply_async", apply_async)
    return runs


@pytest.fixture(name="engine_on")
def engine_on_fixture(app, monkeypatch) -> None:
    """The shadow engine's kill switch on for one test: the tests' config has it off."""
    monkeypatch.setitem(app.config, "ETA_SHADOW_ENGINE_ENABLED", True)


@pytest.fixture(scope="module", name="celery_app_module")
def celery_app_module_fixture(app):
    """`business_app.tasks.celery_app`, imported once with `make_celery()` handed the pytest app (the same guard as
    tests/unit/test_reorder_reminder_beat.py): the beat schedule, the task registry and the routes live on its
    `celery`."""
    module_name = "business_app.tasks.celery_app"
    if module_name not in sys.modules:
        with mock.patch("business_app.create_app", return_value=app):
            import business_app.tasks.celery_app  # noqa: F401
    return sys.modules[module_name]


# ---------------------------------------------------------------------------------------------------------------------
# Phase 1's shown promises (Task 6): what Track and the learned headroom read, which engine rows must never change.
# ---------------------------------------------------------------------------------------------------------------------


def shown_promise(
    db, tapped: DeliveryStatusHistory, *, google_minutes: float = 18.0, window_end: Optional[datetime] = None
) -> DeliveryPromise:
    """The SHOWN `google_tap_v1` promise `record_on_the_way` writes for the in_transit tap `tapped`, from Google's
    `google_minutes` of live traffic, recorded at the tap. `window_end` is the "by" time the customer is told."""
    tapped_at = ensure_utc(tapped.changed_at)
    promise = DeliveryPromise(
        delivery_id=tapped.delivery_id,
        delivery_status_history_id=tapped.id,
        computed_at=tapped_at,
        stage=ETA_STAGE_ON_THE_WAY,
        expected_at=tapped_at + timedelta(minutes=google_minutes),
        window_end=window_end,
        model_version=MODEL_GOOGLE_TAP,
        source=SOURCE_GOOGLE_TRAFFIC,
        shown=True,
    )
    db.session.add(promise)
    db.session.commit()
    return promise


def stale_estimate_promise(db, tapped: DeliveryStatusHistory, *, window_end: datetime) -> DeliveryPromise:
    """The SHOWN `google_tap_v1` promise `record_on_the_way` writes for the in_transit tap `tapped` when the driver's
    position was too old for Google (`SOURCE_STALE_ESTIMATE`): our engine's minutes plus an allowance, by `window_end`,
    and no `expected_at`, which is Google's time alone. Recorded at the tap."""
    promise = DeliveryPromise(
        delivery_id=tapped.delivery_id,
        delivery_status_history_id=tapped.id,
        computed_at=ensure_utc(tapped.changed_at),
        stage=ETA_STAGE_ON_THE_WAY,
        window_end=window_end,
        model_version=MODEL_GOOGLE_TAP,
        source=SOURCE_STALE_ESTIMATE,
        shown=True,
    )
    db.session.add(promise)
    db.session.commit()
    return promise


def shown_leg(
    db, driver: User, tapped_at: datetime, *, google_minutes: float = 18.0, drive_minutes: float = 23.0
) -> DeliveryStatusHistory:
    """One of `driver`'s finished trips that teaches Phase 1's headroom, built as `TestHeadroom`'s legs are
    (tests/unit/test_delivery_eta_service.py): the driver's real in_transit tap at `tapped_at`, its shown promise of
    Google's `google_minutes`, and the driver's real arrival `drive_minutes` later. With the defaults the leg's ratio is
    (23 - 5 off-road) / 18 = 1.0. The stop ends delivered, so no plan holds it, and it has no picked-up or delivered
    move, so no typical day (R3) reads it. Returns the tap."""
    tapped_at = ensure_utc(tapped_at)
    order = order_at_km(db, 2.0, created_at=tapped_at - timedelta(hours=1), driver=driver, status=OrderStatus.DELIVERED)
    delivery = order.delivery
    delivery.status = DeliveryStatus.DELIVERED
    tapped = DeliveryStatusHistory(
        delivery_id=delivery.id,
        old_status=DeliveryStatus.PICKED_UP,
        new_status=DeliveryStatus.IN_TRANSIT,
        changed_by=driver.id,
        changed_at=tapped_at,
    )
    arrived = DeliveryStatusHistory(
        delivery_id=delivery.id,
        old_status=DeliveryStatus.IN_TRANSIT,
        new_status=DeliveryStatus.ARRIVED,
        changed_by=driver.id,
        changed_at=tapped_at + timedelta(minutes=drive_minutes),
    )
    db.session.add_all([tapped, arrived])
    db.session.commit()
    shown_promise(db, tapped, google_minutes=google_minutes)
    return tapped


def shadow_promise(db, tapped: DeliveryStatusHistory, *, minutes: float) -> DeliveryPromise:
    """The HIDDEN `osrm_shadow_v1` row `record_on_the_way` writes beside the shown one: our own engine's `minutes` for
    the leg of the in_transit tap `tapped`, recorded at the tap."""
    tapped_at = ensure_utc(tapped.changed_at)
    promise = DeliveryPromise(
        delivery_id=tapped.delivery_id,
        delivery_status_history_id=tapped.id,
        computed_at=tapped_at,
        stage=ETA_STAGE_ON_THE_WAY,
        expected_at=tapped_at + timedelta(minutes=minutes),
        model_version=MODEL_OSRM_SHADOW,
        source="osrm_selfhosted",
        shown=False,
    )
    db.session.add(promise)
    db.session.commit()
    return promise


def shown_legs(db, driver: User, count_of_legs: int, *, before: datetime) -> List[DeliveryStatusHistory]:
    """`count_of_legs` of `driver`'s finished trips (`shown_leg`), tapped an hour apart, the last an hour before
    `before`. Each drive ends before the driver's next tap, so no leg interrupts another (spec §4.8 S2)."""
    return [shown_leg(db, driver, ensure_utc(before) - timedelta(hours=n + 1)) for n in range(count_of_legs)]


# ---------------------------------------------------------------------------------------------------------------------
# The weekly job's fits (Task 4): what `current_params(now)` reads, written as the weekly job writes them.
# ---------------------------------------------------------------------------------------------------------------------


def stored_params(
    *,
    points=business_config.ETA_CYCLE_SEED_POINTS,
    line=business_config.ETA_DEPOT_LEG_SEED,
    buckets=None,
    monitoring=None,
) -> dict:
    """The `params` JSON of a fit, in the shape the weekly job writes (the plan's Ruling P4)."""
    return {
        "cycle_points": [list(point) for point in points],
        "depot_line": list(line),
        "window_buckets": buckets if buckets is not None else {},
        "monitoring": monitoring if monitoring is not None else {"by_hour_band": {}, "by_bottles_band": {}},
    }


def written_on(monday: date) -> datetime:
    """When the weekly job writes a fit on `monday`: 05:30 in Tashkent."""
    return window_edge_utc(monday, time(5, 30))


def add_fit(db, monday: date, *, fitted_at=None, **params) -> EtaModelFit:
    """A fit of the 28 plan days before `monday`, written on `monday` at 05:30 unless `fitted_at` says otherwise."""
    data_to = window_edge_utc(monday, time(5, 0))
    fit = EtaModelFit(
        fitted_at=fitted_at or written_on(monday),
        data_from=data_to - timedelta(days=28),
        data_to=data_to,
        params=stored_params(**params),
        rows_used={"cycle": 412, "depot": 31, "window": 640},
        kept_previous={},
    )
    db.session.add(fit)
    db.session.commit()
    return fit


# ---------------------------------------------------------------------------------------------------------------------
# The accuracy report (Task 9): the engine's own rows as the recorder writes them, and the report's scenario.
# ---------------------------------------------------------------------------------------------------------------------

# The plan day the report's tests seed (Tuesday 3 November 2026), and the weekly fit the engine reads on it, with what
# its monitoring learned (spec §4.5).
REPORT_DAY = date(2026, 11, 3)
REPORT_FIT_MONDAY = date(2026, 11, 2)
REPORT_MONITORING = {
    "by_hour_band": {"11-13": {"ratio": 1.08, "n": 41}, "13-15": {"ratio": 1.21, "n": 37}},
    "by_bottles_band": {"0-5": {"ratio": 0.94, "n": 52}, "6-10": {"ratio": 1.32, "n": 19}},
}
# The admin page's fixture: tests/integration/test_admin_eta_accuracy.py writes it, the contract test holds the report
# to its keys, and the admin UI's Vitest renders the page from it (Task 10).
REPORT_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "admin_eta_accuracy" / "report.json"


def report_day_at(hour: int, minute: int = 0, *, day: date = REPORT_DAY) -> datetime:
    """`hour:minute` on the Tashkent wall clock on the local date `day` (`REPORT_DAY`), as aware UTC."""
    return window_edge_utc(day, time(hour, minute))


# The report route's one clock reading in those tests: 15:00 in Tashkent the day after `REPORT_DAY`.
REPORT_NOW = report_day_at(15, day=REPORT_DAY + timedelta(days=1))


def engine_promise(
    db,
    delivery: Delivery,
    computed_at: datetime,
    *,
    expected_at: Optional[datetime] = None,
    window: Optional[Tuple[datetime, datetime]] = None,
    customer_date: Optional[date] = None,
    stops_ahead: int = 0,
    started: bool = True,
    drivers_working: int = 1,
    source: str = "seed",
) -> DeliveryPromise:
    """An `engine_s1_v1` row of `delivery`, computed at `computed_at`, as `recorder.record` writes one.

    - With `expected_at` and `window` (start, end): a stop on a car. Its label and promised day are those of E
      (`windows.day_kind`, `windows.promised_date`).
    - With `customer_date` alone: a stop dated after today that no car takes yet, keeping its date with no time or
      window, labelled as noon on that date is.
    - With neither: unconfirmed, no day, no time, no window.

    A stop with no car has no stops ahead and has not started, as the planner leaves it.
    """
    computed_at = ensure_utc(computed_at)
    if expected_at is not None:
        label = windows.day_kind(expected_at, computed_at)
        promised = windows.promised_date(expected_at, customer_date)
    elif customer_date is not None:
        label = windows.day_kind(local_instant_at_minutes(customer_date, _LOCAL_NOON_MINUTES), computed_at)
        promised = windows.promised_date(None, customer_date)
    else:
        label, promised = "unconfirmed", None
    window_start, window_end = window if window is not None else (None, None)
    promise = DeliveryPromise(
        delivery_id=delivery.id,
        delivery_status_history_id=None,
        computed_at=computed_at,
        stage=ETA_STAGE_WAITING,
        expected_at=expected_at,
        window_start=window_start,
        window_end=window_end,
        day_kind=label,
        promised_date=promised,
        stops_ahead=stops_ahead if expected_at is not None else None,
        day_started=started if expected_at is not None else False,
        drivers_working=drivers_working,
        model_version=MODEL_ENGINE_S1,
        source=source,
        shown=False,
    )
    db.session.add(promise)
    db.session.commit()
    return promise
