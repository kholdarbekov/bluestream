"""REAL POSTGRES: two crew members tapping On the way at the same moment cannot promise the same full bottles (A8).

Spec 2026-11-09-bottle-aware-auto-assign-and-transfer-split §4.2 and §5: On the way is where bottles are reserved,
and the crew shares one truck. The owner and his co-driver each hold an 8-bottle stop, both picked up, against 10
full bottles. Each taps On the way on the staff bot's own route (`PUT /staff/delivery/<id>/status`, the body
`StaffAPIClient` sends). Exactly one tap may pass: the other must count the first one's stop and be refused with
2 free / 8 needed.

`with_for_update` compiles to nothing on SQLite, so this runs on a real, fully-migrated Postgres database
(`pg_app`, a copy of the worker's migrated template: the harness tests/integration/conftest.py gives
test_place_concurrency_pg_e2e.py and the other `_pg` files). The interleaving is forced, not hoped for, the way
test_customer_cancel_races_pg.py forces its own: the first tap is held right after its bottle check passed, before
it commits, and the second tap starts only then. The progress guard locks every crew member's DeliveryPerson row,
in user-id order, before it counts, so the second tap must wait on that lock and count the first stop once the
first commits. Without the lock it reads the uncommitted state, sees 10 free and passes too. Each tap runs on its
own thread, app context, session and connection, as two gunicorn workers would. Who taps first is parametrized, so
both lock orders a crew can produce are exercised. Every wait is bounded, so a broken lock fails instead of hanging.
Celery publishing is the only thing faked (`enqueued`).

The same file pins what a Delivered adds to the crew's trip (spec 2026-11-18 §6.5, call 19): the owner's close and a
co-driver's Delivered serialize on the trip row, so the Delivered lands before the seal or finds the trip closed and
adds nothing to it; and two Delivered taps add both their 10 L to the trip's one 10 L line.
"""

import threading
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from flask_jwt_extended import create_access_token
from sqlalchemy import event, text

from business_app.models.bottle import (
    DriverBottleSession,
    DriverBottleSessionOrder,
    DriverBottleSessionUnit,
    DriverSessionMembership,
)
from business_app.models.delivery import Delivery, DeliveryPerson, DeliveryStatusHistory
from business_app.models.order import Order, OrderItem
from business_app.models.product import Product, ProductCategory
from business_app.models.user import UserAddress
from business_app.serializers.bottle_serializers import serialize_bottle_session
from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.services.order_service import OrderService
from business_app.tasks.notification_tasks import send_delivery_update_task
from shared.enums import (
    DeliveryStatus,
    DriverBottleSessionStatus,
    DriverSessionMembershipStatus,
    OrderStatus,
    UserRole,
    UserType,
)
from staff_bot.api_client import StaffAPIClient
from tests.integration.test_customer_cancel_races_pg import _await_a_lock_waiter, _finish, _thread, _user
from tests.integration.test_redispatch_reschedules_to_today import enqueued  # noqa: F401 -- a fixture

pytestmark = [pytest.mark.integration, pytest.mark.e2e]

STATUS = "/api/v1/staff/delivery/{}/status"
CLOSE_SESSION = "/api/v1/staff/bottles/session/close"
ADMIN_ORDER_STATUS = "/api/v1/admin/orders/{}/status"
LOADED = 10
NEEDED = 8


def _crew_member(pg_db, phone, first_name):
    driver = _user(pg_db, phone, role=UserRole.DELIVERY_DRIVER, user_type=UserType.STAFF)
    pg_db.session.add(
        DeliveryPerson(
            user_id=driver.id, full_name=f"{first_name} Driver", phone=phone, is_active=True, is_available=True
        )
    )
    pg_db.session.commit()
    return driver


def _stop_on_the_trip(pg_db, customer, product, driver, session, number, status=DeliveryStatus.PICKED_UP):
    """An 8-bottle order its driver accepted onto the crew's trip, at ``status`` (picked up by default)."""
    address = UserAddress(
        user_id=customer.id,
        title=f"Stop {number}",
        full_address=f"{number} Crew Street, Tashkent",
        street_address=f"{number} Crew Street",
        city="Tashkent",
        latitude=41.3111,
        longitude=69.2797,
    )
    pg_db.session.add(address)
    pg_db.session.flush()
    order = Order(
        user_id=customer.id,
        order_number=f"ORD-CREW-{number}",
        status=OrderStatus.OUT_FOR_DELIVERY,
        subtotal=Decimal("120000.00"),
        delivery_fee=Decimal("0.00"),
        total_amount=Decimal("120000.00"),
        delivery_address_id=address.id,
    )
    pg_db.session.add(order)
    pg_db.session.flush()
    pg_db.session.add(
        OrderItem(
            order_id=order.id,
            product_id=product.id,
            quantity=NEEDED,
            unit_price=Decimal("15000.00"),
            total_price=Decimal("120000.00"),
        )
    )
    delivery = Delivery(
        order_id=order.id,
        status=status,
        delivery_person_id=driver.id,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot="09:00-12:00",
    )
    pg_db.session.add(delivery)
    pg_db.session.add(
        DriverBottleSessionOrder(session_id=session.id, order_id=order.id, accepted_by_driver_id=driver.id)
    )
    pg_db.session.commit()
    return delivery


def _crew_world(pg_db, *, co_driver_stop=DeliveryStatus.PICKED_UP):
    """The owner's trip, loaded with 10 full bottles; his co-driver an ACTIVE member; one stop each. The owner's is
    picked up; the co-driver's is at ``co_driver_stop``."""
    customer = _user(pg_db, "+998901242001", role=UserRole.CUSTOMER, user_type=UserType.INDIVIDUAL)
    owner = _crew_member(pg_db, "+998901242002", "Owner")
    co_driver = _crew_member(pg_db, "+998901242003", "Codriver")
    category = ProductCategory(name="Water", description="Water products", is_active=True)
    pg_db.session.add(category)
    pg_db.session.flush()
    product = Product(
        name="Pure Water 19L",
        category_id=category.id,
        size="19L",
        volume=19.0,
        volume_unit="L",
        base_price=Decimal("15000.00"),
        stock_quantity=100,
        is_active=True,
        tracks_returnable_bottles=True,
        returnable_bottles_per_unit=Decimal("1.00"),
    )
    session = DriverBottleSession(driver_user_id=owner.id, bottles_loaded=LOADED, status=DriverBottleSessionStatus.OPEN)
    pg_db.session.add_all([product, session])
    pg_db.session.flush()
    pg_db.session.add(
        DriverSessionMembership(
            session_id=session.id,
            session_owner_id=owner.id,
            member_driver_id=co_driver.id,
            status=DriverSessionMembershipStatus.ACTIVE,
        )
    )
    pg_db.session.commit()
    stops = {
        "owner": (owner, _stop_on_the_trip(pg_db, customer, product, owner, session, 1)),
        "co_driver": (co_driver, _stop_on_the_trip(pg_db, customer, product, co_driver, session, 2, co_driver_stop)),
    }
    return SimpleNamespace(session_id=session.id, stops=stops)


def _staff_headers(driver):
    """A staff token (staff routes read the DB roles, not a claim)."""
    return {
        "Authorization": f"Bearer {create_access_token(identity=str(driver.id))}",
        "Content-Type": "application/json",
    }


def _taps(driver, delivery_id, status):
    """The staff bot's status tap, with the body `StaffAPIClient` sends."""
    headers = _staff_headers(driver)
    return lambda client: client.put(
        STATUS.format(delivery_id), json=StaffAPIClient.delivery_status_body(status), headers=headers
    )


def _close_trip(owner, bottles, units_returned):
    """The staff bot's Close trip: the 18.9 L count the owner typed and one count per line on the trip
    (`units_returned`, {product_id: quantity})."""
    headers = _staff_headers(owner)
    body = {
        "bottles_returned_to_warehouse": bottles,
        "units_returned": [{"product_id": pid, "quantity": q} for pid, q in units_returned.items()],
    }
    return lambda client: client.post(CLOSE_SESSION, json=body, headers=headers)


def _taps_on_the_way(driver, delivery_id):
    return _taps(driver, delivery_id, "in_transit")


def _race_two_on_the_way_taps(pg_app, pg_db, monkeypatch, first, second):
    """Run two On the way taps against the real app so the second one meets the first one's open transaction.

    The first tap is held right after the bottle guard passed it: it holds whatever the guard locked, and its
    IN_TRANSIT is not committed yet, so only a lock can make the second tap count it. `first` and `second` are
    `_taps(...)` callables. Returns `{"first": (status, body), "second": (status, body)}`."""
    first_passed_its_check = threading.Event()
    release_the_first = threading.Event()
    guard = BottleTrackingService.assert_driver_can_progress_delivery

    def held_after_the_check(self, delivery, *args, **kwargs):
        result = guard(self, delivery, *args, **kwargs)
        if threading.current_thread().name == "first" and not first_passed_its_check.is_set():
            first_passed_its_check.set()
            assert release_the_first.wait(timeout=30), "the test never released the first tap"
        return result

    monkeypatch.setattr(BottleTrackingService, "assert_driver_can_progress_delivery", held_after_the_check)
    answers, errors = {}, []
    first_thread = _thread("first", pg_app, first, answers, errors, finally_set=first_passed_its_check)
    second_thread = _thread("second", pg_app, second, answers, errors)

    first_thread.start()
    try:
        assert first_passed_its_check.wait(timeout=30), "the first tap never reached its bottle check"
        assert errors == [], errors
        second_thread.start()
        assert _await_a_lock_waiter(pg_db), "the second tap did not wait: nothing holds the crew's rows"
        assert second_thread.is_alive(), "the second tap finished while the first one held the crew's rows"
    finally:
        release_the_first.set()
        _finish(first_thread, *([second_thread] if second_thread.ident else []))
    assert errors == []
    return answers


@pytest.mark.parametrize("first_to_tap", ["owner", "co_driver"])
def test_two_crew_members_tapping_on_the_way_at_once_cannot_promise_the_same_bottles(
    pg_app, pg_db, monkeypatch, enqueued, first_to_tap  # noqa: F811 -- `enqueued` is the fixture
):
    monkeypatch.setitem(pg_app.config, "BOTTLE_SESSION_ENFORCEMENT_STRICT", True)
    world = _crew_world(pg_db)
    second_to_tap = "co_driver" if first_to_tap == "owner" else "owner"
    first_driver, first_stop = world.stops[first_to_tap]
    second_driver, second_stop = world.stops[second_to_tap]
    first_stop_id, second_stop_id = first_stop.id, second_stop.id

    answers = _race_two_on_the_way_taps(
        pg_app,
        pg_db,
        monkeypatch,
        _taps_on_the_way(first_driver, first_stop_id),
        _taps_on_the_way(second_driver, second_stop_id),
    )
    (first_status, first_body), (second_status, second_body) = answers["first"], answers["second"]
    assert first_status == 200, first_body
    assert first_body["data"]["status"] == "in_transit"
    assert second_status == 400, second_body
    assert second_body["error_code"] == "BOTTLE_SESSION_CAPACITY_EXCEEDED"
    assert second_body["details"] == {
        "available": LOADED - NEEDED,
        "required": NEEDED,
        "shortfall": NEEDED - (LOADED - NEEDED),
        "validation_errors": [],
    }
    pg_db.session.expire_all()
    assert pg_db.session.get(Delivery, first_stop_id).status is DeliveryStatus.IN_TRANSIT
    assert pg_db.session.get(Delivery, second_stop_id).status is DeliveryStatus.PICKED_UP
    on_the_way = DeliveryStatusHistory.query.filter_by(new_status=DeliveryStatus.IN_TRANSIT).all()
    assert [row.delivery_id for row in on_the_way] == [first_stop_id]
    # Only the stop that really is on its way tells its customer so.
    assert [args for name, args, _kwargs in enqueued if name == send_delivery_update_task.name] == [(on_the_way[0].id,)]


@pytest.mark.parametrize("first_to_tap", ["owner", "co_driver"])
def test_two_crew_taps_on_the_way_on_the_last_ten_litre(
    pg_app, pg_db, monkeypatch, enqueued, first_to_tap  # noqa: F811 -- `enqueued` is the fixture
):
    """10 L 4 on board; the owner and his co-driver each hold a picked-up stop of 4. One tap passes; the other
    counts the first one's stop and is refused with 0 available."""
    from tests.integration.trip_unit_builders import (
        bind,
        delivery_for,
        member,
        order_with_lines,
        ten_litre,
        trip,
        trip_category,
        trip_customer,
        trip_driver,
    )

    monkeypatch.setitem(pg_app.config, "BOTTLE_SESSION_ENFORCEMENT_STRICT", True)
    customer = trip_customer(pg_db)
    owner = trip_driver(pg_db, "Olim", telegram_id="900000012")
    co_driver = trip_driver(pg_db, "Kamol", telegram_id="900000013")
    unit = ten_litre(pg_db, trip_category(pg_db))
    unit_id, unit_name = unit.id, unit.name
    session = trip(pg_db, owner, units={unit_id: 4})
    member(pg_db, session, co_driver)
    stops = {}
    for who, driver in (("owner", owner), ("co_driver", co_driver)):
        order = order_with_lines(pg_db, customer, [(unit, 4)], status=OrderStatus.OUT_FOR_DELIVERY)
        delivery = delivery_for(pg_db, order, driver, DeliveryStatus.PICKED_UP)
        bind(pg_db, session, order, driver)
        stops[who] = (driver, delivery.id)
    second_to_tap = "co_driver" if first_to_tap == "owner" else "owner"
    first_driver, first_stop_id = stops[first_to_tap]
    second_driver, second_stop_id = stops[second_to_tap]

    answers = _race_two_on_the_way_taps(
        pg_app,
        pg_db,
        monkeypatch,
        _taps_on_the_way(first_driver, first_stop_id),
        _taps_on_the_way(second_driver, second_stop_id),
    )
    (first_status, first_body), (second_status, second_body) = answers["first"], answers["second"]
    assert first_status == 200, first_body
    assert second_status == 400, second_body
    assert second_body["error_code"] == "BOTTLE_SESSION_CAPACITY_EXCEEDED"
    assert second_body["details"] == {
        "product_id": unit_id,
        "product_name": unit_name,
        "available": 0,
        "required": 4,
        "shortfall": 4,
        "validation_errors": [],
    }
    pg_db.session.expire_all()
    assert pg_db.session.get(Delivery, first_stop_id).status is DeliveryStatus.IN_TRANSIT
    assert pg_db.session.get(Delivery, second_stop_id).status is DeliveryStatus.PICKED_UP


# --------------------------------------------------------------------------- #
# A delivery of the crew's commits while an On the way tap is counting
# --------------------------------------------------------------------------- #


def _lock_waiting_statements(pg_db):
    """What each connection to this database that waits on a lock right now is running. A fresh connection per
    read: `pg_stat_activity` is a snapshot taken once per transaction."""
    with pg_db.engine.connect() as connection:
        return (
            connection.execute(
                text(
                    "SELECT query FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            )
            .scalars()
            .all()
        )


def _is_the_crew_lock(statement):
    return "FROM delivery_persons" in statement and "FOR UPDATE" in statement


def _admin_marks_delivered(admin, order_id):
    """What Orders.js sends through `adminService.updateOrderStatus` for a pick of Delivered."""
    headers = {
        "Authorization": f"Bearer {create_access_token(identity=str(admin.id), additional_claims={'role': 'admin'})}",
        "Content-Type": "application/json",
    }
    return lambda client: client.put(
        ADMIN_ORDER_STATUS.format(order_id), json={"status": "delivered", "notes": ""}, headers=headers
    )


def _customer_updates(published):
    """Every customer update published, as (delivery id, status its history row moved to)."""
    rows = []
    for name, args, _kwargs in published:
        if name == send_delivery_update_task.name:
            history = DeliveryStatusHistory.query.get(args[0])
            rows.append((history.delivery_id, history.new_status))
    return rows


def _assert_the_tap_counted_the_truck_after_the_delivery(world, answers, owner_stop_id, delivered_stop_id, pg_db):
    """The delivery took its 8 bottles off the truck and its stop off the road: 2 full on board, nothing on its
    way, so the owner's 8 are refused with 2 free. A count that took the stops after the delivery but the truck's
    tallies before it saw 10 free and let the tap promise 8 bottles the truck no longer had."""
    tap_status, tap_body = answers["tap"]
    assert tap_status == 400, tap_body
    assert tap_body["error_code"] == "BOTTLE_SESSION_CAPACITY_EXCEEDED"
    assert tap_body["details"] == {
        "available": LOADED - NEEDED,
        "required": NEEDED,
        "shortfall": NEEDED - (LOADED - NEEDED),
        "validation_errors": [],
    }
    pg_db.session.expire_all()
    assert pg_db.session.get(DriverBottleSession, world.session_id).full_bottles_on_board == LOADED - NEEDED
    assert pg_db.session.get(Delivery, delivered_stop_id).status is DeliveryStatus.DELIVERED
    assert pg_db.session.get(Delivery, owner_stop_id).status is DeliveryStatus.PICKED_UP
    assert DeliveryStatusHistory.query.filter_by(delivery_id=owner_stop_id).count() == 0


def test_an_on_the_way_tap_that_queued_behind_a_co_drivers_delivery_counts_the_truck_after_it(
    pg_app, pg_db, monkeypatch, enqueued  # noqa: F811 -- `enqueued` is the fixture
):
    """The co-driver hands over his 8-bottle stop at the door while the owner taps On the way for his own 8.

    The co-driver's Delivered has booked the 8 bottles off the trip and written his DeliveryPerson row, and has not
    committed. The owner's tap reads the trip as last committed (10 full, the co-driver's stop still on its way),
    then queues on the crew lock behind that row. When the delivery commits and the tap gets the lock, its count
    must see the truck as the delivery left it."""
    monkeypatch.setitem(pg_app.config, "BOTTLE_SESSION_ENFORCEMENT_STRICT", True)
    world = _crew_world(pg_db, co_driver_stop=DeliveryStatus.ARRIVED)
    owner, owner_stop = world.stops["owner"]
    co_driver, delivered_stop = world.stops["co_driver"]
    owner_stop_id, delivered_stop_id = owner_stop.id, delivered_stop.id

    delivery_holds_its_rows = threading.Event()
    release_the_delivery = threading.Event()
    book_the_bottles = OrderService._record_delivery_bottles

    # The staff route's Delivered writes the driver's DeliveryPerson row (his delivery totals) before it books the
    # bottles. Held right after the booking, it holds both rows, uncommitted.
    def held_after_booking_the_bottles(self, order, *args, **kwargs):
        book_the_bottles(self, order, *args, **kwargs)
        if threading.current_thread().name == "delivery" and not delivery_holds_its_rows.is_set():
            delivery_holds_its_rows.set()
            assert release_the_delivery.wait(timeout=30), "the test never released the delivery"

    monkeypatch.setattr(OrderService, "_record_delivery_bottles", held_after_booking_the_bottles)
    answers, errors = {}, []
    delivery = _thread(
        "delivery",
        pg_app,
        _taps(co_driver, delivered_stop_id, "delivered"),
        answers,
        errors,
        finally_set=delivery_holds_its_rows,
    )
    tap = _thread("tap", pg_app, _taps_on_the_way(owner, owner_stop_id), answers, errors)

    delivery.start()
    try:
        assert delivery_holds_its_rows.wait(timeout=30), "the delivery never booked its bottles"
        assert errors == [], errors
        tap.start()
        assert _await_a_lock_waiter(pg_db), "the tap did not wait on the delivery"
        waiting = _lock_waiting_statements(pg_db)
        assert len(waiting) == 1 and _is_the_crew_lock(waiting[0]), waiting
        assert tap.is_alive(), "the tap finished while the delivery held the co-driver's row"
    finally:
        release_the_delivery.set()
        _finish(delivery, *([tap] if tap.ident else []))

    assert errors == []
    delivered_status, delivered_body = answers["delivery"]
    assert delivered_status == 200, delivered_body
    assert delivered_body["data"]["status"] == "delivered"
    _assert_the_tap_counted_the_truck_after_the_delivery(world, answers, owner_stop_id, delivered_stop_id, pg_db)
    # Only the stop that was really delivered tells its customer anything.
    assert _customer_updates(enqueued) == [(delivered_stop_id, DeliveryStatus.DELIVERED)]


def test_a_stop_closed_from_the_orders_page_just_before_the_tap_reads_the_stops_on_their_way_counts_whole(
    pg_app, pg_db, monkeypatch, enqueued  # noqa: F811 -- `enqueued` is the fixture
):
    """An admin marks the co-driver's 8-bottle order Delivered on the Orders page while the owner taps On the way.

    That completion writes no DeliveryPerson row, so the crew lock does not hold it back: it can commit anywhere in
    the tap's count. Here it commits once the tap has read the trip and holds the crew lock, just before the tap
    reads the stops on their way. The count must take the delivery whole: the stop gone from the road AND its 8
    bottles gone from the truck. A count that read the truck's tallies before the stops, even under the lock, took
    the first half only."""
    monkeypatch.setitem(pg_app.config, "BOTTLE_SESSION_ENFORCEMENT_STRICT", True)
    world = _crew_world(pg_db, co_driver_stop=DeliveryStatus.ARRIVED)
    admin = _user(pg_db, "+998901242004", role=UserRole.ADMIN, user_type=UserType.STAFF)
    owner, owner_stop = world.stops["owner"]
    _co_driver, delivered_stop = world.stops["co_driver"]
    owner_stop_id, delivered_stop_id, delivered_order_id = owner_stop.id, delivered_stop.id, delivered_stop.order_id

    answers, errors = {}, []
    admin_closes_the_stop = _thread("admin", pg_app, _admin_marks_delivered(admin, delivered_order_id), answers, errors)
    tap = _thread("tap", pg_app, _taps_on_the_way(owner, owner_stop_id), answers, errors)
    crew_locked = threading.Event()
    closed_before_the_stops_were_read = []

    def before_each_statement(_conn, _cursor, statement, _parameters, _context, _executemany):
        if threading.current_thread().name != "tap":
            return
        if _is_the_crew_lock(statement):
            crew_locked.set()
        elif crew_locked.is_set() and "deliveries.status IN" in statement and not admin_closes_the_stop.ident:
            admin_closes_the_stop.start()
            admin_closes_the_stop.join(timeout=30)
            closed_before_the_stops_were_read.append(not admin_closes_the_stop.is_alive())

    event.listen(pg_db.engine, "before_cursor_execute", before_each_statement)
    try:
        tap.start()
        _finish(tap)
    finally:
        event.remove(pg_db.engine, "before_cursor_execute", before_each_statement)
        if admin_closes_the_stop.ident:
            _finish(admin_closes_the_stop)

    assert errors == []
    assert closed_before_the_stops_were_read == [True], "the admin's Delivered waited on the tap's rows"
    closed_status, closed_body = answers["admin"]
    assert closed_status == 200, closed_body
    assert closed_body["data"]["order"]["status"] == OrderStatus.DELIVERED.value
    _assert_the_tap_counted_the_truck_after_the_delivery(world, answers, owner_stop_id, delivered_stop_id, pg_db)


# --------------------------------------------------------------------------- #
# A Delivered on the crew's trip: against the owner's close, and twice on one 10 L line
# --------------------------------------------------------------------------- #

SEALED_TRIP_KEYS = (
    "status",
    "bottles_delivered",
    "bottles_collected_from_customers",
    "bottles_returned_to_warehouse",
    "discrepancy",
)
SEALED_LINE_KEYS = ("product_id", "loaded", "delivered", "returned", "discrepancy")


def _sealed_figures(trip_payload):
    """What a close seals, from a trip payload (`serialize_bottle_session`): the 18.9 L figures and each line's."""
    return (
        {key: trip_payload[key] for key in SEALED_TRIP_KEYS},
        [{key: line[key] for key in SEALED_LINE_KEYS} for line in trip_payload["units"]],
    )


@pytest.mark.parametrize("path", ["staff_route", "orders_page"])
@pytest.mark.parametrize("first,strict_on", [("close", True), ("close", False), ("delivered", True)])
def test_the_owners_close_and_a_co_drivers_delivered_never_change_the_sealed_figures(
    pg_app, pg_db, monkeypatch, enqueued, first, strict_on, path  # noqa: F811 -- `enqueued` is the fixture
):
    """The owner is back at the depot (10 full and 6 x 10 L loaded) and closes the trip while his co-driver hands over
    a stop of 3 x 18.9 L and 2 x 10 L at a door.

    Close first, held right after it locked the trip and released the undelivered stops: the Delivered waits on the
    trip, then finds the stop's binding released. Strict refuses it (BOTTLE_SESSION_REQUIRED); legacy delivers it and
    adds nothing to the closed trip. Delivered first, held right after its tally with the trip still locked: the close
    waits, then seals figures that include the delivery. Either way the trip read back afterwards carries exactly the
    figures the close answered.

    `path` is how the Delivered arrives: the co-driver's bot tap (`staff_route`) or the office marking the stop on the
    Orders page (`orders_page`, an admin token, the body `adminService.updateOrderStatus` sends). Same stop, same close,
    same sealed figures."""
    from tests.integration.test_bottle_transfer_races_pg_e2e import _double_tap
    from tests.integration.trip_unit_builders import (
        bind,
        delivery_for,
        member,
        nineteen,
        order_with_lines,
        ten_litre,
        trip,
        trip_category,
        trip_customer,
        trip_driver,
    )

    monkeypatch.setitem(pg_app.config, "BOTTLE_SESSION_ENFORCEMENT_STRICT", strict_on)
    customer = trip_customer(pg_db)
    owner = trip_driver(pg_db, "Olim", telegram_id="900000022")
    co_driver = trip_driver(pg_db, "Kamol", telegram_id="900000023")
    category = trip_category(pg_db)
    water, unit = nineteen(pg_db, category), ten_litre(pg_db, category)
    unit_id = unit.id
    session = trip(pg_db, owner, bottles=LOADED, units={unit_id: 6})
    trip_id = session.id
    member(pg_db, session, co_driver)
    order = order_with_lines(pg_db, customer, [(water, 3), (unit, 2)], status=OrderStatus.OUT_FOR_DELIVERY)
    stop_id = delivery_for(pg_db, order, co_driver, DeliveryStatus.ARRIVED).id
    bind(pg_db, session, order, co_driver)
    order_id = order.id
    admin = _user(pg_db, "+998901242005", role=UserRole.ADMIN, user_type=UserType.STAFF)

    def delivers(client):
        """The Delivered of the co-driver's stop, by the path under test."""
        if path == "orders_page":
            return _admin_marks_delivered(admin, order_id)(client)
        return _taps(co_driver, stop_id, "delivered")(client)

    if first == "close":
        (close_status, close_body), (delivered_status, delivered_body) = _double_tap(
            pg_app,
            pg_db,
            monkeypatch,
            held_method="_release_open_bindings_for_session",
            first_tap=_close_trip(owner, LOADED, {unit_id: 6}),
            second_tap=delivers,
        )
    else:
        (delivered_status, delivered_body), (close_status, close_body) = _double_tap(
            pg_app,
            pg_db,
            monkeypatch,
            owner=OrderService,
            held_method="_record_delivery_bottles",
            first_tap=delivers,
            second_tap=_close_trip(owner, LOADED - 3, {unit_id: 6 - 2}),
        )

    assert close_status == 200, close_body
    sealed = close_body["data"]
    if first == "close":
        assert (sealed["bottles_delivered"], sealed["discrepancy"]) == (0, 0)
        assert [(line["product_id"], line["delivered"]) for line in sealed["units"]] == [(unit_id, 0)]
        if strict_on:
            assert delivered_status == 400, delivered_body
            # The staff routes answer the code at the top level; the admin route nests it under `data`.
            code = delivered_body["data"]["error_code"] if path == "orders_page" else delivered_body["error_code"]
            assert code == "BOTTLE_SESSION_REQUIRED"
        else:
            assert delivered_status == 200, delivered_body
            pg_db.session.expire_all()
            assert pg_db.session.get(Order, order_id).status is OrderStatus.DELIVERED
    else:
        assert delivered_status == 200, delivered_body
        assert (sealed["bottles_delivered"], sealed["discrepancy"]) == (3, 0)
        assert [(line["product_id"], line["delivered"]) for line in sealed["units"]] == [(unit_id, 2)]
    pg_db.session.expire_all()
    after = serialize_bottle_session(pg_db.session.get(DriverBottleSession, trip_id))
    assert _sealed_figures(after) == _sealed_figures(sealed)


def test_two_delivered_taps_add_to_one_line(
    pg_app, pg_db, monkeypatch, enqueued  # noqa: F811 -- `enqueued` is the fixture
):
    """The owner and his co-driver each hand over a 10 L stop, 4 and 3, at once. The second Delivered waits for the
    first, then adds to what it wrote: the trip's one 10 L line counts both."""
    from tests.integration.test_bottle_transfer_races_pg_e2e import _double_tap
    from tests.integration.trip_unit_builders import (
        bind,
        delivery_for,
        member,
        order_with_lines,
        ten_litre,
        trip,
        trip_category,
        trip_customer,
        trip_driver,
    )

    customer = trip_customer(pg_db)
    owner = trip_driver(pg_db, "Olim", telegram_id="900000032")
    co_driver = trip_driver(pg_db, "Kamol", telegram_id="900000033")
    unit = ten_litre(pg_db, trip_category(pg_db))
    unit_id = unit.id
    session = trip(pg_db, owner, units={unit_id: 12})
    trip_id = session.id
    member(pg_db, session, co_driver)
    stops = {}
    for who, driver, quantity in (("owner", owner, 4), ("co_driver", co_driver, 3)):
        order = order_with_lines(pg_db, customer, [(unit, quantity)], status=OrderStatus.OUT_FOR_DELIVERY)
        stops[who] = delivery_for(pg_db, order, driver, DeliveryStatus.ARRIVED).id
        bind(pg_db, session, order, driver)

    (first_status, first_body), (second_status, second_body) = _double_tap(
        pg_app,
        pg_db,
        monkeypatch,
        owner=OrderService,
        held_method="_record_delivery_bottles",
        first_tap=_taps(owner, stops["owner"], "delivered"),
        second_tap=_taps(co_driver, stops["co_driver"], "delivered"),
    )

    assert first_status == 200, first_body
    assert second_status == 200, second_body
    pg_db.session.expire_all()
    line = DriverBottleSessionUnit.query.filter_by(session_id=trip_id, product_id=unit_id).one()
    assert (line.delivered, line.on_board) == (4 + 3, 12 - 4 - 3)
