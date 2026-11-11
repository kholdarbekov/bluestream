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

`with_for_update` compiles to nothing on SQLite, so this runs on a real, fully-migrated Postgres database (`pg_app`).
The interleaving is forced the way test_bottle_transfer_races_pg_e2e.py forces its own (`_double_tap`): the first
request is held right after the locks it takes, before it commits, and the second starts only then. It must wait on
the first, except the assignment behind a send, which must not. Each request runs on its own thread, app context,
session and connection, as two gunicorn workers would. Every wait is bounded, so a broken lock order fails instead of
hanging. Celery publishing is the only thing faked (`enqueued`).

Run: docker run --rm --network bluestream_default --env-file .env -v "$(pwd):/app" -w /app -e FLASK_ENV=testing
-e TESTING=true bluestream-business_app sh -c 'pip install -q pytest-xdist >/dev/null 2>&1;
pytest tests/integration/test_bottle_lock_order_pg_e2e.py -n0 --no-cov --no-header -q'
"""

import threading
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from business_app.models.bottle import DriverBottleSession, DriverBottleSessionOrder, DriverSessionMembership
from business_app.models.delivery import Delivery
from business_app.models.order import Order, OrderItem
from business_app.models.product import Product, ProductCategory
from business_app.models.user import UserAddress
from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.services.driver_reconciliation_service import DriverReconciliationService
from business_app.services.order_service import OrderService
from shared.enums import (
    DeliveryStatus,
    DriverBottleSessionStatus,
    DriverSessionMembershipStatus,
    OrderStatus,
    PaymentMethod,
    UserRole,
    UserType,
)
from staff_bot.api_client import StaffAPIClient
from staff_bot.handlers.delivery.bottle_collection import BottleCollectionHandler
from tests.integration.test_bottle_transfer_races_pg_e2e import _admin_bearer, _bearer, _double_tap
from tests.integration.test_customer_cancel_races_pg import _finish, _lock_waiters, _thread, _user
from tests.integration.test_on_the_way_concurrency_pg_e2e import _crew_member
from tests.integration.test_redispatch_reschedules_to_today import enqueued  # noqa: F401 -- a fixture

pytestmark = [pytest.mark.integration, pytest.mark.e2e]

CLOSE_SESSION = "/api/v1/staff/bottles/session/close"
TRANSFERS = "/api/v1/staff/bottles/transfers"
COLLECTION = "/api/v1/staff/bottles/collection"
STATUS = "/api/v1/staff/delivery/{}/status"
ADMIN_ASSIGN = "/api/v1/admin/staff/delivery/assign/{}"
ADMIN_REASSIGN = "/api/v1/admin/staff/delivery/reassign/{}"
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
