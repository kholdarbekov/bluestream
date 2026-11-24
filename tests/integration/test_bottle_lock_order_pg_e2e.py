"""REAL POSTGRES: two hands on one truck at once both finish, and the trip's tallies keep both.

The lock order every path keeps (spec 2026-11-09 §4.2): the crew's DeliveryPerson rows first, in user-id order, then
the trip row. Each race below breaks that order in one place, or loses a write:

* The driver closes his trip while an admin assigns him a pool order. The close moves him to the depot, which writes
  his DeliveryPerson row; the assignment locks that row and then binds the order to his trip, and the binding's
  foreign-key check takes FOR KEY SHARE on the trip row. A close that locked the trip FOR UPDATE and then wrote the
  DeliveryPerson row deadlocked with it (40P01). `close_bottle_session` now writes the depot position first and locks
  the trip FOR NO KEY UPDATE, which a binding's key check does not wait on.
* The owner closes the trip while his co-driver taps On the way for a stop the trip never carried (the progress
  guard locks the crew's rows, then late-binds the order to the trip): the same cycle, the same fix.
* An admin assigns a driver a pool order while the driver's transfer send holds his trip. The binding's key check does
  not queue behind the send's FOR NO KEY UPDATE; under FOR UPDATE every bind waited on every send, count and close.
* Two crew members tap Delivered on one trip, or one taps Delivered while the other picks up empties at a door. Each
  adds to the trip's Delivered and collected-empties tallies; read-then-write let the second overwrite the first. The
  tallies are now added in SQL (`BottleTrackingService.add_to_session_tallies`).
* An admin hands a stop from one co-driver to the other while one of them taps On the way. `assign_driver` locked the
  new driver's row, then wrote the old driver's (the workload counter), while the crew lock takes them in user-id
  order. It now locks both, in user-id order, before either is written.
* Product rows come before the trip row (spec 2026-11-18 §6.2). An office edit locks the product rows, then adds to
  the trip's Delivered tally; a try-out hand-off writes the product row, then adds to it; a cash Delivered added to the
  tally first and locked the product rows afterwards, to take the stock. Each pair deadlocked (40P01). A Delivered now
  locks the product rows it will take the stock from right after it claims the order, before it books any bottle, and
  the hand-off locks its products before it writes them. The edit races the same way when it adds 10 L to the trip's
  line, or adds a new 10 L line to the order while the Delivered takes 10 L stock; every product lock on these paths is
  FOR NO KEY UPDATE, so none upgrades behind the trip lock. The edit re-reads the stock under its lock, so the
  Delivered's stock step, committed while the edit waited, is not overwritten.
* Two office confirmations of paid card orders holding the same two products in opposite line order: each took the
  stock line by line, locking each product row as it reached it, and they deadlocked (40P01). A confirmation now
  locks all its rows in one id-ordered statement first.
* An office edit can commit between the driver's screen being read and his Delivered claiming the order. The Delivered
  now re-reads the order after the claim, its lines and its total, so it books the edited quantity, a line the edit
  added and none it removed, and settles the cash against the edited total.
* An office edit of a delivered order changes the trip it was delivered on while its driver closes that trip. The edit
  locks the trip row before it reads whether the trip is closed, so it reopens a trip the close sealed while it
  waited (and a close behind the edit seals the edited tally); a second edit behind one that reopened the trip sees it
  open and only adds to it.
* An office edit of a delivered order and a Delivered of another order at the same place on the same trip both book
  the place's bottle balance, then the trip. The edit locked the trip before the balance row and deadlocked with the
  Delivered (40P01); it now locks the trip after its ledger rows, as the Delivered does.
* An office edit that reopens a driver's closed trip, against his Load or his join of another driver's trip: the edit
  locks his DeliveryPerson row before the order, the row the open and the join take first, so the later of the two
  is refused for the earlier. (The edit's loyalty estimate committed in the middle of the edit for a customer with no
  loyalty account yet, releasing that lock; it no longer commits.)
* That lock is FOR KEY SHARE, against the open's and the join's FOR UPDATE. An office edit of a customer's delivered
  cash order, against the driver recording her debt payment or the office cancelling her card-paid order he holds:
  each of those locks her open debts (the edited order's payment among them) and then writes his DeliveryPerson row.
  Taken FOR UPDATE, the edit held that row and waited for the payment while they waited for the row (40P01). It also
  kept his own Delivered off the balance and trip rows, so the edit-and-Delivered races above stopped pinning their
  lock order.
* An office edit of a customer's delivered cash order while the driver records her paying that same order: the
  payment's allocation holds the order row (its foreign key) until it commits, and the edit's order lock, waiting for
  it, returned the order's payment as it was before. The edit priced the new total as if nothing had been paid. It
  now re-reads the payment under its row lock where it writes it.
* An office edit of a first-timer's order while he opens his AquaCoins: the edit's points estimate created his loyalty
  account inside the edit (flushed, uncommitted), so his page waited on his user id and then failed on the duplicate.
  The estimate creates nothing now.
* Two office edits of a first-timer's delivered, paid order: the first edit's AquaCoins step created his account and
  committed, so the order row was free half way through it, and the second edit refunded against a total the first
  had not rewritten yet. The account is now created inside the edit's own transaction; an edit that fails after that
  step leaves nothing behind.
* A driver who left a trip has a stop still bound to it, and moves it on (On the way, or the office's Delivered) while
  the trip's owner closes it: the guard moves the binding onto the driver's own trip. The close read its open stops
  and then deleted each binding by id, so after waiting on the move it deleted the moved one. It now deletes in one
  statement whose trip condition Postgres re-checks on the moved row.
* A co-driver's try-out hand-off against the owner's close of their trip: the hand-off tallies only on an open trip,
  read under the trip's lock, so a close that committed while it waited keeps its sealed figures; a close behind it
  seals what the hand-off added.
* The driver's Handed over and the office's Complete of one try-out hand-off: both queue on the try-out's row, so the
  stock and the trip book it once.
* An admin saves a product's "counted on the bottle trip" setting while a driver opens his trip. The save locks the
  product row, then the trips table in SHARE mode; a trip open takes the table in ROW EXCLUSIVE mode before it reads
  the setting. Whichever comes first, the other waits for it and reads what it committed. A count that opens the
  receiver a trip (he had nothing open) is a trip open like any other.

`with_for_update` compiles to nothing on SQLite, so this runs on a real, fully-migrated Postgres database (`pg_app`).
The interleaving is forced the way test_bottle_transfer_races_pg_e2e.py forces its own (`_double_tap`): the first
request is held right after the locks it takes, before it commits, and the second starts only then. It must wait on
the first, except the assignment behind a send and the edit before a Delivered's claim, which must not. Each request
runs on its own thread, app context, session and connection, as two gunicorn workers would. Every wait is bounded, so a
broken lock order fails instead of hanging. Celery publishing is the only thing faked (`enqueued`).

Run: docker run --rm --network bluestream_default --env-file .env -v "$(pwd):/app" -w /app -e FLASK_ENV=testing
-e TESTING=true bluestream-business_app sh -c 'pip install -q pytest-xdist >/dev/null 2>&1;
pytest tests/integration/test_bottle_lock_order_pg_e2e.py -n0 --no-cov --no-header -q'
"""

import os
import threading
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from flask import current_app

from business_app.models.bottle import (
    BottleLedger,
    DriverBottleSession,
    DriverBottleSessionOrder,
    DriverBottleSessionUnit,
    DriverSessionMembership,
)
from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.loyalty import LoyaltyPoints, LoyaltyProgram
from business_app.models.order import Order, OrderEditHistory, OrderItem
from business_app.models.payment import CashCollectionEvent, Payment
from business_app.models.product import Product, ProductCategory
from business_app.models.tryout import ProductTryout, TryoutBottleLedger, TryoutTask
from business_app.models.user import UserAddress
from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.services.cash_collection_service import CashCollectionService
from business_app.services.driver_reconciliation_service import DriverReconciliationService
from business_app.services.inventory_service import InventoryService
from business_app.services.order_edit_service import OrderEditService
from business_app.services.order_service import OrderService
from business_app.services.product_fiscal_service import ProductFiscalService
from business_app.services.tryout_service import TryoutService
from shared.enums import (
    BottleLedgerEventType,
    DeliveryStatus,
    DriverBottleSessionStatus,
    DriverSessionMembershipStatus,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    TryoutBottleLedgerEventType,
    TryoutTaskStatus,
    TryoutTaskType,
    UserRole,
    UserType,
)
from staff_bot.api_client import StaffAPIClient
from staff_bot.handlers.delivery.bottle_collection import BottleCollectionHandler
from tests.integration.test_bottle_transfer_races_pg_e2e import _admin_bearer, _bearer, _double_tap
from tests.integration.test_customer_cancel_races_pg import (
    _await_a_lock_waiter,
    _finish,
    _lock_waiters,
    _thread,
    _user,
)
from tests.integration.test_on_the_way_concurrency_pg_e2e import _crew_member
from tests.integration.test_redispatch_reschedules_to_today import enqueued  # noqa: F401 -- a fixture
from tests.integration.trip_unit_builders import order_with_lines, ten_litre, trip, trip_category, trip_driver

pytestmark = [pytest.mark.integration, pytest.mark.e2e]

CLOSE_SESSION = "/api/v1/staff/bottles/session/close"
TRANSFERS = "/api/v1/staff/bottles/transfers"
COLLECTION = "/api/v1/staff/bottles/collection"
STATUS = "/api/v1/staff/delivery/{}/status"
ADMIN_ASSIGN = "/api/v1/admin/staff/delivery/assign/{}"
ADMIN_REASSIGN = "/api/v1/admin/staff/delivery/reassign/{}"
ADMIN_EDIT = "/api/v1/admin/orders/{}/edit"
HANDOFF = "/api/v1/staff/tryout-tasks/{}/complete-handoff"
ADMIN_COMPLETE_TASK = "/api/v1/admin/tryout-tasks/{}/complete"
APPROVE_JOIN = "/api/v1/staff/bottles/session/join-request/approve"
OPEN_SESSION = "/api/v1/staff/bottles/session/open"
CASH_COLLECTIONS = "/api/v1/staff/cash-collections"
LOYALTY_POINTS = "/api/v1/loyalty/points"
ADMIN_ORDER_STATUS = "/api/v1/admin/orders/{}/status"
LOADED = 10


def _water(pg_db):
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
    pg_db.session.add(product)
    pg_db.session.commit()
    return product


def _customer(pg_db, phone):
    return _user(pg_db, phone, role=UserRole.CUSTOMER, user_type=UserType.INDIVIDUAL)


def _stop(pg_db, product, customer, *, bottles, status, driver=None, trip=None, payment_method=None):
    """A `bottles`-bottle order and its delivery at `status`, held by `driver` (None: in the pool) and bound to `trip`
    (None: never bound)."""
    address = UserAddress(
        user_id=customer.id,
        title="Door",
        full_address=f"{uuid.uuid4().hex[:6]} Lock Street, Tashkent",
        street_address="Lock Street",
        city="Tashkent",
        latitude=41.3111,
        longitude=69.2797,
    )
    pg_db.session.add(address)
    pg_db.session.flush()
    on_the_road = status in (DeliveryStatus.PICKED_UP, DeliveryStatus.IN_TRANSIT, DeliveryStatus.ARRIVED)
    total = Decimal("15000.00") * bottles
    order = Order(
        user_id=customer.id,
        order_number=f"ORD-LOCK-{uuid.uuid4().hex[:8].upper()}",
        status=OrderStatus.OUT_FOR_DELIVERY if on_the_road else OrderStatus.CONFIRMED,
        payment_method=payment_method,
        subtotal=total,
        delivery_fee=Decimal("0.00"),
        total_amount=total,
        delivery_address_id=address.id,
    )
    pg_db.session.add(order)
    pg_db.session.flush()
    pg_db.session.add(
        OrderItem(
            order_id=order.id,
            product_id=product.id,
            quantity=bottles,
            unit_price=Decimal("15000.00"),
            total_price=total,
        )
    )
    delivery = Delivery(
        order_id=order.id,
        status=status,
        delivery_person_id=driver.id if driver else None,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot="09:00-12:00",
    )
    pg_db.session.add(delivery)
    if trip is not None:
        pg_db.session.add(
            DriverBottleSessionOrder(session_id=trip.id, order_id=order.id, accepted_by_driver_id=driver.id)
        )
    pg_db.session.commit()
    return delivery


def _crew(pg_db):
    """The owner's trip, loaded with 10 full bottles, and his co-driver, an ACTIVE member. The owner is created first,
    so his user id is the lower: the crew lock takes his row first."""
    owner = _crew_member(pg_db, "+998901261001", "Owner")
    co_driver = _crew_member(pg_db, "+998901261002", "Codriver")
    trip = DriverBottleSession(driver_user_id=owner.id, bottles_loaded=LOADED, status=DriverBottleSessionStatus.OPEN)
    pg_db.session.add(trip)
    pg_db.session.flush()
    pg_db.session.add(
        DriverSessionMembership(
            session_id=trip.id,
            session_owner_id=owner.id,
            member_driver_id=co_driver.id,
            status=DriverSessionMembershipStatus.ACTIVE,
        )
    )
    pg_db.session.commit()
    assert owner.id < co_driver.id
    return SimpleNamespace(owner=owner, co_driver=co_driver, trip=trip, water=_water(pg_db))


def _closes(driver, returned):
    """The staff bot's Close trip, with the count he typed."""
    return lambda client: client.post(
        CLOSE_SESSION, json={"bottles_returned_to_warehouse": returned}, headers=_bearer(driver)
    )


def _taps(driver, delivery_id, status, metadata=None):
    """The staff bot's status tap, with the body `StaffAPIClient` sends."""
    body = StaffAPIClient.delivery_status_body(status, metadata)
    return lambda client: client.put(STATUS.format(delivery_id), json=body, headers=_bearer(driver))


def _binding_of(delivery_id):
    order_id = Delivery.query.get(delivery_id).order_id
    return [row.session_id for row in DriverBottleSessionOrder.query.filter_by(order_id=order_id)]


# --------------------------------------------------------------------------- #
# A close against an assignment, and against a late-binding On the way
# --------------------------------------------------------------------------- #


def test_a_close_and_an_admin_assignment_to_the_same_driver_both_finish(
    pg_app, pg_db, monkeypatch, enqueued  # noqa: F811 -- `enqueued` is the fixture
):
    """Ali is back at the depot with his 6 full bottles and closes his trip, while an admin assigns him a 3-bottle pool
    order from the Deliveries page. The close holds Ali's row and his trip; the assignment waits for both, finds the
    trip closed, and assigns the order without binding it to a trip that has ended."""
    admin = _user(pg_db, "+998901261009", role=UserRole.ADMIN, user_type=UserType.STAFF)
    driver = _crew_member(pg_db, "+998901261003", "Ali")
    trip = DriverBottleSession(driver_user_id=driver.id, bottles_loaded=6, status=DriverBottleSessionStatus.OPEN)
    pg_db.session.add(trip)
    pg_db.session.commit()
    trip_id = trip.id
    pool_stop = _stop(pg_db, _water(pg_db), _customer(pg_db, "+998901261004"), bottles=3, status=DeliveryStatus.PENDING)
    pool_stop_id = pool_stop.id

    def admin_assigns(client):
        """What Delivery.js sends for Assign."""
        return client.post(
            ADMIN_ASSIGN.format(pool_stop_id), json={"delivery_person_id": driver.id}, headers=_admin_bearer(admin)
        )

    # Held inside the close, after it has locked the trip and released its open orders, before it marks anything.
    (close_status, close_body), (assign_status, assign_body) = _double_tap(
        pg_app,
        pg_db,
        monkeypatch,
        held_method="_release_open_bindings_for_session",
        first_tap=_closes(driver, 6),
        second_tap=admin_assigns,
    )

    assert close_status == 200, close_body
    assert (close_body["data"]["status"], close_body["data"]["discrepancy"]) == ("closed", 0)
    assert assign_status == 200, assign_body
    assert (assign_body["data"]["delivery"]["status"], assign_body["data"]["delivery"]["delivery_person"]["id"]) == (
        "assigned",
        driver.id,
    )
    pg_db.session.expire_all()
    assert pg_db.session.get(DriverBottleSession, trip_id).status is DriverBottleSessionStatus.CLOSED
    assert _binding_of(pool_stop_id) == []


def test_a_close_and_a_co_drivers_late_binding_on_the_way_both_finish(
    pg_app, pg_db, monkeypatch, enqueued  # noqa: F811 -- `enqueued` is the fixture
):
    """The owner closes the trip (10 loaded, 10 returned) while his co-driver taps On the way for a 4-bottle stop an
    admin gave him before he joined, so it was never bound to the trip. The close holds the owner's row and the trip;
    the tap's crew lock waits for it. What this pins is the lock order: neither side deadlocks or errors, and the
    sealed trip's books stay sealed.

    It deliberately does NOT pin what the tap answers. The guard resolves the effective trip before its crew lock, so
    after waiting on the close it still sees the trip as open and late-binds the stop to it. That pre-existing gap
    (re-resolve the effective trip after the crew lock) is a recorded follow-up, not intended behaviour."""
    monkeypatch.setitem(pg_app.config, "BOTTLE_SESSION_ENFORCEMENT_STRICT", True)
    crew = _crew(pg_db)
    trip_id = crew.trip.id
    stop = _stop(
        pg_db,
        crew.water,
        _customer(pg_db, "+998901261005"),
        bottles=4,
        status=DeliveryStatus.PICKED_UP,
        driver=crew.co_driver,
    )
    stop_id = stop.id
    assert _binding_of(stop_id) == []

    (close_status, close_body), (tap_status, tap_body) = _double_tap(
        pg_app,
        pg_db,
        monkeypatch,
        held_method="_release_open_bindings_for_session",
        first_tap=_closes(crew.owner, LOADED),
        second_tap=_taps(crew.co_driver, stop_id, "in_transit"),
    )

    assert close_status == 200, close_body
    assert (close_body["data"]["status"], close_body["data"]["discrepancy"]) == ("closed", 0)
    # A deadlock (40P01) surfaces as a 500 from the tap's route; a refusal would be a 4xx. Either answer but a 500 is
    # outside this test's claim (see the docstring).
    assert tap_status != 500, tap_body
    pg_db.session.expire_all()
    closed = pg_db.session.get(DriverBottleSession, trip_id)
    assert (closed.status, closed.bottles_returned_to_warehouse, closed.discrepancy) == (
        DriverBottleSessionStatus.CLOSED,
        LOADED,
        0,
    )


@pytest.mark.parametrize("path", ["on_the_way", "orders_page_delivered"])
def test_a_close_leaves_a_stop_its_former_co_driver_moved_onto_his_own_trip(
    pg_app, pg_db, monkeypatch, enqueued, path  # noqa: F811 -- `enqueued` is the fixture
):
    """Kamol co-drove Olim's trip and took a stop of 2 x 10 L on it, then left and loaded his own trip (6 x 10 L): the
    stop is still bound to Olim's. Olim closes his trip at the depot (4 x 10 L back) at the moment the stop moves on:
    Kamol taps On the way (`on_the_way`), or the office marks it Delivered on the Orders page
    (`orders_page_delivered`). Either way the progress guard moves the stop's binding onto Kamol's trip.

    Held right after that move, uncommitted, the close's release of its open stops waits on the binding row. Once the
    move commits, the binding is on Kamol's trip, and a delivered stop's binding is history: the close releases only
    what is still on its own trip and not finished, so it must leave this one where it is. It deleted it by id: the
    stop lost its trip, and a delivered one lost the binding a later edit of the order follows to its trip."""
    from tests.integration.test_on_the_way_concurrency_pg_e2e import _admin_marks_delivered, _close_trip
    from tests.integration.trip_unit_builders import bind, delivery_for, trip_customer

    monkeypatch.setitem(pg_app.config, "BOTTLE_SESSION_ENFORCEMENT_STRICT", True)
    owner = trip_driver(pg_db, "Olim", telegram_id="900000062")
    co_driver = trip_driver(pg_db, "Kamol", telegram_id="900000063")
    admin = _user(pg_db, "+998901269801", role=UserRole.ADMIN, user_type=UserType.STAFF)
    unit = ten_litre(pg_db, trip_category(pg_db))
    unit_id = unit.id
    olims_trip_id = trip(pg_db, owner, units={unit_id: 4}).id
    kamols_trip = trip(pg_db, co_driver, units={unit_id: 6})
    kamols_trip_id = kamols_trip.id
    order = order_with_lines(pg_db, trip_customer(pg_db), [(unit, 2)], status=OrderStatus.OUT_FOR_DELIVERY)
    order_id = order.id
    at = DeliveryStatus.PICKED_UP if path == "on_the_way" else DeliveryStatus.ARRIVED
    stop_id = delivery_for(pg_db, order, co_driver, at).id
    bind(pg_db, pg_db.session.get(DriverBottleSession, olims_trip_id), order, co_driver)
    moves_on = (
        _taps(co_driver, stop_id, "in_transit") if path == "on_the_way" else _admin_marks_delivered(admin, order_id)
    )

    (move_status, move_body), (close_status, close_body) = _double_tap(
        pg_app,
        pg_db,
        monkeypatch,
        held_method="rebind_order_to_session",
        first_tap=moves_on,
        second_tap=_close_trip(owner, 0, {unit_id: 4}),
    )

    assert move_status == 200, move_body
    assert close_status == 200, close_body
    assert (close_body["data"]["status"], close_body["data"]["units"][0]["discrepancy"]) == ("closed", 0)
    pg_db.session.expire_all()
    assert _binding_of(stop_id) == [kamols_trip_id]
    assert pg_db.session.get(DriverBottleSession, olims_trip_id).status is DriverBottleSessionStatus.CLOSED
    if path == "on_the_way":
        assert pg_db.session.get(Delivery, stop_id).status is DeliveryStatus.IN_TRANSIT
    else:
        assert pg_db.session.get(Order, order_id).status is OrderStatus.DELIVERED
        line = DriverBottleSessionUnit.query.filter_by(session_id=kamols_trip_id, product_id=unit_id).one()
        assert (line.delivered, line.on_board) == (2, 6 - 2)


def test_an_assignment_binds_to_a_trip_while_a_transfer_send_holds_it(
    pg_app, pg_db, monkeypatch, enqueued  # noqa: F811 -- `enqueued` is the fixture
):
    """Ali (6 full + 2 empties) sends Vali 2 full + 2 empties while an admin assigns Ali a 3-bottle pool order. The send
    holds Ali's trip row; the assignment binds the order to that trip, and the binding's key check must not queue
    behind the send. It finishes while the send still holds the trip."""
    admin = _user(pg_db, "+998901261029", role=UserRole.ADMIN, user_type=UserType.STAFF)
    sender = _crew_member(pg_db, "+998901261021", "Ali")
    receiver = _crew_member(pg_db, "+998901261022", "Vali")
    trip = DriverBottleSession(
        driver_user_id=sender.id,
        bottles_loaded=6,
        bottles_collected_from_customers=2,
        status=DriverBottleSessionStatus.OPEN,
    )
    pg_db.session.add(trip)
    pg_db.session.commit()
    trip_id = trip.id
    pool_stop_id = _stop(
        pg_db, _water(pg_db), _customer(pg_db, "+998901261023"), bottles=3, status=DeliveryStatus.PENDING
    ).id

    send_holds_the_trip = threading.Event()
    release_the_send = threading.Event()
    recipients = BottleTrackingService._transfer_recipients_query

    # Held after the send has read (and locked) Ali's trip, before it books anything.
    def held_after_the_trip_lock(self, *args, **kwargs):
        result = recipients(self, *args, **kwargs)
        if threading.current_thread().name == "send" and not send_holds_the_trip.is_set():
            send_holds_the_trip.set()
            assert release_the_send.wait(timeout=30), "the test never released the send"
        return result

    monkeypatch.setattr(BottleTrackingService, "_transfer_recipients_query", held_after_the_trip_lock)
    answers, errors = {}, []
    send = _thread(
        "send",
        pg_app,
        lambda client: client.post(
            TRANSFERS,
            json={"receiverDriverId": receiver.id, "fullQuantity": 2, "emptyQuantity": 2},
            headers=_bearer(sender),
        ),
        answers,
        errors,
        finally_set=send_holds_the_trip,
    )
    assign = _thread(
        "assign",
        pg_app,
        lambda client: client.post(
            ADMIN_ASSIGN.format(pool_stop_id), json={"delivery_person_id": sender.id}, headers=_admin_bearer(admin)
        ),
        answers,
        errors,
    )

    send.start()
    try:
        assert send_holds_the_trip.wait(timeout=30), "the send never locked the trip"
        assert errors == [], errors
        assign.start()
        assign.join(timeout=15)
        assert not assign.is_alive(), "the assignment queued behind the send's lock on the trip"
        assert _lock_waiters(pg_db) == 0
        assert send.is_alive(), "the send finished before the assignment ran"
    finally:
        release_the_send.set()
        _finish(send, *([assign] if assign.ident else []))

    assert errors == []
    assign_status, assign_body = answers["assign"]
    assert assign_status == 200, assign_body
    send_status, send_body = answers["send"]
    assert send_status == 201, send_body
    assert (send_body["data"]["declared_full"], send_body["data"]["declared_empty"]) == (2, 2)
    pg_db.session.expire_all()
    assert _binding_of(pool_stop_id) == [trip_id]
    sent_from = pg_db.session.get(DriverBottleSession, trip_id)
    assert (sent_from.bottles_transferred_out, sent_from.empties_transferred_out) == (2, 2)


# --------------------------------------------------------------------------- #
# Two Delivered taps on one trip
# --------------------------------------------------------------------------- #


def test_two_crew_delivered_taps_on_one_trip_both_reach_its_tallies(
    pg_app, pg_db, monkeypatch, enqueued  # noqa: F811 -- `enqueued` is the fixture
):
    """The co-driver hands over 5 bottles and takes 1 empty back while the owner, next door, hands over 3 and takes 2
    empties. The co-driver's Delivered has written the trip's tallies and not committed; the owner's waits on the trip
    row, then adds his own on top."""
    monkeypatch.setitem(pg_app.config, "BOTTLE_SESSION_ENFORCEMENT_STRICT", True)
    crew = _crew(pg_db)
    trip_id = crew.trip.id
    co_drivers_stop = _stop(
        pg_db,
        crew.water,
        _customer(pg_db, "+998901261006"),
        bottles=5,
        status=DeliveryStatus.ARRIVED,
        driver=crew.co_driver,
        trip=crew.trip,
    )
    owners_stop = _stop(
        pg_db,
        crew.water,
        _customer(pg_db, "+998901261007"),
        bottles=3,
        status=DeliveryStatus.ARRIVED,
        driver=crew.owner,
        trip=crew.trip,
    )

    # Held right after the co-driver's Delivered booked its bottles and wrote the trip's tallies.
    (first_status, first_body), (second_status, second_body) = _double_tap(
        pg_app,
        pg_db,
        monkeypatch,
        owner=OrderService,
        held_method="_record_delivery_bottles",
        first_tap=_taps(crew.co_driver, co_drivers_stop.id, "delivered", {"bottles_returned": 1}),
        second_tap=_taps(crew.owner, owners_stop.id, "delivered", {"bottles_returned": 2}),
    )

    assert first_status == 200, first_body
    assert second_status == 200, second_body
    assert (first_body["data"]["status"], second_body["data"]["status"]) == ("delivered", "delivered")
    pg_db.session.expire_all()
    trip = pg_db.session.get(DriverBottleSession, trip_id)
    assert (trip.bottles_delivered, trip.bottles_collected_from_customers) == (5 + 3, 1 + 2)
    assert (trip.full_bottles_on_board, trip.empties_on_board) == (LOADED - 8, 3)


def test_a_doorstep_collection_and_a_crew_delivered_on_one_trip_both_reach_its_empties(
    pg_app, pg_db, monkeypatch, enqueued  # noqa: F811
):
    """The co-driver hands over 5 bottles and takes 1 empty back while the owner, at another door, picks up 4 empties
    with no order (the bot's bottle collection). Both add to the trip's collected empties."""
    monkeypatch.setitem(pg_app.config, "BOTTLE_SESSION_ENFORCEMENT_STRICT", True)
    crew = _crew(pg_db)
    trip_id = crew.trip.id
    co_drivers_stop = _stop(
        pg_db,
        crew.water,
        _customer(pg_db, "+998901261031"),
        bottles=5,
        status=DeliveryStatus.ARRIVED,
        driver=crew.co_driver,
        trip=crew.trip,
    )
    neighbour = _customer(pg_db, "+998901261032")
    door = UserAddress(
        user_id=neighbour.id,
        title="Neighbour",
        full_address="7 Lock Street, Tashkent",
        street_address="7 Lock Street",
        city="Tashkent",
        latitude=41.3112,
        longitude=69.2798,
    )
    pg_db.session.add(door)
    pg_db.session.commit()
    neighbour_id, door_id = neighbour.id, door.id

    def owner_collects(client):
        """What `StaffAPIClient` sends for a doorstep collection."""
        return client.post(
            COLLECTION,
            json={
                "customer_id": neighbour_id,
                "address_id": door_id,
                "quantity": 4,
                "notes": None,
                "idempotency_key": BottleCollectionHandler._new_intent_token(),
            },
            headers=_bearer(crew.owner),
        )

    # Held right after the co-driver's Delivered booked its bottles and wrote the trip's tallies.
    (delivered_status, delivered_body), (collected_status, collected_body) = _double_tap(
        pg_app,
        pg_db,
        monkeypatch,
        owner=OrderService,
        held_method="_record_delivery_bottles",
        first_tap=_taps(crew.co_driver, co_drivers_stop.id, "delivered", {"bottles_returned": 1}),
        second_tap=owner_collects,
    )

    assert delivered_status == 200, delivered_body
    assert collected_status == 200, collected_body
    pg_db.session.expire_all()
    trip = pg_db.session.get(DriverBottleSession, trip_id)
    assert (trip.bottles_delivered, trip.bottles_collected_from_customers) == (5, 1 + 4)


# --------------------------------------------------------------------------- #
# A reassignment between co-drivers against the crew lock
# --------------------------------------------------------------------------- #


def test_a_reassignment_between_co_drivers_and_an_on_the_way_tap_both_finish(
    pg_app, pg_db, monkeypatch, enqueued  # noqa: F811 -- `enqueued` is the fixture
):
    """An admin moves the owner's 3-bottle cash stop to the co-driver while the owner taps On the way for his other,
    4-bottle stop. The reassignment holds both drivers' rows, taken in user-id order as the crew lock takes them; the
    tap waits for them, then goes through. (Cash, so the reassignment runs the COD check, where it is held.)"""
    monkeypatch.setitem(pg_app.config, "BOTTLE_SESSION_ENFORCEMENT_STRICT", True)
    admin = _user(pg_db, "+998901261019", role=UserRole.ADMIN, user_type=UserType.STAFF)
    crew = _crew(pg_db)
    moved = _stop(
        pg_db,
        crew.water,
        _customer(pg_db, "+998901261010"),
        bottles=3,
        status=DeliveryStatus.ASSIGNED,
        driver=crew.owner,
        trip=crew.trip,
        payment_method=PaymentMethod.CASH,
    )
    tapped = _stop(
        pg_db,
        crew.water,
        _customer(pg_db, "+998901261011"),
        bottles=4,
        status=DeliveryStatus.PICKED_UP,
        driver=crew.owner,
        trip=crew.trip,
    )
    moved_id, tapped_id = moved.id, tapped.id

    def admin_reassigns(client):
        """What Delivery.js sends for Reassign."""
        return client.put(
            ADMIN_REASSIGN.format(moved_id),
            json={"new_delivery_person_id": crew.co_driver.id},
            headers=_admin_bearer(admin),
        )

    # Held at the COD check, right after the reassignment has locked the drivers' rows.
    (reassign_status, reassign_body), (tap_status, tap_body) = _double_tap(
        pg_app,
        pg_db,
        monkeypatch,
        owner=DriverReconciliationService,
        held_method="is_driver_blocked_from_cod",
        first_tap=admin_reassigns,
        second_tap=_taps(crew.owner, tapped_id, "in_transit"),
    )

    assert reassign_status == 200, reassign_body
    moved_now = reassign_body["data"]["delivery"]
    assert (moved_now["status"], moved_now["delivery_person"]["id"]) == ("assigned", crew.co_driver.id)
    assert tap_status == 200, tap_body
    assert tap_body["data"]["status"] == "in_transit"
    pg_db.session.expire_all()
    assert (pg_db.session.get(Delivery, moved_id).delivery_person_id, _binding_of(moved_id)) == (
        crew.co_driver.id,
        [crew.trip.id],
    )
    assert pg_db.session.get(Delivery, tapped_id).status is DeliveryStatus.IN_TRANSIT


# --------------------------------------------------------------------------- #
# Product rows before the trip row
# --------------------------------------------------------------------------- #


def _drivers_trip(pg_db, phone, *, delivered):
    """Dilshod's own open trip, loaded with 10 full bottles, `delivered` of them already handed over."""
    driver = _crew_member(pg_db, phone, "Dilshod")
    trip = DriverBottleSession(
        driver_user_id=driver.id,
        bottles_loaded=LOADED,
        bottles_delivered=delivered,
        status=DriverBottleSessionStatus.OPEN,
    )
    pg_db.session.add(trip)
    pg_db.session.commit()
    return driver, trip


def _cash_delivered(driver, delivery_id, cash):
    """The bot's Delivered on a cash stop: the cash the driver typed, no empties back."""
    return _taps(driver, delivery_id, "delivered", {"cash_collected": cash, "bottles_returned": 0})


def _admin_edits(admin, order_id, lines, reason):
    """What Orders.js sends for Apply on the order's Edit items dialog: every line as `(order item id, product id,
    quantity)`, a new line with no order item id."""
    body = {
        "items": [
            {"orderItemId": order_item_id, "productId": product_id, "quantity": quantity}
            for order_item_id, product_id, quantity in lines
        ],
        "reason": reason,
    }
    return lambda client: client.post(ADMIN_EDIT.format(order_id), json=body, headers=_admin_bearer(admin))


@pytest.mark.parametrize("product", ["nineteen", "ten_litre", "mixed"])
def test_an_edit_and_a_cash_delivered_on_one_trip_never_deadlock(
    pg_app, pg_db, monkeypatch, enqueued, product  # noqa: F811 -- `enqueued` is the fixture
):
    """Dilshod handed a card customer his order earlier on this trip (order A, tallied) and now taps Delivered at a cash
    door (order B). Meanwhile the office edits A: the customer took one more. The edit locks the product rows it takes
    the extra unit's stock from, then adds it to the trip; a Delivered that tallied the trip before it locked the
    product rows for B's stock deadlocked with it. Holding the Delivered right after its tally, the edit must wait, and
    both must finish with every bottle, every 10 L unit and every unit of stock counted once.

    * nineteen: A had 2 bottles of water and gets a 3rd; B is 3 bottles.
    * ten_litre: A had 2 of the 10 L water and gets a 3rd; B is 3 of them.
    * mixed: A had 2 bottles of water and gets a NEW line of one 10 L; B is 3 of the 10 L. The new line's insert takes
      the 10 L row's foreign-key lock (FOR KEY SHARE) before the edit waits for the row's stock lock, so a Delivered
      that upgraded its own lock on that row to take B's stock (FOR UPDATE) would wait on the edit: the deadlock
      `PRODUCT_ROW_LOCK` in `confirm_reservations` keeps out.

    The edit's stock step re-reads the stock under its lock, so B's stock decrement, committed while the edit waited,
    is kept."""
    admin = _user(pg_db, "+998901261049", role=UserRole.ADMIN, user_type=UserType.STAFF)
    driver, trip = _drivers_trip(pg_db, "+998901261041", delivered=0 if product == "ten_litre" else 2)
    trip_id = trip.id
    water = _water(pg_db)
    water_id = water.id
    small = ten_litre(pg_db, trip_category(pg_db))
    small_id = small.id
    delivered_at = datetime.now(UTC)
    if product != "nineteen":
        # The trip had its 10 L line before A was delivered, so A's 10 L were tallied on it.
        pg_db.session.add(
            DriverBottleSessionUnit(
                session_id=trip_id,
                product_id=small_id,
                loaded=LOADED,
                delivered=2 if product == "ten_litre" else 0,
                created_at=delivered_at - timedelta(minutes=30),
            )
        )
        pg_db.session.commit()
    a_product, b_product = {"nineteen": (water, water), "ten_litre": (small, small), "mixed": (water, small)}[product]
    card_stop = _stop(
        pg_db,
        a_product,
        _customer(pg_db, "+998901261042"),
        bottles=2,
        status=DeliveryStatus.DELIVERED,
        driver=driver,
        trip=trip,
        payment_method=PaymentMethod.CARD,
    )
    card_stop.delivered_at = delivered_at
    card_stop.actual_delivery_time = delivered_at
    card_stop.order.status = OrderStatus.DELIVERED
    pg_db.session.commit()
    card_order_id, card_line_id = card_stop.order_id, card_stop.order.order_items[0].id
    if product == "mixed":
        edited = [(card_line_id, water_id, 2), (None, small_id, 1)]
    else:
        edited = [(card_line_id, a_product.id, 3)]
    cash_stop_id = _stop(
        pg_db,
        b_product,
        _customer(pg_db, "+998901261043"),
        bottles=3,
        status=DeliveryStatus.ARRIVED,
        driver=driver,
        trip=trip,
        payment_method=PaymentMethod.CASH,
    ).id

    # Held right after the Delivered added B's 3 to the trip (its `add_to_session_tallies`, the last write
    # `_record_delivery_bottles` makes), before it takes B's stock.
    (delivered_status, delivered_body), (edit_status, edit_body) = _double_tap(
        pg_app,
        pg_db,
        monkeypatch,
        owner=OrderService,
        held_method="_record_delivery_bottles",
        first_tap=_cash_delivered(driver, cash_stop_id, 45000),
        second_tap=_admin_edits(admin, card_order_id, edited, "Customer took one more"),
    )

    assert "deadlock" not in str(delivered_body), delivered_body
    assert "deadlock" not in str(edit_body), edit_body
    assert delivered_status == 200, delivered_body
    assert delivered_body["data"]["status"] == "delivered"
    assert edit_status == 200, edit_body
    assert edit_body["data"]["cascade_summary"]["bottle"]["affected_session_id"] == trip_id
    pg_db.session.expire_all()
    session = pg_db.session.get(DriverBottleSession, trip_id)
    lines = [(line.product_id, line.delivered) for line in session.unit_lines]
    stock = (pg_db.session.get(Product, water_id).stock_quantity, pg_db.session.get(Product, small_id).stock_quantity)
    if product == "nineteen":
        assert (session.bottles_delivered, lines) == (2 + 3 + 1, [])
        assert stock == (100 - 1 - 3, 1000)
    elif product == "ten_litre":
        assert (session.bottles_delivered, lines) == (0, [(small_id, 2 + 3 + 1)])
        assert stock == (100, 1000 - 1 - 3)
    else:
        assert (session.bottles_delivered, lines) == (2, [(small_id, 3 + 1)])
        assert stock == (100, 1000 - 3 - 1)


def test_a_tryout_handoff_and_a_cash_delivered_never_deadlock(
    pg_app, pg_db, monkeypatch, enqueued  # noqa: F811 -- `enqueued` is the fixture
):
    """Dilshod taps Delivered at a cash door with 3 bottles, and on the next tap completes a try-out hand-off of 1
    bottle. The hand-off writes the water's stock, then adds its bottle to the trip; a Delivered that tallied the trip
    before it locked the product row for its stock deadlocked with it. Holding the Delivered right after its tally, the
    hand-off must wait, and both must finish with every bottle and every unit of stock counted once."""
    admin = _user(pg_db, "+998901261059", role=UserRole.ADMIN, user_type=UserType.STAFF)
    driver, trip = _drivers_trip(pg_db, "+998901261051", delivered=2)
    trip_id = trip.id
    water = _water(pg_db)
    water_id = water.id
    cash_stop_id = _stop(
        pg_db,
        water,
        _customer(pg_db, "+998901261052"),
        bottles=3,
        status=DeliveryStatus.ARRIVED,
        driver=driver,
        trip=trip,
        payment_method=PaymentMethod.CASH,
    ).id
    tryout = TryoutService.create_tryout(
        {
            "trial_contact": {"first_name": "Trial", "phone": "+998901261053", "preferred_language": "uz"},
            "address": {
                "full_address": "3 Lock Street, Tashkent",
                "city": "Tashkent",
                "latitude": 41.3113,
                "longitude": 69.2799,
                "is_default": True,
            },
            "items": [{"product_id": water_id, "quantity": 1}],
            "assigned_driver_user_id": driver.id,
        },
        admin.id,
        source="admin",
    )
    tryout_id = tryout.id
    (handoff_task_id,) = [task.id for task in tryout.tasks if task.task_type is TryoutTaskType.HANDOFF]
    assert [item.returnable_bottles_due for item in tryout.items] == [Decimal("1.00")]

    def completes_the_handoff(client):
        """What the bot sends for Handed over (`StaffAPIClient.complete_tryout_handoff`)."""
        return client.post(HANDOFF.format(handoff_task_id), json={}, headers=_bearer(driver))

    # Held right after the Delivered added B's 3 bottles to the trip, before it takes B's stock.
    (delivered_status, delivered_body), (handoff_status, handoff_body) = _double_tap(
        pg_app,
        pg_db,
        monkeypatch,
        owner=OrderService,
        held_method="_record_delivery_bottles",
        first_tap=_cash_delivered(driver, cash_stop_id, 45000),
        second_tap=completes_the_handoff,
    )

    assert "deadlock" not in str(delivered_body), delivered_body
    assert "deadlock" not in str(handoff_body), handoff_body
    assert delivered_status == 200, delivered_body
    assert delivered_body["data"]["status"] == "delivered"
    assert handoff_status == 200, handoff_body
    pg_db.session.expire_all()
    assert pg_db.session.get(DriverBottleSession, trip_id).bottles_delivered == 2 + 1 + 3
    assert pg_db.session.get(Product, water_id).stock_quantity == 100 - 1 - 3
    assert pg_db.session.get(ProductTryout, tryout_id).handoff_completed_at is not None
    handoffs = TryoutBottleLedger.query.filter_by(
        tryout_id=tryout_id, event_type=TryoutBottleLedgerEventType.HANDOFF
    ).all()
    assert [entry.units for entry in handoffs] == [Decimal("1.00")]


def _paid_card_order(pg_db, customer, lines):
    """A card order paid online, waiting for the office to confirm it. `lines` is `[(product, quantity)]` in the order
    the customer added them."""
    order = order_with_lines(pg_db, customer, lines, status=OrderStatus.PENDING, payment_method=PaymentMethod.CARD)
    order.is_paid = True
    pg_db.session.commit()
    return order.id


def _held_in_the_stock_step(pg_app, pg_db, monkeypatch, *, first_tap, second_tap):
    """Run `first_tap`, hold it inside `InventoryService.confirm_reservations` right after it has locked the first
    product row it takes stock from (its first `ProductFiscalService.is_stock_derived`, asked of each row once it is
    locked), start `second_tap`, and prove the second waits on a lock the first holds. Returns each tap's
    `(status, body)`."""
    first_holds, release_the_first = threading.Event(), threading.Event()
    inside = threading.local()
    confirm, derived = InventoryService.confirm_reservations, ProductFiscalService.is_stock_derived

    def confirm_reservations(self, order_id):
        inside.confirming = True
        try:
            return confirm(self, order_id)
        finally:
            inside.confirming = False

    def is_stock_derived(product, **kwargs):
        result = derived(product, **kwargs)
        if threading.current_thread().name == "first" and getattr(inside, "confirming", False):
            if not first_holds.is_set():
                first_holds.set()
                assert release_the_first.wait(timeout=30), "the test never released the first tap"
        return result

    monkeypatch.setattr(InventoryService, "confirm_reservations", confirm_reservations)
    monkeypatch.setattr(ProductFiscalService, "is_stock_derived", staticmethod(is_stock_derived))
    answers, errors = {}, []
    first = _thread("first", pg_app, first_tap, answers, errors, finally_set=first_holds)
    second = _thread("second", pg_app, second_tap, answers, errors)
    first.start()
    try:
        assert first_holds.wait(timeout=30), "the first tap never reached its stock step"
        assert errors == [], errors
        second.start()
        assert _await_a_lock_waiter(pg_db), "the second tap did not wait: nothing holds the row it reads"
        assert second.is_alive(), "the second tap finished while the first one held the row"
    finally:
        release_the_first.set()
        _finish(first, *([second] if second.ident else []))
    assert errors == []
    return answers["first"], answers["second"]


def test_two_confirmations_taking_stock_of_the_same_two_products_never_deadlock(
    pg_app, pg_db, monkeypatch, enqueued  # noqa: F811 -- `enqueued` is the fixture
):
    """Two paid card orders hold the same two products, added in opposite order: Aziza's water then the 10 L, Bekzod's
    10 L then the water. Two office staff confirm them at once, and each confirmation takes the stock of its lines
    (`confirm_reservations`). It locked each line's product row as it reached the line, so one held the water and
    wanted the 10 L while the other held the 10 L and wanted the water (40P01). It now locks every row of the order in
    one statement, in product-id order, before it takes any stock. Held after Aziza's took its first row, Bekzod's must
    wait, and both must finish with the stock taken once per line."""
    current_app.config["REDIS_URL"] = os.environ["REDIS_URL"]  # the confirmation releases stock reservations in Redis
    admin = _user(pg_db, "+998901262209", role=UserRole.ADMIN, user_type=UserType.STAFF)
    water = _water(pg_db)
    small = ten_litre(pg_db, trip_category(pg_db))
    water_id, small_id = water.id, small.id
    assert water_id < small_id
    water_first = _paid_card_order(pg_db, _customer(pg_db, "+998901262201"), [(water, 2), (small, 3)])
    small_first = _paid_card_order(pg_db, _customer(pg_db, "+998901262202"), [(small, 1), (water, 4)])
    headers = _admin_bearer(admin)

    def confirms(order_id):
        """What Orders.js sends when the office picks Confirmed for the order."""
        return lambda client: client.put(
            ADMIN_ORDER_STATUS.format(order_id), json={"status": "confirmed"}, headers=headers
        )

    (first_status, first_body), (second_status, second_body) = _held_in_the_stock_step(
        pg_app, pg_db, monkeypatch, first_tap=confirms(water_first), second_tap=confirms(small_first)
    )

    assert "deadlock" not in str(first_body), first_body
    assert "deadlock" not in str(second_body), second_body
    assert first_status == 200, first_body
    assert second_status == 200, second_body
    pg_db.session.expire_all()
    assert [pg_db.session.get(Order, order_id).status for order_id in (water_first, small_first)] == [
        OrderStatus.CONFIRMED,
        OrderStatus.CONFIRMED,
    ]
    assert (
        pg_db.session.get(Product, water_id).stock_quantity,
        pg_db.session.get(Product, small_id).stock_quantity,
    ) == (
        100 - 2 - 4,
        1000 - 3 - 1,
    )


def _an_edit_commits_between_the_guard_and_the_claim(pg_app, monkeypatch, *, delivered_tap, edit_tap):
    """Start the driver's Delivered and hold it right after the staff path's progress guard (which read the order and
    its lines), before it claims the order; run the office's edit to its end meanwhile, then release the Delivered.
    The edit must not queue behind the held Delivered, and the Delivered must still be waiting when the edit commits.
    Returns `{"delivered": (status, body), "edit": (status, body)}`."""
    delivered_has_read = threading.Event()
    release_the_delivered = threading.Event()
    guard = BottleTrackingService.assert_driver_can_progress_delivery

    def held_after_the_guard(self, *args, **kwargs):
        result = guard(self, *args, **kwargs)
        if threading.current_thread().name == "delivered" and not delivered_has_read.is_set():
            delivered_has_read.set()
            assert release_the_delivered.wait(timeout=30), "the test never released the Delivered"
        return result

    monkeypatch.setattr(BottleTrackingService, "assert_driver_can_progress_delivery", held_after_the_guard)
    answers, errors = {}, []
    delivered = _thread("delivered", pg_app, delivered_tap, answers, errors, finally_set=delivered_has_read)
    edit = _thread("edit", pg_app, edit_tap, answers, errors)

    delivered.start()
    try:
        assert delivered_has_read.wait(timeout=30), "the Delivered never reached its progress guard"
        assert errors == [], errors
        edit.start()
        edit.join(timeout=15)
        assert not edit.is_alive(), "the edit queued behind the Delivered"
        assert delivered.is_alive(), "the Delivered finished before the edit committed"
    finally:
        release_the_delivered.set()
        _finish(delivered, *([edit] if edit.ident else []))

    assert errors == []
    return answers


def test_a_pre_delivery_edit_committed_before_the_claim_is_counted_at_its_new_quantity(
    pg_app, pg_db, monkeypatch, enqueued  # noqa: F811 -- `enqueued` is the fixture
):
    """Dilshod is at a cash door with 3 bottles and taps Delivered. The customer had phoned for 5, and the office's edit
    to 5 commits after the Delivered has read the order (its progress guard counted 3) but before it claims it. The
    Delivered must book 5: on the trip, on the customer's bottle balance and off the warehouse stock. He collects the
    edited total, 75 000, and the cash must settle the order at that total: the payment is 75 000, all of it collected,
    nothing outstanding and nothing left over as the customer's credit (settled against the 45 000 read before the
    claim, 30 000 of his cash became credit)."""
    admin = _user(pg_db, "+998901261069", role=UserRole.ADMIN, user_type=UserType.STAFF)
    driver, trip = _drivers_trip(pg_db, "+998901261061", delivered=0)
    trip_id = trip.id
    water = _water(pg_db)
    water_id = water.id
    cash_stop = _stop(
        pg_db,
        water,
        _customer(pg_db, "+998901261062"),
        bottles=3,
        status=DeliveryStatus.ARRIVED,
        driver=driver,
        trip=trip,
        payment_method=PaymentMethod.CASH,
    )
    cash_stop_id, cash_order_id = cash_stop.id, cash_stop.order_id
    cash_line_id = cash_stop.order.order_items[0].id
    customer_id = cash_stop.order.user_id

    answers = _an_edit_commits_between_the_guard_and_the_claim(
        pg_app,
        monkeypatch,
        delivered_tap=_cash_delivered(driver, cash_stop_id, 75000),
        edit_tap=_admin_edits(admin, cash_order_id, [(cash_line_id, water_id, 5)], "Customer phoned for 5"),
    )

    edit_status, edit_body = answers["edit"]
    assert edit_status == 200, edit_body
    delivered_status, delivered_body = answers["delivered"]
    assert delivered_status == 200, delivered_body
    assert delivered_body["data"]["status"] == "delivered"
    pg_db.session.expire_all()
    assert pg_db.session.get(DriverBottleSession, trip_id).bottles_delivered == 5
    booked = BottleLedger.query.filter_by(order_id=cash_order_id).all()
    assert [(entry.event_type, entry.quantity) for entry in booked] == [
        (BottleLedgerEventType.DELIVERY, Decimal("5.00"))
    ]
    assert pg_db.session.get(Product, water_id).stock_quantity == 100 - 5
    assert pg_db.session.get(Order, cash_order_id).total_amount == Decimal("75000.00")
    payment = Payment.query.filter_by(order_id=cash_order_id).one()
    assert (payment.amount, payment.amount_collected, payment.outstanding_amount) == (
        Decimal("75000.00"),
        Decimal("75000.00"),
        Decimal("0.00"),
    )
    assert CashCollectionService().get_customer_prepaid_balance(customer_id) == Decimal("0.00")


@pytest.mark.parametrize(
    "edit, total, bottles, small_units",
    [
        ("adds_a_line", Decimal("63000.00"), 3, 2),
        ("removes_a_line", Decimal("45000.00"), 3, 0),
        ("swaps_its_line", Decimal("36000.00"), 0, 4),
    ],
    ids=["adds_a_line", "removes_a_line", "swaps_its_line"],
)
def test_a_pre_delivery_edit_of_the_orders_lines_committed_before_the_claim_is_booked_as_edited(
    pg_app, pg_db, monkeypatch, enqueued, edit, total, bottles, small_units  # noqa: F811 -- `enqueued` is the fixture
):
    """Dilshod is at a cash door and taps Delivered. The customer had phoned, and the office's edit of the order's lines
    commits after the Delivered has read them (its progress guard) but before it claims the order:

    * adds_a_line: the order was 3 bottles of water; the customer wants 2 of the 10 L water as well (63 000).
    * removes_a_line: the order was 3 bottles of water and 2 of the 10 L; the customer drops the 10 L (45 000).
    * swaps_its_line: the order was 3 bottles of water; the customer wants 4 of the 10 L water instead (36 000).

    The Delivered must book the lines the order holds once it is claimed, not the ones its guard read: its bottles on
    the trip and on the customer's bottle balance, its 10 L on the trip's 10 L line, and the stock of exactly those
    lines. Booked from the lines read before the claim, an added line was missing from the trip and the stock, and a
    removed one was tallied on the trip and the balance, its stock taken (its product row locked after the trip row).
    He collects the edited total, and the cash settles the order at it."""
    admin = _user(pg_db, "+998901261068", role=UserRole.ADMIN, user_type=UserType.STAFF)
    driver, trip = _drivers_trip(pg_db, "+998901261066", delivered=0)
    trip_id = trip.id
    water = _water(pg_db)
    water_id = water.id
    small_id = ten_litre(pg_db, trip_category(pg_db)).id
    _ten_litre_line(pg_db, trip_id, small_id, delivered=0)
    cash_stop = _stop(
        pg_db,
        water,
        _customer(pg_db, "+998901261067"),
        bottles=3,
        status=DeliveryStatus.ARRIVED,
        driver=driver,
        trip=trip,
        payment_method=PaymentMethod.CASH,
    )
    cash_stop_id, cash_order_id = cash_stop.id, cash_stop.order_id
    water_line_id = cash_stop.order.order_items[0].id
    customer_id = cash_stop.order.user_id
    if edit == "adds_a_line":
        edited = [(water_line_id, water_id, 3), (None, small_id, 2)]
    elif edit == "swaps_its_line":
        edited = [(water_line_id, water_id, 0), (None, small_id, 4)]
    else:
        small_line = OrderItem(
            order_id=cash_order_id,
            product_id=small_id,
            quantity=2,
            unit_price=Decimal("9000.00"),
            total_price=Decimal("18000.00"),
        )
        pg_db.session.add(small_line)
        cash_stop.order.subtotal = cash_stop.order.total_amount = Decimal("63000.00")
        pg_db.session.commit()
        edited = [(water_line_id, water_id, 3), (small_line.id, small_id, 0)]

    answers = _an_edit_commits_between_the_guard_and_the_claim(
        pg_app,
        monkeypatch,
        delivered_tap=_cash_delivered(driver, cash_stop_id, int(total)),
        edit_tap=_admin_edits(admin, cash_order_id, edited, "Customer phoned"),
    )

    edit_status, edit_body = answers["edit"]
    assert edit_status == 200, edit_body
    delivered_status, delivered_body = answers["delivered"]
    assert delivered_status == 200, delivered_body
    assert delivered_body["data"]["status"] == "delivered"
    pg_db.session.expire_all()
    session = pg_db.session.get(DriverBottleSession, trip_id)
    assert (session.bottles_delivered, [(line.product_id, line.delivered) for line in session.unit_lines]) == (
        bottles,
        [(small_id, small_units)],
    )
    booked = BottleLedger.query.filter_by(order_id=cash_order_id).all()
    assert [(entry.event_type, entry.quantity) for entry in booked] == (
        [(BottleLedgerEventType.DELIVERY, Decimal(bottles))] if bottles else []
    )
    stock = (pg_db.session.get(Product, water_id).stock_quantity, pg_db.session.get(Product, small_id).stock_quantity)
    assert stock == (100 - bottles, 1000 - small_units)
    assert pg_db.session.get(Order, cash_order_id).total_amount == total
    payment = Payment.query.filter_by(order_id=cash_order_id).one()
    assert (payment.amount, payment.amount_collected, payment.outstanding_amount) == (total, total, Decimal("0.00"))
    assert CashCollectionService().get_customer_prepaid_balance(customer_id) == Decimal("0.00")


# --------------------------------------------------------------------------- #
# An office edit of a delivered order against the close of its trip
# --------------------------------------------------------------------------- #


def _delivered_on_the_trip(pg_db, driver, trip, product, phone, *, quantity=2):
    """A card order of `quantity` x `product` Dilshod delivered half an hour ago, bound to `trip`."""
    stop = _stop(
        pg_db,
        product,
        _customer(pg_db, phone),
        bottles=quantity,
        status=DeliveryStatus.DELIVERED,
        driver=driver,
        trip=trip,
        payment_method=PaymentMethod.CARD,
    )
    delivered_at = datetime.now(UTC) - timedelta(minutes=30)
    stop.delivered_at = delivered_at
    stop.actual_delivery_time = delivered_at
    stop.order.status = OrderStatus.DELIVERED
    pg_db.session.commit()
    return stop.order_id, stop.order.order_items[0].id


def _ten_litre_line(pg_db, trip_id, product_id, *, delivered):
    """The trip's 10 L line, 10 loaded, made an hour ago (before any order on the trip was delivered)."""
    pg_db.session.add(
        DriverBottleSessionUnit(
            session_id=trip_id,
            product_id=product_id,
            loaded=LOADED,
            delivered=delivered,
            created_at=datetime.now(UTC) - timedelta(hours=1),
        )
    )
    pg_db.session.commit()


@pytest.mark.parametrize("product", ["nineteen", "ten_litre"])
def test_an_edit_behind_the_drivers_close_reopens_the_trip_it_sealed(
    pg_app, pg_db, monkeypatch, enqueued, product  # noqa: F811 -- `enqueued` is the fixture
):
    """Dilshod delivered a card order of 2 on his trip and is closing it at the depot, every remaining one returned,
    while the office adds a 3rd to that order. The close holds the trip; the edit waits for it and must then see the
    trip closed: it reopens the trip (returns and discrepancies cleared) and adds its 1 there. Tallied on the sealed
    trip without a reopen, the delivered tally said 3 while the sealed return still said 2 were handed over.

    * nineteen: 10 x 18.9 L loaded, the order is 2 of them.
    * ten_litre: 10 x 18.9 L and 10 x 10 L loaded, the order is 2 of the 10 L."""
    admin = _user(pg_db, "+998901261079", role=UserRole.ADMIN, user_type=UserType.STAFF)
    driver, trip = _drivers_trip(pg_db, "+998901261071", delivered=2 if product == "nineteen" else 0)
    trip_id, driver_id = trip.id, driver.id
    goods = _water(pg_db) if product == "nineteen" else ten_litre(pg_db, trip_category(pg_db))
    goods_id = goods.id
    if product == "ten_litre":
        _ten_litre_line(pg_db, trip_id, goods_id, delivered=2)
    order_id, line_id = _delivered_on_the_trip(pg_db, driver, trip, goods, "+998901261072")
    close_body = {"bottles_returned_to_warehouse": LOADED - 2}
    if product == "ten_litre":
        close_body = {
            "bottles_returned_to_warehouse": LOADED,
            "units_returned": [{"product_id": goods_id, "quantity": LOADED - 2}],
        }
    driver_headers = _bearer(driver)

    def closes(client):
        """The bot's Close trip: the 18.9 L count and one return per line on the trip."""
        return client.post(CLOSE_SESSION, json=close_body, headers=driver_headers)

    # Held inside the close after it has locked the trip, before it seals anything.
    (close_status, close_body_out), (edit_status, edit_body) = _double_tap(
        pg_app,
        pg_db,
        monkeypatch,
        owner=BottleTrackingService,
        held_method="_release_open_bindings_for_session",
        first_tap=closes,
        second_tap=_admin_edits(admin, order_id, [(line_id, goods_id, 3)], "Customer took one more"),
    )

    assert close_status == 200, close_body_out
    assert close_body_out["data"]["status"] == "closed"
    assert edit_status == 200, edit_body
    assert edit_body["data"]["cascade_summary"]["session_reopened"] == {
        "bottle_session_id": trip_id,
        "bottle_driver_user_id": driver_id,
    }
    assert edit_body["data"]["cascade_summary"]["trip_effect"]["reopens"] is True
    pg_db.session.expire_all()
    session = pg_db.session.get(DriverBottleSession, trip_id)
    assert (session.status, session.reopen_count, session.bottles_returned_to_warehouse, session.discrepancy) == (
        DriverBottleSessionStatus.OPEN,
        1,
        None,
        None,
    )
    lines = [(line.product_id, line.delivered, line.returned, line.discrepancy) for line in session.unit_lines]
    if product == "nineteen":
        assert (session.bottles_delivered, lines) == (3, [])
    else:
        assert (session.bottles_delivered, lines) == (0, [(goods_id, 3, None, None)])


def test_a_close_behind_an_edit_seals_what_the_edit_added(
    pg_app, pg_db, monkeypatch, enqueued  # noqa: F811 -- `enqueued` is the fixture
):
    """The same two taps the other way round: the office's edit (a 3rd bottle on the delivered order) holds the trip
    when Dilshod closes it returning 8 of his 10. The close waits for the edit and seals the trip from the edited
    tally: 3 delivered, so 7 should have come back and 8 did (discrepancy -1)."""
    admin = _user(pg_db, "+998901261089", role=UserRole.ADMIN, user_type=UserType.STAFF)
    driver, trip = _drivers_trip(pg_db, "+998901261081", delivered=2)
    trip_id = trip.id
    water = _water(pg_db)
    water_id = water.id
    order_id, line_id = _delivered_on_the_trip(pg_db, driver, trip, water, "+998901261082")

    # Held right after the edit added its bottle to the trip, before it commits.
    (edit_status, edit_body), (close_status, close_body) = _double_tap(
        pg_app,
        pg_db,
        monkeypatch,
        owner=BottleTrackingService,
        held_method="add_to_session_tallies",
        first_tap=_admin_edits(admin, order_id, [(line_id, water_id, 3)], "Customer took one more"),
        second_tap=_closes(driver, LOADED - 2),
    )

    assert edit_status == 200, edit_body
    assert edit_body["data"]["cascade_summary"]["session_reopened"] == {}
    assert close_status == 200, close_body
    pg_db.session.expire_all()
    session = pg_db.session.get(DriverBottleSession, trip_id)
    assert (session.status, session.reopen_count, session.bottles_delivered, session.discrepancy) == (
        DriverBottleSessionStatus.CLOSED,
        0,
        3,
        -1,
    )


def test_two_edits_of_orders_on_one_closed_trip_reopen_it_once(
    pg_app, pg_db, monkeypatch, enqueued  # noqa: F811 -- `enqueued` is the fixture
):
    """Dilshod's trip is closed (10 loaded, 2 + 2 delivered on two card orders of two 18.9 L waters, 6 back). Two
    office staff each add a bottle to one of the orders at once. The first reopens the trip and holds it; the second
    (another customer, another product: nothing else to wait for) waits on the trip, then sees it open and only adds
    its bottle: no second reopen, and no refusal (it was answered 400 BOTTLE_SESSION_NOT_REOPENABLE, with no code, when
    the second decided on the status it read before the first committed)."""
    admin = _user(pg_db, "+998901261099", role=UserRole.ADMIN, user_type=UserType.STAFF)
    driver, trip = _drivers_trip(pg_db, "+998901261091", delivered=4)
    trip_id, driver_id = trip.id, driver.id
    water, other_water = _water(pg_db), _water(pg_db)
    water_id, other_water_id = water.id, other_water.id
    first_order, first_line = _delivered_on_the_trip(pg_db, driver, trip, water, "+998901261092")
    second_order, second_line = _delivered_on_the_trip(pg_db, driver, trip, other_water, "+998901261093")
    closed = pg_db.session.get(DriverBottleSession, trip_id)
    closed.status = DriverBottleSessionStatus.CLOSED
    closed.closed_at = datetime.now(UTC) - timedelta(minutes=10)
    closed.closed_by_user_id = driver_id
    closed.bottles_returned_to_warehouse = 6
    closed.discrepancy = 0
    pg_db.session.commit()

    # Held right after the first edit reopened the trip and added its bottle, before it commits.
    (first_status, first_body), (second_status, second_body) = _double_tap(
        pg_app,
        pg_db,
        monkeypatch,
        owner=BottleTrackingService,
        held_method="add_to_session_tallies",
        first_tap=_admin_edits(admin, first_order, [(first_line, water_id, 3)], "Customer took one more"),
        second_tap=_admin_edits(admin, second_order, [(second_line, other_water_id, 3)], "Customer took one more"),
    )

    assert first_status == 200, first_body
    assert first_body["data"]["cascade_summary"]["session_reopened"] == {
        "bottle_session_id": trip_id,
        "bottle_driver_user_id": driver_id,
    }
    assert second_status == 200, second_body
    assert second_body["data"]["cascade_summary"]["session_reopened"] == {}
    assert second_body["data"]["cascade_summary"]["trip_effect"]["reopens"] is False
    pg_db.session.expire_all()
    session = pg_db.session.get(DriverBottleSession, trip_id)
    assert (session.status, session.reopen_count, session.bottles_delivered, session.bottles_returned_to_warehouse) == (
        DriverBottleSessionStatus.OPEN,
        1,
        4 + 1 + 1,
        None,
    )


def _alone(pg_app, tap):
    """`tap` on a thread of its own (its own app context, session and connection), run to the end: setup through a
    real route. Returns its `(status, body)`."""
    answers, errors = {}, []
    alone = _thread("alone", pg_app, tap, answers, errors)
    alone.start()
    _finish(alone)
    assert errors == [], errors
    return answers["alone"]


def test_an_edit_and_a_delivered_at_one_place_on_one_trip_never_deadlock(
    pg_app, pg_db, monkeypatch, enqueued  # noqa: F811 -- `enqueued` is the fixture
):
    """Dilshod handed Aziza 2 bottles earlier on this trip (order A) and is at her door again with her second order
    (B, 3 bottles, not cash). The office adds a 3rd bottle to A at that moment. The edit books the change on her
    place's bottle balance, then on the trip; the Delivered books B on the same balance row, then on the trip. An edit
    that locked the trip before the balance row deadlocked with the Delivered (40P01). Holding the edit right after
    it locks the trip, the Delivered must wait on the balance row the edit already holds, and both must finish with
    every bottle counted once, on the trip and on her balance."""
    admin = _user(pg_db, "+998901262009", role=UserRole.ADMIN, user_type=UserType.STAFF)
    driver, trip = _drivers_trip(pg_db, "+998901262001", delivered=0)
    trip_id = trip.id
    water = _water(pg_db)
    water_id = water.id
    customer = _customer(pg_db, "+998901262002")
    earlier = _stop(pg_db, water, customer, bottles=2, status=DeliveryStatus.ARRIVED, driver=driver, trip=trip)
    earlier_id, first_order_id = earlier.id, earlier.order_id
    first_line_id, address_id = earlier.order.order_items[0].id, earlier.order.delivery_address_id
    now = _stop(pg_db, water, customer, bottles=3, status=DeliveryStatus.ARRIVED, driver=driver, trip=trip)
    now.order.delivery_address_id = address_id
    pg_db.session.commit()
    now_id, second_order_id = now.id, now.order_id
    status, body = _alone(pg_app, _taps(driver, earlier_id, "delivered", {"bottles_returned": 0}))
    assert status == 200, body

    # Held right after the edit's first trip lock, before it commits.
    (edit_status, edit_body), (delivered_status, delivered_body) = _double_tap(
        pg_app,
        pg_db,
        monkeypatch,
        owner=BottleTrackingService,
        held_method="_lock_session",
        first_tap=_admin_edits(admin, first_order_id, [(first_line_id, water_id, 3)], "Customer took one more"),
        second_tap=_taps(driver, now_id, "delivered", {"bottles_returned": 0}),
    )

    assert "deadlock" not in str(edit_body), edit_body
    assert "deadlock" not in str(delivered_body), delivered_body
    assert edit_status == 200, edit_body
    assert edit_body["data"]["cascade_summary"]["bottle"]["affected_session_id"] == trip_id
    assert delivered_status == 200, delivered_body
    assert delivered_body["data"]["status"] == "delivered"
    pg_db.session.expire_all()
    assert pg_db.session.get(DriverBottleSession, trip_id).bottles_delivered == 2 + 1 + 3
    booked = BottleLedger.query.filter(BottleLedger.order_id.in_([first_order_id, second_order_id])).all()
    assert sorted(
        ((entry.order_id, entry.event_type, entry.quantity) for entry in booked), key=lambda row: (row[0], row[2])
    ) == [
        (first_order_id, BottleLedgerEventType.ADMIN_ADJUSTMENT, Decimal("1.00")),
        (first_order_id, BottleLedgerEventType.DELIVERY, Decimal("2.00")),
        (second_order_id, BottleLedgerEventType.DELIVERY, Decimal("3.00")),
    ]
    assert BottleTrackingService.get_place_balance(address_id) == Decimal("6.00")


def _approves(owner, session_id, requester_id):
    """What the staff bot sends when the trip's owner taps Approve on a join request."""
    headers = _bearer(owner)
    return lambda client: client.post(
        APPROVE_JOIN, json={"session_id": session_id, "requester_id": requester_id}, headers=headers
    )


def _loads(driver, bottles):
    """The staff bot's Load: the one open call, 18.9 L only."""
    headers = _bearer(driver)
    return lambda client: client.post(OPEN_SESSION, json={"bottles_loaded": bottles}, headers=headers)


@pytest.mark.parametrize(
    "first, second",
    [("edit", "join"), ("join", "edit"), ("edit", "load"), ("load", "edit")],
    ids=["edit-then-join", "join-then-edit", "edit-then-load", "load-then-edit"],
)
def test_an_edit_reopening_a_trip_and_its_drivers_join_or_load_serialize(
    pg_app, pg_db, monkeypatch, enqueued, first, second  # noqa: F811 -- `enqueued` is the fixture
):
    """Kamol's trip is closed (10 loaded, a card order of 2 delivered on it, 8 back). The office adds a 3rd bottle to
    that order, which reopens his trip, while at the depot Olim approves Kamol's request to ride on Olim's trip, or
    Kamol loads a trip of his own. The open and the join serialize on Kamol's DeliveryPerson row; the edit locks that
    row too, before the order, so whichever comes second waits for the first and is refused for it. Read unlocked,
    both saw Kamol on no trip: he ended on his reopened trip and on Olim's at once, or the open met the reopened row
    at the one-open-trip index and answered 500."""
    admin = _user(pg_db, "+998901262019", role=UserRole.ADMIN, user_type=UserType.STAFF)
    owner = trip_driver(pg_db, "Olim", telegram_id="900000141")
    kamol = trip_driver(pg_db, "Kamol", telegram_id="900000142")
    kamol_id = kamol.id
    owners_trip_id = trip(pg_db, owner, bottles=40).id
    closed = trip(pg_db, kamol, bottles=LOADED, status=DriverBottleSessionStatus.CLOSED)
    closed.bottles_delivered = 2
    closed.bottles_returned_to_warehouse = LOADED - 2
    closed.discrepancy = 0
    pg_db.session.commit()
    closed_id = closed.id
    water = _water(pg_db)
    water_id = water.id
    order_id, line_id = _delivered_on_the_trip(pg_db, kamol, closed, water, "+998901262012")
    taps = {
        "edit": _admin_edits(admin, order_id, [(line_id, water_id, 3)], "Customer took one more"),
        "join": _approves(owner, owners_trip_id, kamol_id),
        "load": _loads(kamol, LOADED),
    }
    # Each side held where it has locked Kamol's DeliveryPerson row and decided, before it writes what it decided:
    # the edit right after it reopened his trip, the join after its rules passed, the open on its trips-table lock.
    held = {"edit": "reopen_session", "join": "_assert_can_join", "load": "lock_trip_table"}

    (first_status, first_body), (second_status, second_body) = _double_tap(
        pg_app,
        pg_db,
        monkeypatch,
        owner=BottleTrackingService,
        held_method=held[first],
        first_tap=taps[first],
        second_tap=taps[second],
    )
    answers = {first: (first_status, first_body), second: (second_status, second_body)}

    for status, body in answers.values():
        assert "deadlock" not in str(body), body
    pg_db.session.expire_all()
    his_trips = DriverBottleSession.query.filter_by(driver_user_id=kamol_id, status=DriverBottleSessionStatus.OPEN)
    his_memberships = DriverSessionMembership.query.filter_by(
        member_driver_id=kamol_id, status=DriverSessionMembershipStatus.ACTIVE
    )
    reopened = pg_db.session.get(DriverBottleSession, closed_id)
    edit_status, edit_body = answers["edit"]
    if first == "edit":
        assert edit_status == 200, edit_body
        assert edit_body["data"]["cascade_summary"]["session_reopened"] == {
            "bottle_session_id": closed_id,
            "bottle_driver_user_id": kamol_id,
        }
        assert (reopened.status, reopened.reopen_count, reopened.bottles_delivered) == (
            DriverBottleSessionStatus.OPEN,
            1,
            3,
        )
        assert [t.id for t in his_trips] == [closed_id]
        assert his_memberships.all() == []
        other_status, other_body = answers[second]
        assert other_status == 409, other_body
        assert other_body["error_code"] == (
            "BOTTLE_INVITEE_HAS_SESSION" if second == "join" else "BOTTLE_SESSION_ALREADY_OPEN"
        )
    else:
        assert edit_status == 409, edit_body
        assert edit_body["data"]["error_code"] == "BOTTLE_SESSION_ACTIVE_CONFLICT"
        assert (reopened.status, reopened.reopen_count, reopened.bottles_delivered) == (
            DriverBottleSessionStatus.CLOSED,
            0,
            2,
        )
        other_status, other_body = answers[first]
        if first == "join":
            assert other_status == 200, other_body
            assert [m.session_id for m in his_memberships] == [owners_trip_id]
            assert his_trips.all() == []
        else:
            assert other_status == 201, other_body
            assert [t.id for t in his_trips] == [other_body["data"]["id"]]
            assert his_memberships.all() == []


def _open_cod_debt(pg_db, driver, trip, product, customer, *, bottles, delivered_at):
    """A cash order of `bottles` x `product` Dilshod handed `customer` at `delivered_at` on `trip`, nothing paid yet:
    an open COD debt. Returns `(order id, its line id, its payment id)`."""
    stop = _stop(
        pg_db,
        product,
        customer,
        bottles=bottles,
        status=DeliveryStatus.DELIVERED,
        driver=driver,
        trip=trip,
        payment_method=PaymentMethod.CASH,
    )
    stop.delivered_at = delivered_at
    stop.actual_delivery_time = delivered_at
    order = stop.order
    order.status = OrderStatus.DELIVERED
    order.created_at = delivered_at - timedelta(hours=1)
    payment = Payment(
        order_id=order.id,
        user_id=customer.id,
        payment_method=PaymentMethod.CASH,
        amount=order.total_amount,
        amount_collected=Decimal("0.00"),
        outstanding_amount=order.total_amount,
        currency="UZS",
        status=PaymentStatus.PENDING,
        payment_id=f"PAY-LOCK-{order.id}",
    )
    pg_db.session.add(payment)
    pg_db.session.commit()
    return order.id, order.order_items[0].id, payment.id


def _collects_a_debt(driver, customer_id, amount):
    """What the staff bot sends when the driver records a customer's debt payment
    (`CashCollectionHandler.receive_collection_note`)."""
    body = {
        "customer_id": customer_id,
        "amount": amount,
        "source": "standalone_meeting",
        "notes": "Paid part of the old debt",
        "delivery_address_id": None,
        "proof_data": {"channel": "staff_bot", "flow": "standalone_cod_collection"},
    }
    headers = _bearer(driver)
    return lambda client: client.post(CASH_COLLECTIONS, json=body, headers=headers)


@pytest.mark.parametrize("other", ["collection", "cancel"])
def test_an_edit_of_a_delivered_cod_order_and_a_debt_payment_to_its_driver_never_deadlock(
    pg_app, pg_db, monkeypatch, enqueued, other  # noqa: F811 -- `enqueued` is the fixture
):
    """Dilshod delivered Aziza two cash orders on his trip and she owes both: Z (2 bottles, 30 000) and X (2 bottles,
    30 000, the newer). The office adds a 3rd bottle to X while, at the same moment:

    * collection: at her door, Dilshod records 10 000 of her debt in the staff bot. The collection locks every open
      debt of hers (Z and X, one id-ordered statement), pays Z (the oldest), then writes his DeliveryPerson row (cash
      collected);
    * cancel: at her request the office cancels her card-paid order Y (15 000), which Dilshod has accepted (a paid
      order is not hers to cancel in the bot). Its money becomes her credit and pays Z the same way, then the cancel
      writes his DeliveryPerson row (active deliveries).

    The edit locks Dilshod's DeliveryPerson row first, so his Load or join waits for its reopen check (the lock
    order's first rung), and writes X's payment last. Locked FOR UPDATE, that row conflicted with the plain UPDATE
    of his counters: the edit held it and waited for X's payment, the collection or cancel held X's payment and
    waited for the row, and Postgres aborted one of them (40P01). FOR KEY SHARE still queues his Load and join (FOR
    UPDATE) and never a counter write. Held right after the debt batch is locked, the edit must wait for X's payment,
    and both must finish with every figure booked."""
    admin = _user(pg_db, "+998901262109", role=UserRole.ADMIN, user_type=UserType.STAFF)
    driver, trip = _drivers_trip(pg_db, "+998901262101", delivered=4)
    driver_id, trip_id = driver.id, trip.id
    water = _water(pg_db)
    water_id = water.id
    customer = _customer(pg_db, "+998901262102")
    customer_id = customer.id
    delivered_at = datetime.now(UTC) - timedelta(minutes=30)
    _, _, older_payment_id = _open_cod_debt(
        pg_db, driver, trip, water, customer, bottles=2, delivered_at=delivered_at - timedelta(hours=2)
    )
    edited_order_id, edited_line_id, edited_payment_id = _open_cod_debt(
        pg_db, driver, trip, water, customer, bottles=2, delivered_at=delivered_at
    )
    if other == "collection":
        other_tap, paid = _collects_a_debt(driver, customer_id, 10000), Decimal("10000.00")
    else:
        card_stop = _stop(
            pg_db,
            _water(pg_db),
            customer,
            bottles=1,
            status=DeliveryStatus.ASSIGNED,
            driver=driver,
            payment_method=PaymentMethod.CARD,
        )
        card_order = card_stop.order
        card_order.is_paid = True
        pg_db.session.add(
            Payment(
                order_id=card_order.id,
                user_id=customer_id,
                payment_method=PaymentMethod.CARD,
                amount=Decimal("15000.00"),
                amount_collected=Decimal("15000.00"),
                outstanding_amount=Decimal("0.00"),
                currency="UZS",
                status=PaymentStatus.COMPLETED,
                payment_id=f"PAY-LOCK-{card_order.id}",
            )
        )
        DeliveryPerson.query.filter_by(user_id=driver_id).one().current_active_deliveries = 1  # Y, his one stop
        pg_db.session.commit()
        card_order_id = card_order.id
        admin_headers = _admin_bearer(admin)
        current_app.config["REDIS_URL"] = os.environ["REDIS_URL"]  # the cancel releases stock reservations in Redis

        def other_tap(client):
            """What Orders.js sends when the office picks Cancelled for the order, with the reason it asks for."""
            body = {"status": "cancelled", "reason": "Customer changed their mind"}
            return client.put(ADMIN_ORDER_STATUS.format(card_order_id), json=body, headers=admin_headers)

        paid = Decimal("15000.00")

    (other_status, other_body), (edit_status, edit_body) = _double_tap(
        pg_app,
        pg_db,
        monkeypatch,
        owner=CashCollectionService,
        held_method="_allocate_scoped",
        first_tap=other_tap,
        second_tap=_admin_edits(admin, edited_order_id, [(edited_line_id, water_id, 3)], "Customer took one more"),
    )

    assert "deadlock" not in str(other_body), other_body
    assert "deadlock" not in str(edit_body), edit_body
    assert other_status == (201 if other == "collection" else 200), other_body
    assert edit_status == 200, edit_body
    pg_db.session.expire_all()
    older, edited = pg_db.session.get(Payment, older_payment_id), pg_db.session.get(Payment, edited_payment_id)
    assert (older.amount_collected, older.outstanding_amount) == (paid, Decimal("30000.00") - paid)
    assert (edited.amount, edited.amount_collected, edited.outstanding_amount) == (
        Decimal("45000.00"),
        Decimal("0.00"),
        Decimal("45000.00"),
    )
    assert pg_db.session.get(DriverBottleSession, trip_id).bottles_delivered == 4 + 1
    if other == "collection":
        assert DeliveryPerson.query.filter_by(user_id=driver_id).one().total_cash_collected == paid
    else:
        assert pg_db.session.get(Order, card_order_id).status == OrderStatus.CANCELLED
        assert DeliveryPerson.query.filter_by(user_id=driver_id).one().current_active_deliveries == 0


@pytest.mark.parametrize("collected", [10000, 30000], ids=["part", "all"])
def test_an_edit_that_waited_for_a_debt_payment_to_its_order_keeps_what_was_paid(
    pg_app, pg_db, monkeypatch, enqueued, collected  # noqa: F811 -- `enqueued` is the fixture
):
    """Dilshod delivered Aziza a cash order X on his trip: 2 bottles, 30 000, nothing paid yet, her only debt. At her
    door he records `collected` of it in the staff bot (part of it, or all of it), and while that payment is being
    booked the office adds a 3rd bottle to X (45 000).

    The payment's allocation to X takes the key-share lock on X's order row (its foreign key), so the edit's lock on
    the order waits for the payment to commit. The edit read X's payment in that same statement, as it was before the
    commit, and priced the 45 000 as if nothing had been paid: it stored 45 000 outstanding (part), or 45 000
    outstanding on a payment still marked paid in full (all). The edit now re-reads the payment under its row lock,
    where it writes it: X is 45 000, what Dilshod collected stays collected, and the rest is owed."""
    admin = _user(pg_db, "+998901262509", role=UserRole.ADMIN, user_type=UserType.STAFF)
    driver, trip = _drivers_trip(pg_db, "+998901262501", delivered=2)
    driver_id, trip_id = driver.id, trip.id
    water = _water(pg_db)
    water_id = water.id
    customer = _customer(pg_db, "+998901262502")
    customer_id = customer.id
    order_id, line_id, payment_id = _open_cod_debt(
        pg_db, driver, trip, water, customer, bottles=2, delivered_at=datetime.now(UTC) - timedelta(minutes=30)
    )

    # Held once the payment's allocation to X is written (the query right after it flushes it), before it commits.
    (collection_status, collection_body), (edit_status, edit_body) = _double_tap(
        pg_app,
        pg_db,
        monkeypatch,
        owner=CashCollectionService,
        held_method="_payments_allocated_by_event",
        first_tap=_collects_a_debt(driver, customer_id, collected),
        second_tap=_admin_edits(admin, order_id, [(line_id, water_id, 3)], "Customer took one more"),
    )

    assert collection_status == 201, collection_body
    assert edit_status == 200, edit_body
    pg_db.session.expire_all()
    paid = Decimal(collected)
    payment = pg_db.session.get(Payment, payment_id)
    assert (payment.amount, payment.amount_collected, payment.outstanding_amount, payment.status) == (
        Decimal("45000.00"),
        paid,
        Decimal("45000.00") - paid,
        PaymentStatus.PARTIALLY_PAID,
    )
    order = pg_db.session.get(Order, order_id)
    assert (order.total_amount, order.is_paid) == (Decimal("45000.00"), False)
    assert DeliveryPerson.query.filter_by(user_id=driver_id).one().total_cash_collected == paid
    assert pg_db.session.get(DriverBottleSession, trip_id).bottles_delivered == 2 + 1


def test_a_first_timers_loyalty_page_is_not_held_up_by_an_edit_of_his_order(
    pg_app, pg_db, monkeypatch, enqueued  # noqa: F811 -- `enqueued` is the fixture
):
    """Aziza has no loyalty account yet. The office adds a bottle to her confirmed cash order, and while the edit runs
    she opens her AquaCoins in the bot, which creates her account. The edit's points estimate created it too, flushed
    inside the edit's transaction, so her unique user id was held until the edit committed: her page waited for the
    edit, then failed on the duplicate. The estimate now creates nothing. Held right before it commits, the edit must
    not hold up her page, and both must finish with one account."""
    admin = _user(pg_db, "+998901262309", role=UserRole.ADMIN, user_type=UserType.STAFF)
    program = LoyaltyProgram(name="Default", is_active=True, is_default=True, uzs_per_point=250)
    pg_db.session.add(program)
    pg_db.session.commit()
    water = _water(pg_db)
    water_id = water.id
    customer = _customer(pg_db, "+998901262302")
    customer_id = customer.id
    order = order_with_lines(pg_db, customer, [(water, 2)], status=OrderStatus.CONFIRMED)
    order_id, line_id = order.id, order.order_items[0].id
    customer_headers = _bearer(customer)
    edit_holds, release_the_edit = threading.Event(), threading.Event()
    recompute = OrderEditService._recompute_totals

    def held_before_the_commit(self, *args, **kwargs):
        result = recompute(self, *args, **kwargs)
        if threading.current_thread().name == "edit" and not edit_holds.is_set():
            edit_holds.set()
            assert release_the_edit.wait(timeout=30), "the test never released the edit"
        return result

    def opens_her_aquacoins(client):
        """What the customer bot asks for its AquaCoins screen (`get_loyalty_points`)."""
        return client.get(LOYALTY_POINTS, headers=customer_headers)

    monkeypatch.setattr(OrderEditService, "_recompute_totals", held_before_the_commit)
    answers, errors = {}, []
    edit = _thread(
        "edit",
        pg_app,
        _admin_edits(admin, order_id, [(line_id, water_id, 3)], "Customer took one more"),
        answers,
        errors,
        finally_set=edit_holds,
    )
    page = _thread("page", pg_app, opens_her_aquacoins, answers, errors)
    edit.start()
    try:
        assert edit_holds.wait(timeout=30), "the edit never reached its last step"
        assert errors == [], errors
        page.start()
        page.join(timeout=15)
        assert not page.is_alive(), "her AquaCoins page queued behind the edit"
        assert edit.is_alive(), "the edit finished before her page was answered"
    finally:
        release_the_edit.set()
        _finish(edit, *([page] if page.ident else []))

    assert errors == []
    page_status, page_body = answers["page"]
    edit_status, edit_body = answers["edit"]
    assert page_status == 200, page_body
    assert page_body["data"]["points_balance"] == 0
    assert edit_status == 200, edit_body
    pg_db.session.expire_all()
    assert [account.user_id for account in LoyaltyPoints.query.filter_by(user_id=customer_id)] == [customer_id]
    assert [item.quantity for item in pg_db.session.get(Order, order_id).order_items] == [3]


def _first_timers_delivered_card_order(pg_db, phone):
    """Aziza, registered by the office, has no loyalty account: her delivered order of 4 x 10 L (36 000 so'm), paid by
    card, earned nothing. The program pays 1 AquaCoin per 250 so'm. Returns her id, the order's id, its line's id and
    the 10 L product's id."""
    pg_db.session.add(LoyaltyProgram(name="Default", is_active=True, is_default=True, uzs_per_point=250))
    pg_db.session.commit()
    unit = ten_litre(pg_db, trip_category(pg_db))
    customer = _customer(pg_db, phone)
    order = order_with_lines(
        pg_db, customer, [(unit, 4)], status=OrderStatus.DELIVERED, payment_method=PaymentMethod.CARD
    )
    order.is_paid = True
    pg_db.session.add(
        Payment(
            order_id=order.id,
            user_id=customer.id,
            payment_method=PaymentMethod.CARD,
            amount=Decimal("36000.00"),
            amount_collected=Decimal("36000.00"),
            outstanding_amount=Decimal("0.00"),
            currency="UZS",
            status=PaymentStatus.COMPLETED,
            payment_id=f"PAY-LOCK-{order.id}",
        )
    )
    pg_db.session.commit()
    assert LoyaltyPoints.query.filter_by(user_id=customer.id).count() == 0
    return customer.id, order.id, order.order_items[0].id, unit.id


def test_an_edit_of_a_first_timers_delivered_order_that_fails_late_leaves_nothing(
    pg_app, pg_db, monkeypatch, enqueued  # noqa: F811 -- `enqueued` is the fixture
):
    """The office lowers Aziza's order from 4 to 3 and the edit fails at its last step, after its AquaCoins step
    created her account. Nothing of it may stay: the line at 4, the total at 36 000, the stock as it was, no refund
    credit, no account, no history row. The AquaCoins step committed the first half (the line, the stock, the refund
    credit, the account) and only the rest rolled back."""
    admin = _user(pg_db, "+998901262411", role=UserRole.ADMIN, user_type=UserType.STAFF)
    customer_id, order_id, line_id, unit_id = _first_timers_delivered_card_order(pg_db, "+998901262412")
    recompute_totals = OrderEditService._recompute_totals

    def fails_after_the_totals(self, *args, **kwargs):
        recompute_totals(self, *args, **kwargs)
        raise RuntimeError("synthetic failure after the AquaCoins step")

    monkeypatch.setattr(OrderEditService, "_recompute_totals", fails_after_the_totals)

    status, body = _alone(pg_app, _admin_edits(admin, order_id, [(line_id, unit_id, 3)], "Customer kept 3 of 4"))

    assert status == 500, body
    pg_db.session.expire_all()
    order = pg_db.session.get(Order, order_id)
    assert [item.quantity for item in order.order_items] == [4]
    assert (Decimal(str(order.total_amount)), order.loyalty_points_earned or 0) == (Decimal("36000.00"), 0)
    assert pg_db.session.get(Product, unit_id).stock_quantity == 1000
    assert CashCollectionEvent.query.filter_by(order_id=order_id).count() == 0
    assert LoyaltyPoints.query.filter_by(user_id=customer_id).count() == 0
    assert OrderEditHistory.query.filter_by(order_id=order_id).count() == 0


def test_a_second_edit_of_a_first_timers_delivered_order_waits_for_the_whole_first_one(
    pg_app, pg_db, monkeypatch, enqueued  # noqa: F811 -- `enqueued` is the fixture
):
    """Aziza was registered by the office and has no loyalty account: her delivered order of 4 x 10 L (36 000 so'm),
    paid by card, earned nothing. Two office users lower it at once, Malika from 4 to 3 and Bobur from 3 to 1.

    Malika's edit re-quotes the order's AquaCoins, which creates Aziza's account, and that creation committed in the
    middle of the edit: Bobur's edit, queued on the order row, took it while its stored total was still 36 000,
    refunded 27 000 against it, and met the rest of Malika's edit at the order and account rows (40P01, or Malika's
    totals for 3 overwriting Bobur's for 1). Held right before its AquaCoins step, Malika's edit holds the order and
    Bobur waits. Then Malika's edit must finish whole before Bobur's starts on what she left: 3 to 1 is a refund of
    18 000 and 108 - 36 = 72 AquaCoins back."""
    malika = _user(pg_db, "+998901262401", role=UserRole.ADMIN, user_type=UserType.STAFF)
    bobur = _user(pg_db, "+998901262402", role=UserRole.ADMIN, user_type=UserType.STAFF)
    customer_id, order_id, line_id, unit_id = _first_timers_delivered_card_order(pg_db, "+998901262403")

    # Held right after the bottle step, the last one before the AquaCoins step (`_cascade_loyalty`).
    (first_status, first_body), (second_status, second_body) = _double_tap(
        pg_app,
        pg_db,
        monkeypatch,
        owner=OrderEditService,
        held_method="_cascade_bottle",
        first_tap=_admin_edits(malika, order_id, [(line_id, unit_id, 3)], "Customer kept 3 of 4"),
        second_tap=_admin_edits(bobur, order_id, [(line_id, unit_id, 1)], "Customer kept 1"),
    )

    assert first_status == 200, first_body
    assert second_status == 200, second_body
    assert first_body["data"]["cascade_summary"]["cash"]["amount"] == 9000.0
    assert second_body["data"]["cascade_summary"]["cash"]["amount"] == 18000.0
    pg_db.session.expire_all()
    edited = pg_db.session.get(Order, order_id)
    assert [item.quantity for item in edited.order_items] == [1]
    assert (Decimal(str(edited.total_amount)), edited.loyalty_points_earned) == (Decimal("9000.00"), 36)
    refunds = CashCollectionEvent.query.filter_by(order_id=order_id).order_by(CashCollectionEvent.id)
    assert [Decimal(str(event.amount)) for event in refunds] == [Decimal("9000.00"), Decimal("18000.00")]
    assert OrderEditHistory.query.filter_by(order_id=order_id).count() == 2
    accounts = LoyaltyPoints.query.filter_by(user_id=customer_id).all()
    assert [(account.current_balance, account.total_earned) for account in accounts] == [(36, 36)]


@pytest.mark.parametrize("first", ["close", "handoff"])
def test_a_tryout_handoff_and_the_close_of_its_trip_never_change_what_the_close_sealed(
    pg_app, pg_db, monkeypatch, enqueued, first  # noqa: F811 -- `enqueued` is the fixture
):
    """Owner and co-driver share one trip (10 x 18.9 L, 10 x 10 L). The co-driver completes a try-out hand-off of
    2 x 10 L (it tallies on the trip he rides on) while the owner closes the trip at the depot.

    * close first: the close holds the trip and seals it, every 10 L returned; the hand-off waits, then finds the
      trip closed and tallies nothing on it. Tallied on the sealed line, the 10 L delivered changed after the seal
      and its discrepancy no longer matched it.
    * hand-off first: the hand-off holds the trip with its 2 x 10 L added; the close waits for it and seals the line
      from what the hand-off committed: 8 back is no discrepancy. Without the hand-off's lock on the trip row, the
      close sealed the line from the delivered it read before the hand-off committed."""
    crew = _crew(pg_db)
    trip_id, owner, co_driver = crew.trip.id, crew.owner, crew.co_driver
    small = ten_litre(pg_db, trip_category(pg_db))
    small_id = small.id
    _ten_litre_line(pg_db, trip_id, small_id, delivered=0)
    admin = _user(pg_db, "+998901262029", role=UserRole.ADMIN, user_type=UserType.STAFF)
    tryout = TryoutService.create_tryout(
        {
            "trial_contact": {"first_name": "Trial", "phone": "+998901262023", "preferred_language": "uz"},
            "address": {
                "full_address": "5 Lock Street, Tashkent",
                "city": "Tashkent",
                "latitude": 41.3115,
                "longitude": 69.2801,
                "is_default": True,
            },
            "items": [{"product_id": small_id, "quantity": 2}],
            "assigned_driver_user_id": co_driver.id,
        },
        admin.id,
        source="admin",
    )
    (handoff_task_id,) = [task.id for task in tryout.tasks if task.task_type is TryoutTaskType.HANDOFF]
    co_driver_headers, owner_headers = _bearer(co_driver), _bearer(owner)
    returned = LOADED if first == "close" else LOADED - 2

    def hands_over(client):
        """What the bot sends for Handed over (`StaffAPIClient.complete_tryout_handoff`)."""
        return client.post(HANDOFF.format(handoff_task_id), json={}, headers=co_driver_headers)

    def closes(client):
        """The owner's Close trip: the 18.9 L count and one return per line on the trip."""
        body = {
            "bottles_returned_to_warehouse": LOADED,
            "units_returned": [{"product_id": small_id, "quantity": returned}],
        }
        return client.post(CLOSE_SESSION, json=body, headers=owner_headers)

    if first == "close":
        # Held inside the close after it has locked the trip, before it seals anything.
        (close_status, close_body), (handoff_status, handoff_body) = _double_tap(
            pg_app,
            pg_db,
            monkeypatch,
            owner=BottleTrackingService,
            held_method="_release_open_bindings_for_session",
            first_tap=closes,
            second_tap=hands_over,
        )
    else:
        # Held right after the hand-off added its 2 x 10 L to the trip, before it commits.
        (handoff_status, handoff_body), (close_status, close_body) = _double_tap(
            pg_app,
            pg_db,
            monkeypatch,
            owner=BottleTrackingService,
            held_method="add_to_session_tallies",
            first_tap=hands_over,
            second_tap=closes,
        )

    assert close_status == 200, close_body
    assert close_body["data"]["status"] == "closed"
    assert handoff_status == 200, handoff_body
    pg_db.session.expire_all()
    session = pg_db.session.get(DriverBottleSession, trip_id)
    assert (session.status, session.bottles_delivered, session.discrepancy) == (DriverBottleSessionStatus.CLOSED, 0, 0)
    lines = [(line.product_id, line.delivered, line.returned, line.discrepancy) for line in session.unit_lines]
    if first == "close":
        assert lines == [(small_id, 0, LOADED, 0)]
    else:
        assert lines == [(small_id, 2, LOADED - 2, 0)]
    assert pg_db.session.get(Product, small_id).stock_quantity == 1000 - 2


def test_the_driver_and_the_office_completing_one_handoff_book_it_once(
    pg_app, pg_db, monkeypatch, enqueued  # noqa: F811 -- `enqueued` is the fixture
):
    """Dilshod taps Handed over on a try-out of 2 x 10 L while the office, on the Try-outs page, completes the same
    hand-off. The 10 L is not returnable, so no ledger row's key stops a second booking: both completions read the
    task open, and the stock went down twice and the trip took the 10 L twice. The completions now queue on the
    try-out's row and the later one finds the hand-off done: the stock goes down once, the trip of the driver who
    handed it over takes it once, and both answer 200."""
    admin = _user(pg_db, "+998901262039", role=UserRole.ADMIN, user_type=UserType.STAFF)
    driver, trip = _drivers_trip(pg_db, "+998901262031", delivered=0)
    trip_id, driver_id = trip.id, driver.id
    small = ten_litre(pg_db, trip_category(pg_db))
    small_id = small.id
    _ten_litre_line(pg_db, trip_id, small_id, delivered=0)
    tryout = TryoutService.create_tryout(
        {
            "trial_contact": {"first_name": "Trial", "phone": "+998901262033", "preferred_language": "uz"},
            "address": {
                "full_address": "7 Lock Street, Tashkent",
                "city": "Tashkent",
                "latitude": 41.3117,
                "longitude": 69.2803,
                "is_default": True,
            },
            "items": [{"product_id": small_id, "quantity": 2}],
            "assigned_driver_user_id": driver_id,
        },
        admin.id,
        source="admin",
    )
    tryout_id = tryout.id
    (handoff_task_id,) = [task.id for task in tryout.tasks if task.task_type is TryoutTaskType.HANDOFF]
    driver_headers, admin_headers = _bearer(driver), _admin_bearer(admin)

    def driver_hands_over(client):
        """What the bot sends for Handed over (`StaffAPIClient.complete_tryout_handoff`)."""
        return client.post(HANDOFF.format(handoff_task_id), json={}, headers=driver_headers)

    def office_completes(client):
        """What the admin Try-outs page sends for Complete on the hand-off task."""
        return client.post(ADMIN_COMPLETE_TASK.format(handoff_task_id), json={}, headers=admin_headers)

    # Held right after the driver's completion locked the product rows, before it takes the stock.
    (driver_status, driver_body), (office_status, office_body) = _double_tap(
        pg_app,
        pg_db,
        monkeypatch,
        owner=InventoryService,
        held_method="lock_product_rows",
        first_tap=driver_hands_over,
        second_tap=office_completes,
    )

    assert driver_status == 200, driver_body
    assert office_status == 200, office_body
    pg_db.session.expire_all()
    assert pg_db.session.get(Product, small_id).stock_quantity == 1000 - 2
    session = pg_db.session.get(DriverBottleSession, trip_id)
    assert [(line.product_id, line.delivered) for line in session.unit_lines] == [(small_id, 2)]
    assert pg_db.session.get(ProductTryout, tryout_id).handoff_completed_at is not None
    task = pg_db.session.get(TryoutTask, handoff_task_id)
    assert (task.status, task.completed_by_user_id) == (TryoutTaskStatus.COMPLETED, driver_id)


# --------------------------------------------------------------------------- #
# The product's trip setting against a trip open (spec 2026-11-18 §6.1, §6.2)
# --------------------------------------------------------------------------- #

ADMIN_PRODUCT = "/api/v1/admin/products/{}"


def _held_at_the_trips_table_lock(pg_app, pg_db, monkeypatch, *, held_mode, first_tap, second_tap):
    """Run `first_tap`, hold it right after its `lock_trip_table(held_mode)`, start `second_tap`, and prove the second
    waits on the table lock the first holds. `lock_trip_table` is a staticmethod, so the hold is installed as one (the
    product save calls it on the class, the open on the instance). Returns each tap's `(status, body)`."""
    first_holds = threading.Event()
    release_the_first = threading.Event()
    original = BottleTrackingService.lock_trip_table

    def held(mode):
        original(mode)
        if threading.current_thread().name == "first" and mode == held_mode and not first_holds.is_set():
            first_holds.set()
            assert release_the_first.wait(timeout=30), "the test never released the first tap"

    monkeypatch.setattr(BottleTrackingService, "lock_trip_table", staticmethod(held))
    answers, errors = {}, []
    first = _thread("first", pg_app, first_tap, answers, errors, finally_set=first_holds)
    second = _thread("second", pg_app, second_tap, answers, errors)

    first.start()
    try:
        assert first_holds.wait(timeout=30), "the first tap never reached its trips-table lock"
        assert errors == [], errors
        second.start()
        assert _await_a_lock_waiter(pg_db), "the second tap did not wait on the trips table"
        assert second.is_alive(), "the second tap finished while the first one held the trips table"
    finally:
        release_the_first.set()
        _finish(first, *([second] if second.ident else []))

    assert errors == []
    return answers["first"], answers["second"]


@pytest.mark.parametrize(
    "first, counted_before, tick",
    [("save", False, True), ("open", False, True), ("open", True, False)],
    ids=["save-first-tick", "open-first-tick", "open-first-untick"],
)
def test_a_setting_change_and_a_trip_open_serialize(
    pg_app, pg_db, monkeypatch, enqueued, first, counted_before, tick  # noqa: F811 -- `enqueued` is the fixture
):
    """Dilshod opens his trip at the depot (10 x 18.9 L, plus 4 x 10 L when the Load screen offered it) while an admin
    ticks or unticks 10 L's "counted on the bottle trip" on the Products page. The save holds the trips table in SHARE
    mode until it commits; the open takes it in ROW EXCLUSIVE mode before it reads the setting. Whichever is first, the
    other waits for it and reads what it committed: a save first gives the new trip a line for the newly counted
    product; an open first leaves a trip open, so the save is refused with that trip named. Neither deadlocks."""
    admin = _user(pg_db, "+998901261079", role=UserRole.ADMIN, user_type=UserType.STAFF)
    driver = trip_driver(pg_db, "Dilshod", telegram_id="900000071")
    driver_id = driver.id
    product = ten_litre(pg_db, trip_category(pg_db), counted=counted_before)
    product_id = product.id
    load = {"bottles_loaded": LOADED}
    if counted_before:
        load["units"] = [{"product_id": product_id, "quantity": 4}]

    def opens(client):
        """What the staff bot sends for its Load (`StaffAPIClient.open_bottle_session`)."""
        return client.post(OPEN_SESSION, json=load, headers=_bearer(driver))

    def saves(client):
        """What Products.js sends when the admin saves the ticked or unticked box."""
        return client.put(
            ADMIN_PRODUCT.format(product_id), json={"counted_on_bottle_trip": tick}, headers=_admin_bearer(admin)
        )

    (first_status, first_body), (second_status, second_body) = _held_at_the_trips_table_lock(
        pg_app,
        pg_db,
        monkeypatch,
        held_mode="SHARE" if first == "save" else "ROW EXCLUSIVE",
        first_tap=saves if first == "save" else opens,
        second_tap=opens if first == "save" else saves,
    )
    (open_status, open_body), (save_status, save_body) = (
        ((second_status, second_body), (first_status, first_body))
        if first == "save"
        else ((first_status, first_body), (second_status, second_body))
    )

    assert "deadlock" not in str(open_body), open_body
    assert "deadlock" not in str(save_body), save_body
    assert open_status == 201, open_body
    pg_db.session.expire_all()
    (opened,) = DriverBottleSession.query.filter_by(driver_user_id=driver_id).all()
    assert open_body["data"]["id"] == opened.id
    if first == "save":
        assert save_status == 200, save_body
        assert save_body["data"]["product"]["counted_on_bottle_trip"] is True
        assert [(u["product_id"], u["loaded"]) for u in open_body["data"]["units"]] == [(product_id, 0)]
    else:
        assert save_status == 409, save_body
        assert save_body["error_code"] == "TRIP_UNIT_SETTING_BLOCKED"
        assert save_body["details"]["open_trips"] == [
            {
                "session_id": opened.id,
                "session_ref": opened.session_ref,
                "driver_user_id": driver_id,
                "driver_name": "Dilshod",
            }
        ]
        expected_units = [(product_id, 4)] if counted_before else []
        assert [(u["product_id"], u["loaded"]) for u in open_body["data"]["units"]] == expected_units
        assert pg_db.session.get(Product, product_id).counted_on_bottle_trip is counted_before


CONFIRM = TRANSFERS + "/{}/confirm"


@pytest.mark.parametrize(
    "first, counted_before, tick",
    [("save", False, True), ("count", False, True), ("count", True, False)],
    ids=["save-first-tick", "count-first-tick", "count-first-untick"],
)
def test_a_setting_change_and_a_transfer_opened_trip_serialize(
    pg_app, pg_db, monkeypatch, enqueued, first, counted_before, tick  # noqa: F811 -- `enqueued` is the fixture
):
    """Aziz handed Bobur 5 x 18.9 L (and 3 x 10 L when his trip counted it) and closed his trip at the depot. Bobur,
    with nothing open, taps Confirm while an admin ticks or unticks 10 L's "counted on the bottle trip". The count
    opens Bobur a trip, and like every trip open it takes the trips table in ROW EXCLUSIVE mode before it reads the
    setting; the save holds the table in SHARE mode until it commits. Whichever is first, the other waits for it and
    reads what it committed: a save first gives the new trip a line for the newly counted product; a count first
    leaves a trip open, so the save is refused with that trip named, and the trip keeps the line the hand-off carried.
    Neither deadlocks."""
    admin = _user(pg_db, "+998901261089", role=UserRole.ADMIN, user_type=UserType.STAFF)
    sender = trip_driver(pg_db, "Aziz", telegram_id="900000081")
    receiver = trip_driver(pg_db, "Bobur", telegram_id="900000082")
    sender_id, receiver_id = sender.id, receiver.id
    product_id = ten_litre(pg_db, trip_category(pg_db), counted=counted_before).id
    carried = {product_id: 3} if counted_before else {}
    trip(pg_db, sender, bottles=5, units=carried)
    service = BottleTrackingService()
    transfer_id = service.initiate_bottle_transfer(sender_id, receiver_id, full_quantity=5, units=carried).id
    service.close_bottle_session(sender_id, 0, units_returned={pid: 0 for pid in carried})
    pg_db.session.commit()
    receiver_headers, admin_headers = _bearer(receiver), _admin_bearer(admin)

    def counts(client):
        """The receiver's one-tap Confirm, as the staff bot sends it."""
        return client.post(CONFIRM.format(transfer_id), json={"confirmAsDeclared": True}, headers=receiver_headers)

    def saves(client):
        """What Products.js sends when the admin saves the ticked or unticked box."""
        return client.put(
            ADMIN_PRODUCT.format(product_id), json={"counted_on_bottle_trip": tick}, headers=admin_headers
        )

    (first_status, first_body), (second_status, second_body) = _held_at_the_trips_table_lock(
        pg_app,
        pg_db,
        monkeypatch,
        held_mode="SHARE" if first == "save" else "ROW EXCLUSIVE",
        first_tap=saves if first == "save" else counts,
        second_tap=counts if first == "save" else saves,
    )
    (count_status, count_body), (save_status, save_body) = (
        ((second_status, second_body), (first_status, first_body))
        if first == "save"
        else ((first_status, first_body), (second_status, second_body))
    )

    assert "deadlock" not in str(count_body), count_body
    assert "deadlock" not in str(save_body), save_body
    assert count_status == 200, count_body
    pg_db.session.expire_all()
    (opened,) = DriverBottleSession.query.filter_by(driver_user_id=receiver_id).all()
    assert (count_body["data"]["status"], count_body["data"]["receiver_session_id"]) == ("confirmed", opened.id)
    assert (opened.status, opened.bottles_loaded, opened.bottles_transferred_in) == (
        DriverBottleSessionStatus.OPEN,
        0,
        5,
    )
    lines = [(line.product_id, line.loaded, line.transferred_in) for line in opened.unit_lines]
    if first == "save":
        assert save_status == 200, save_body
        assert save_body["data"]["product"]["counted_on_bottle_trip"] is True
        assert lines == [(product_id, 0, 0)]
    else:
        assert save_status == 409, save_body
        assert save_body["error_code"] == "TRIP_UNIT_SETTING_BLOCKED"
        assert save_body["details"]["open_trips"] == [
            {
                "session_id": opened.id,
                "session_ref": opened.session_ref,
                "driver_user_id": receiver_id,
                "driver_name": "Bobur",
            }
        ]
        assert lines == ([(product_id, 0, 3)] if counted_before else [])
        assert pg_db.session.get(Product, product_id).counted_on_bottle_trip is counted_before
