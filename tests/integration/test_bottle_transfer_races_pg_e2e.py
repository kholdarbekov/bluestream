"""REAL POSTGRES: a double tap on a transfer button moves the bottles once.

Telegram delivers a quick second tap as a second request. Two such races are pinned here:

* Send all, tapped twice. Each send reads the sender's truck and books what it holds. Without a row lock both read
  5 full + 2 empties and both book them, so the truck is spent twice. `_record_bottle_transfer` reads the sender's
  open session FOR UPDATE (with `populate_existing`): the second tap waits, then reads the truck the first one
  emptied, and is refused with BOTTLE_TRANSFER_NOTHING_TO_SEND.
* Confirm, tapped twice. Each confirm reads the transfer, checks it is still PENDING and credits the receiver's truck.
  Without a row lock both read PENDING and both credit. `confirm_bottle_transfer` loads the transfer FOR UPDATE (with
  `populate_existing`) before that status check: the second tap waits, reads the first tap's CONFIRMED, and is
  refused with BOTTLE_TRANSFER_NOT_PENDING.
* Resolve, submitted twice from the admin page. `admin_resolve_transfer_dispute` takes the same transfer lock before
  its DISPUTED check, so the second submit reads RESOLVED and is refused instead of correcting the trucks again.

A transfer also races the close of a trip it touches:

* A confirm onto the truck the receiver co-drives, while its owner closes that trip. The landing rule reads the trip
  under its lock, so the confirm waits, sees the trip closed, and lands the bottles on a trip opened for the receiver
  by this hand-off. A closed trip's books are sealed: crediting it would leave its discrepancy off.
* A send and a close of the sender's own trip. `close_bottle_session` reads the trip FOR UPDATE, so whichever comes
  second waits: a close behind a send seals what the send left, and a send behind a close finds no open trip.
* A send and an admin's force-close of the same trip. `admin_force_close_session` reads the trip FOR UPDATE too, with
  the same two outcomes.
* A send of 10 L and a cash Delivered of 10 L from the same trip. Both lock the trip row before they read or write its
  10 L line, so they serialize there and neither loses the other's write: behind a Delivered the send reads the units
  gone and is refused; behind a send the Delivered still books its units (a stop already Arrived is never refused for
  what is on board), and the line reads below zero.

A co-driver's join (the owner's Approve) races the trip it joins and the joiner's own trip:

* An Approve while the owner closes the trip. `_join_session` reads the trip under its lock, so the Approve waits, sees
  the trip closed and is refused with BOTTLE_SESSION_NOT_OPEN: no co-driver is left on a sealed trip.
* An Approve while the joiner opens a trip of his own at the depot. Both lock his DeliveryPerson row first (FOR
  UPDATE, before the open writes the depot anchor), so whichever comes second waits and is refused for the first. A
  driver with no DeliveryPerson row (no delivery profile) has his users row locked in its place, by both.
* An Approve while the joiner, with nothing open, counts a transfer that opens him a trip. The count locks his
  DeliveryPerson row before the transfer, so whichever comes second waits: behind the count the Approve finds him on
  the trip it opened and is refused; behind the join the count lands on the joined trip.

The receiver's Reject (spec 2026-11-18 §6.7) locks the transfer row, then the sender's trip row, the count's order:

* Reject and Confirm of one transfer, tapped on two phones. Both lock the transfer before its PENDING check, so the
  second waits, reads what the first wrote and is refused with BOTTLE_TRANSFER_NOT_PENDING: the counters are the
  winner's.
* A reject and the sender's close of the trip the units left. Both lock the trip row: a close behind a reject seals
  the restored counters; a reject behind a close puts the units back on the closed trip and recomputes its
  discrepancies. A close never locks a transfer, so there is no cycle.
* A reject of one transfer and the confirm of another from the same trip, whose count would empty it. The confirm's
  auto-close reads the trip under its lock: behind a reject it reads the units back on board; ahead of one it still
  reads the rejected transfer pending. Either way the trip stays open.

`with_for_update` compiles to nothing on SQLite, so this runs on a real, fully-migrated Postgres database (`pg_app`,
the shared harness in tests/integration/conftest.py). The interleaving is forced, the way
test_on_the_way_concurrency_pg_e2e.py forces its own: the first tap is held right after the read its lock protects,
before it commits, and the second tap starts only then. Each runs on its own thread, app context, session and
connection, as two gunicorn workers would. Every wait is bounded, so a broken lock fails instead of hanging. Celery
publishing (`enqueued`) and, for a reject, the Slack post are the only things faked.

Run: docker run --rm --network bluestream_default --env-file .env -v "$(pwd):/app" -w /app -e FLASK_ENV=testing
-e TESTING=true bluestream-business_app sh -c 'pip install -q pytest-xdist >/dev/null 2>&1;
pytest tests/integration/test_bottle_transfer_races_pg_e2e.py -n0 --no-cov --no-header -q'
"""

import inspect
import threading
import uuid

import pytest
from flask_jwt_extended import create_access_token
from sqlalchemy import text

from business_app.models.bottle import (
    DriverBottleSession,
    DriverBottleSessionUnit,
    DriverBottleTransfer,
    DriverSessionMembership,
)
from business_app.models.delivery import DeliveryPerson
from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.services.order_service import OrderService
from business_app.utils import slack_alerts
from shared.enums import (
    DeliveryStatus,
    DriverBottleSessionStatus,
    DriverBottleTransferStatus,
    DriverSessionMembershipStatus,
    OrderStatus,
    UserRole,
    UserType,
)
from staff_bot.api_client import StaffAPIClient
from tests.integration.test_customer_cancel_races_pg import _await_a_lock_waiter, _finish, _thread, _user
from tests.integration.test_on_the_way_concurrency_pg_e2e import _crew_member
from tests.integration.test_redispatch_reschedules_to_today import enqueued  # noqa: F401 -- a fixture
from tests.integration.trip_unit_builders import (
    bind,
    delivery_for,
    order_with_lines,
    pushes,
    reoffers,
    ten_litre,
    trip,
    trip_category,
    trip_customer,
    trip_driver,
)
from tests.unit.test_sales_agent_tasks import _spy

pytestmark = [pytest.mark.integration, pytest.mark.e2e]

TRANSFERS = "/api/v1/staff/bottles/transfers"
CONFIRM = TRANSFERS + "/{}/confirm"
REJECT = TRANSFERS + "/{}/reject"
RESOLVE = "/api/v1/admin/bottles/transfers/{}/resolve"
CLOSE_SESSION = "/api/v1/staff/bottles/session/close"
FORCE_CLOSE = "/api/v1/admin/bottles/sessions/{}/force-close"
OPEN_SESSION = "/api/v1/staff/bottles/session/open"
APPROVE_JOIN = "/api/v1/staff/bottles/session/join-request/approve"
INVITE = "/api/v1/staff/bottles/session/invite"
DELIVERY_STATUS = "/api/v1/staff/delivery/{}/status"


def _trip(pg_db, driver, *, loaded, collected=0):
    session = DriverBottleSession(
        driver_user_id=driver.id,
        bottles_loaded=loaded,
        bottles_collected_from_customers=collected,
        status=DriverBottleSessionStatus.OPEN,
    )
    pg_db.session.add(session)
    pg_db.session.commit()
    return session


def _bearer(driver):
    token = create_access_token(identity=str(driver.id))
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _double_tap(pg_app, pg_db, monkeypatch, *, held_method, first_tap, second_tap, owner=BottleTrackingService):
    """Run `first_tap`, hold it right after its first call of `owner.<held_method>` returns, start `second_tap`, and
    prove the second waits on a lock the first holds. A staticmethod is held as one. Returns each tap's
    `(status, body)`."""
    first_holds = threading.Event()
    release_the_first = threading.Event()
    original = getattr(owner, held_method)

    def held(*args, **kwargs):
        result = original(*args, **kwargs)
        if threading.current_thread().name == "first" and not first_holds.is_set():
            first_holds.set()
            assert release_the_first.wait(timeout=30), "the test never released the first tap"
        return result

    is_static = isinstance(inspect.getattr_static(owner, held_method), staticmethod)
    monkeypatch.setattr(owner, held_method, staticmethod(held) if is_static else held)
    answers, errors = {}, []
    first = _thread("first", pg_app, first_tap, answers, errors, finally_set=first_holds)
    second = _thread("second", pg_app, second_tap, answers, errors)

    first.start()
    try:
        assert first_holds.wait(timeout=30), "the first tap never reached its hold point"
        assert errors == [], errors
        second.start()
        assert _await_a_lock_waiter(pg_db), "the second tap did not wait: nothing holds the row it reads"
        assert second.is_alive(), "the second tap finished while the first one held the row"
    finally:
        release_the_first.set()
        _finish(first, *([second] if second.ident else []))

    assert errors == []
    return answers["first"], answers["second"]


def test_a_double_tap_on_send_all_spends_the_truck_once(pg_app, pg_db, monkeypatch, enqueued):  # noqa: F811
    sender = _crew_member(pg_db, "+998901251021", "Ali")
    receiver = _crew_member(pg_db, "+998901251022", "Vali")
    trip_id = _trip(pg_db, sender, loaded=5, collected=2).id

    def taps_send_all(client):
        return client.post(TRANSFERS, json={"receiverDriverId": receiver.id, "sendAll": True}, headers=_bearer(sender))

    # Held after the send has read (and locked) the truck and checked the receiver, before it books anything.
    (first_status, first_body), (second_status, second_body) = _double_tap(
        pg_app,
        pg_db,
        monkeypatch,
        held_method="_transfer_recipients_query",
        first_tap=taps_send_all,
        second_tap=taps_send_all,
    )

    assert first_status == 201, first_body
    assert (first_body["data"]["declared_full"], first_body["data"]["declared_empty"]) == (5, 2)
    assert second_status == 400, second_body
    assert second_body["error_code"] == "BOTTLE_TRANSFER_NOTHING_TO_SEND"
    pg_db.session.expire_all()
    trip = pg_db.session.get(DriverBottleSession, trip_id)
    assert (trip.bottles_transferred_out, trip.empties_transferred_out) == (5, 2)
    assert [t.id for t in DriverBottleTransfer.query.all()] == [first_body["data"]["id"]]


def test_a_double_tap_on_confirm_credits_the_receiver_once(pg_app, pg_db, monkeypatch, enqueued):  # noqa: F811
    sender = _crew_member(pg_db, "+998901251001", "Ali")
    receiver = _crew_member(pg_db, "+998901251002", "Vali")
    _trip(pg_db, sender, loaded=7, collected=3)
    receiver_session_id = _trip(pg_db, receiver, loaded=4).id
    transfer_id = (
        BottleTrackingService().initiate_bottle_transfer(sender.id, receiver.id, full_quantity=5, empty_quantity=2).id
    )

    def taps_confirm(client):
        """The receiver's one-tap Confirm."""
        return client.post(CONFIRM.format(transfer_id), json={"confirmAsDeclared": True}, headers=_bearer(receiver))

    # Held right after its PENDING check passed, where it picks the receiver's session, before it credits.
    (first_status, first_body), (second_status, second_body) = _double_tap(
        pg_app,
        pg_db,
        monkeypatch,
        held_method="_receiver_session_for",
        first_tap=taps_confirm,
        second_tap=taps_confirm,
    )

    assert first_status == 200, first_body
    data = first_body["data"]
    assert (data["status"], data["confirmed_full"], data["confirmed_empty"]) == ("confirmed", 5, 2)
    assert second_status == 409, second_body
    assert second_body["error_code"] == "BOTTLE_TRANSFER_NOT_PENDING"
    pg_db.session.expire_all()
    landing = pg_db.session.get(DriverBottleSession, receiver_session_id)
    assert (landing.bottles_transferred_in, landing.empties_transferred_in) == (5, 2)
    transfer = pg_db.session.get(DriverBottleTransfer, transfer_id)
    assert transfer.status == DriverBottleTransferStatus.CONFIRMED
    assert transfer.receiver_session_id == receiver_session_id


def test_a_double_submitted_resolve_corrects_the_trucks_once(pg_app, pg_db, monkeypatch, enqueued):  # noqa: F811
    admin = _user(pg_db, "+998901251033", role=UserRole.ADMIN, user_type=UserType.STAFF)
    sender = _crew_member(pg_db, "+998901251031", "Ali")
    receiver = _crew_member(pg_db, "+998901251032", "Vali")
    sender_trip_id = _trip(pg_db, sender, loaded=7, collected=3).id
    receiver_trip_id = _trip(pg_db, receiver, loaded=4).id
    service = BottleTrackingService()
    transfer_id = service.initiate_bottle_transfer(sender.id, receiver.id, full_quantity=5, empty_quantity=2).id
    service.confirm_bottle_transfer(transfer_id, receiver.id, confirmed_full=4, confirmed_empty=2)
    # The sender is back at the depot with what is left (2 full + 1 empty): his trip closes, so the resolve
    # recomputes its discrepancy, which is where the first submit is held.
    service.close_bottle_session(sender.id, 3)
    pg_db.session.commit()
    admin_headers = {
        "Authorization": "Bearer "
        + create_access_token(identity=str(admin.id), additional_claims={"role": "admin"}),
        "Content-Type": "application/json",
    }

    def submits_resolve(client):
        """The admin resolve modal's body."""
        return client.post(
            RESOLVE.format(transfer_id),
            json={"resolvedFull": 5, "resolvedEmpty": 2, "resolutionNotes": "Counted again at the depot"},
            headers=admin_headers,
        )

    (first_status, first_body), (second_status, second_body) = _double_tap(
        pg_app,
        pg_db,
        monkeypatch,
        owner=DriverBottleSession,
        held_method="compute_discrepancy",
        first_tap=submits_resolve,
        second_tap=submits_resolve,
    )

    assert first_status == 200, first_body
    assert (first_body["data"]["status"], first_body["data"]["confirmed_full"]) == ("resolved", 5)
    assert second_status == 409, second_body
    assert second_body["message"] == "Can only resolve DISPUTED transfers"
    pg_db.session.expire_all()
    sender_trip = pg_db.session.get(DriverBottleSession, sender_trip_id)
    receiver_trip = pg_db.session.get(DriverBottleSession, receiver_trip_id)
    assert (sender_trip.bottles_transferred_out, sender_trip.empties_transferred_out, sender_trip.discrepancy) == (
        5,
        2,
        0,
    )
    assert (receiver_trip.bottles_transferred_in, receiver_trip.empties_transferred_in) == (5, 2)


def test_a_transfer_written_without_the_empties_columns_reads_zero_empties(pg_app, pg_db):
    """The app still running the old model during the deploy inserts transfers without the empties columns: the
    migration's server default makes such a row read 0 empties declared, and none confirmed yet."""
    sender = _crew_member(pg_db, "+998901251011", "Ali")
    receiver = _crew_member(pg_db, "+998901251012", "Vali")
    session = _trip(pg_db, sender, loaded=7)

    row = pg_db.session.execute(
        text(
            "INSERT INTO driver_bottle_transfers "
            "(transfer_ref, sender_session_id, sender_driver_id, receiver_driver_id, declared_quantity, status, "
            "sent_at, created_at, updated_at) "
            "VALUES (:ref, :session, :sender, :receiver, 4, 'pending', now(), now(), now()) "
            "RETURNING declared_quantity, declared_empty_quantity, confirmed_empty_quantity"
        ),
        {"ref": str(uuid.uuid4()), "session": session.id, "sender": sender.id, "receiver": receiver.id},
    ).one()
    pg_db.session.commit()

    assert tuple(row) == (4, 0, None)


def test_a_confirm_onto_a_trip_its_owner_is_closing_lands_on_a_trip_opened_for_the_receiver(
    pg_app, pg_db, monkeypatch, enqueued  # noqa: F811
):
    """Vali co-drives Bobur's truck. Bobur closes his trip (6 loaded, 6 returned) while Vali counts Ali's 5 + 2."""
    sender = _crew_member(pg_db, "+998901251041", "Ali")
    receiver = _crew_member(pg_db, "+998901251042", "Vali")
    owner = _crew_member(pg_db, "+998901251043", "Bobur")
    _trip(pg_db, sender, loaded=7, collected=3)
    co_driven_id = _trip(pg_db, owner, loaded=6).id
    pg_db.session.add(
        DriverSessionMembership(session_id=co_driven_id, session_owner_id=owner.id, member_driver_id=receiver.id)
    )
    pg_db.session.commit()
    transfer_id = (
        BottleTrackingService().initiate_bottle_transfer(sender.id, receiver.id, full_quantity=5, empty_quantity=2).id
    )

    def owner_closes(client):
        return client.post(CLOSE_SESSION, json={"bottles_returned_to_warehouse": 6}, headers=_bearer(owner))

    def receiver_confirms(client):
        return client.post(CONFIRM.format(transfer_id), json={"confirmAsDeclared": True}, headers=_bearer(receiver))

    # Held inside the close, after the trip is marked closed and its co-drivers let go, before it commits.
    (close_status, close_body), (confirm_status, confirm_body) = _double_tap(
        pg_app,
        pg_db,
        monkeypatch,
        held_method="revoke_all_memberships",
        first_tap=owner_closes,
        second_tap=receiver_confirms,
    )

    assert close_status == 200, close_body
    assert (close_body["data"]["status"], close_body["data"]["discrepancy"]) == ("closed", 0)
    assert confirm_status == 200, confirm_body
    assert confirm_body["data"]["status"] == "confirmed"
    pg_db.session.expire_all()
    co_driven = pg_db.session.get(DriverBottleSession, co_driven_id)
    assert (co_driven.bottles_transferred_in, co_driven.empties_transferred_in, co_driven.discrepancy) == (0, 0, 0)
    [landing] = DriverBottleSession.query.filter_by(
        driver_user_id=receiver.id, status=DriverBottleSessionStatus.OPEN
    ).all()
    assert (landing.bottles_loaded, landing.bottles_transferred_in, landing.empties_transferred_in) == (0, 5, 2)
    assert pg_db.session.get(DriverBottleTransfer, transfer_id).receiver_session_id == landing.id


def test_a_close_behind_a_send_seals_what_the_send_left(pg_app, pg_db, monkeypatch, enqueued):  # noqa: F811
    """Ali (7 full + 3 empties) sends 5 + 2 and, from a second phone, closes his trip returning the 3 he keeps."""
    sender = _crew_member(pg_db, "+998901251051", "Ali")
    receiver = _crew_member(pg_db, "+998901251052", "Vali")
    trip_id = _trip(pg_db, sender, loaded=7, collected=3).id

    def sends(client):
        body = {"receiverDriverId": receiver.id, "fullQuantity": 5, "emptyQuantity": 2}
        return client.post(TRANSFERS, json=body, headers=_bearer(sender))

    def closes(client):
        return client.post(CLOSE_SESSION, json={"bottles_returned_to_warehouse": 3}, headers=_bearer(sender))

    # Held after the send has read (and locked) the truck, before it books anything.
    (send_status, send_body), (close_status, close_body) = _double_tap(
        pg_app,
        pg_db,
        monkeypatch,
        held_method="_transfer_recipients_query",
        first_tap=sends,
        second_tap=closes,
    )

    assert send_status == 201, send_body
    assert close_status == 200, close_body
    pg_db.session.expire_all()
    trip = pg_db.session.get(DriverBottleSession, trip_id)
    assert (trip.status, trip.bottles_transferred_out, trip.empties_transferred_out, trip.discrepancy) == (
        DriverBottleSessionStatus.CLOSED,
        5,
        2,
        0,
    )


def test_a_send_behind_a_close_finds_no_open_trip(pg_app, pg_db, monkeypatch, enqueued):  # noqa: F811
    sender = _crew_member(pg_db, "+998901251061", "Ali")
    receiver = _crew_member(pg_db, "+998901251062", "Vali")
    trip_id = _trip(pg_db, sender, loaded=7, collected=3).id

    def closes(client):
        return client.post(CLOSE_SESSION, json={"bottles_returned_to_warehouse": 10}, headers=_bearer(sender))

    def sends_all(client):
        return client.post(TRANSFERS, json={"receiverDriverId": receiver.id, "sendAll": True}, headers=_bearer(sender))

    # Held inside the close after it has read the trip, before it marks anything.
    (close_status, close_body), (send_status, send_body) = _double_tap(
        pg_app,
        pg_db,
        monkeypatch,
        held_method="_release_open_bindings_for_session",
        first_tap=closes,
        second_tap=sends_all,
    )

    assert close_status == 200, close_body
    assert close_body["data"]["discrepancy"] == 0
    assert send_status == 404, send_body
    assert send_body["error_code"] == "BOTTLE_SESSION_NOT_FOUND"
    pg_db.session.expire_all()
    trip = pg_db.session.get(DriverBottleSession, trip_id)
    assert (trip.bottles_transferred_out, trip.empties_transferred_out) == (0, 0)
    assert DriverBottleTransfer.query.count() == 0


def _admin_bearer(admin):
    """The admin page's bearer: the role claim the panel's routes read."""
    token = create_access_token(identity=str(admin.id), additional_claims={"role": "admin"})
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def test_a_force_close_behind_a_send_seals_what_the_send_left(pg_app, pg_db, monkeypatch, enqueued):  # noqa: F811
    """Ali (7 full + 3 empties) sends 5 + 2 while an admin force-closes his trip with the 3 he keeps returned."""
    admin = _user(pg_db, "+998901251073", role=UserRole.ADMIN, user_type=UserType.STAFF)
    sender = _crew_member(pg_db, "+998901251071", "Ali")
    receiver = _crew_member(pg_db, "+998901251072", "Vali")
    trip_id = _trip(pg_db, sender, loaded=7, collected=3).id

    def sends(client):
        body = {"receiverDriverId": receiver.id, "fullQuantity": 5, "emptyQuantity": 2}
        return client.post(TRANSFERS, json=body, headers=_bearer(sender))

    def force_closes(client):
        """The admin page's force-close form."""
        body = {"bottles_returned_to_warehouse": 3, "reason": "Driver left the trip open"}
        return client.post(FORCE_CLOSE.format(trip_id), json=body, headers=_admin_bearer(admin))

    # Held after the send has read (and locked) the truck, before it books anything.
    (send_status, send_body), (close_status, close_body) = _double_tap(
        pg_app,
        pg_db,
        monkeypatch,
        held_method="_transfer_recipients_query",
        first_tap=sends,
        second_tap=force_closes,
    )

    assert send_status == 201, send_body
    assert close_status == 200, close_body
    assert (close_body["data"]["status"], close_body["data"]["discrepancy"]) == ("force_closed", 0)
    pg_db.session.expire_all()
    trip = pg_db.session.get(DriverBottleSession, trip_id)
    assert (trip.status, trip.bottles_transferred_out, trip.empties_transferred_out, trip.discrepancy) == (
        DriverBottleSessionStatus.FORCE_CLOSED,
        5,
        2,
        0,
    )


def test_a_send_behind_a_force_close_finds_no_open_trip(pg_app, pg_db, monkeypatch, enqueued):  # noqa: F811
    admin = _user(pg_db, "+998901251083", role=UserRole.ADMIN, user_type=UserType.STAFF)
    sender = _crew_member(pg_db, "+998901251081", "Ali")
    receiver = _crew_member(pg_db, "+998901251082", "Vali")
    trip_id = _trip(pg_db, sender, loaded=7, collected=3).id

    def force_closes(client):
        body = {"bottles_returned_to_warehouse": 10, "reason": "Driver left the trip open"}
        return client.post(FORCE_CLOSE.format(trip_id), json=body, headers=_admin_bearer(admin))

    def sends_all(client):
        return client.post(TRANSFERS, json={"receiverDriverId": receiver.id, "sendAll": True}, headers=_bearer(sender))

    # Held inside the force-close after it has read the trip, before it marks anything.
    (close_status, close_body), (send_status, send_body) = _double_tap(
        pg_app,
        pg_db,
        monkeypatch,
        held_method="_release_open_bindings_for_session",
        first_tap=force_closes,
        second_tap=sends_all,
    )

    assert close_status == 200, close_body
    assert close_body["data"]["discrepancy"] == 0
    assert send_status == 404, send_body
    assert send_body["error_code"] == "BOTTLE_SESSION_NOT_FOUND"
    pg_db.session.expire_all()
    trip = pg_db.session.get(DriverBottleSession, trip_id)
    assert (trip.bottles_transferred_out, trip.empties_transferred_out) == (0, 0)
    assert DriverBottleTransfer.query.count() == 0


@pytest.mark.parametrize("order", ["delivered_first", "send_first"])
def test_a_send_and_a_ten_litre_only_delivered_serialize_on_the_trip_row(
    pg_app, pg_db, monkeypatch, enqueued, order  # noqa: F811
):
    """Aziz has 4 x 10 L on board and no 18.9 L. He is Arrived at a cash door with an order of 4 x 10 L, and from a
    second phone he sends Bobur the same 4. Both lock the trip row before they touch its 10 L line, so the second waits
    for the first and reads what it wrote: behind the Delivered the send finds 0 to send; behind the send the Delivered
    still books its 4 (an Arrived stop is never refused for what is on board), so the line reads 4 below zero. Every
    answer is asserted exactly: none is a 500, and no deadlock (40P01) aborted either side."""
    customer = trip_customer(pg_db)
    sender = trip_driver(pg_db, "Aziz", telegram_id="900000141")
    receiver = trip_driver(pg_db, "Bobur", telegram_id="900000142")
    small = ten_litre(pg_db, trip_category(pg_db))
    small_id, receiver_id = small.id, receiver.id
    session = trip(pg_db, sender, units={small_id: 4})
    trip_id = session.id
    cash_order = order_with_lines(pg_db, customer, [(small, 4)], status=OrderStatus.OUT_FOR_DELIVERY)
    delivery_id = delivery_for(pg_db, cash_order, sender, DeliveryStatus.ARRIVED).id
    bind(pg_db, session, cash_order, sender)
    sender_headers = _bearer(sender)

    def sends(client):
        """What the staff bot sends for 0 full, 0 empties and 4 x 10 L."""
        body = {
            "receiver_driver_id": receiver_id,
            "full_quantity": 0,
            "empty_quantity": 0,
            "units": [{"product_id": small_id, "quantity": 4}],
        }
        return client.post(TRANSFERS, json=body, headers=sender_headers)

    def delivers(client):
        """The staff bot's Delivered tap."""
        body = StaffAPIClient.delivery_status_body("delivered", None)
        return client.put(DELIVERY_STATUS.format(delivery_id), json=body, headers=sender_headers)

    if order == "delivered_first":
        # Held right after the Delivered added its 4 to the trip's line (under the trip lock), before it commits.
        (delivered_status, delivered_body), (send_status, send_body) = _double_tap(
            pg_app,
            pg_db,
            monkeypatch,
            owner=OrderService,
            held_method="_record_delivery_bottles",
            first_tap=delivers,
            second_tap=sends,
        )
    else:
        # Held after the send has read (and locked) the trip and checked the receiver, before it books anything.
        (send_status, send_body), (delivered_status, delivered_body) = _double_tap(
            pg_app,
            pg_db,
            monkeypatch,
            held_method="_transfer_recipients_query",
            first_tap=sends,
            second_tap=delivers,
        )

    assert "deadlock" not in str(send_body), send_body
    assert "deadlock" not in str(delivered_body), delivered_body
    assert delivered_status == 200, delivered_body
    assert delivered_body["data"]["status"] == "delivered"
    pg_db.session.expire_all()
    line = DriverBottleSessionUnit.query.filter_by(session_id=trip_id, product_id=small_id).one()
    if order == "delivered_first":
        assert send_status == 400, send_body
        assert (send_body["error_code"], send_body["details"]) == (
            "BOTTLE_TRANSFER_EXCEEDS_INVENTORY",
            {
                "kind": "product",
                "product_id": small_id,
                "product_name": "10 Litr suv",
                "requested": 4,
                "available": 0,
                "validation_errors": [],
            },
        )
        assert (line.delivered, line.transferred_out) == (4, 0)
        assert DriverBottleTransfer.query.count() == 0
    else:
        assert send_status == 201, send_body
        assert send_body["data"]["units"] == [
            {"product_id": small_id, "product_name": "10 Litr suv", "declared": 4, "confirmed": None, "resolved": None}
        ]
        assert (line.delivered, line.transferred_out, line.on_board, line.transferable) == (4, 4, -4, 0)


# --------------------------------------------------------------------------- #
# A join against the close and the open of a trip (spec 2026-11-18 §6.2)
# --------------------------------------------------------------------------- #


def _depot_anchor(user_id):
    """The driver's stored position: an open at the depot writes it before anything else."""
    person = DeliveryPerson.query.filter_by(user_id=user_id).one()
    return person.current_location_lat, person.current_location_lng, person.last_location_update


def _open_held_after_its_anchor(pg_app, pg_db, monkeypatch, *, opens, approves):
    """Run `opens` on the thread "open" and hold it on entry to its `lock_trip_table("ROW EXCLUSIVE")`, where the depot
    anchor has written the joiner's DeliveryPerson row and nothing else is done. Then start `approves` on the thread
    "approve" and prove it waits on a lock the open holds. `lock_trip_table` is a staticmethod, so the hold is installed
    as one. Returns the open's `(status, body)`, then the approve's."""
    open_holds = threading.Event()
    release_the_open = threading.Event()
    original = BottleTrackingService.lock_trip_table

    def held(mode):
        if threading.current_thread().name == "open" and mode == "ROW EXCLUSIVE" and not open_holds.is_set():
            open_holds.set()
            assert release_the_open.wait(timeout=30), "the test never released the open"
        original(mode)

    monkeypatch.setattr(BottleTrackingService, "lock_trip_table", staticmethod(held))
    answers, errors = {}, []
    open_thread = _thread("open", pg_app, opens, answers, errors, finally_set=open_holds)
    approve_thread = _thread("approve", pg_app, approves, answers, errors)

    open_thread.start()
    try:
        assert open_holds.wait(timeout=30), "the open never reached its trips-table lock"
        assert errors == [], errors
        approve_thread.start()
        assert _await_a_lock_waiter(pg_db), "the approve did not wait on the joiner's DeliveryPerson row the open holds"
        assert approve_thread.is_alive(), "the approve finished while the open held the joiner's DeliveryPerson row"
    finally:
        release_the_open.set()
        _finish(open_thread, *([approve_thread] if approve_thread.ident else []))

    assert errors == []
    return answers["open"], answers["approve"]


@pytest.mark.parametrize(
    "answer, refusal",
    [("approve", "BOTTLE_JOIN_REQUEST_STALE"), ("invite", "BOTTLE_SESSION_REQUIRED_TO_INVITE")],
)
def test_a_join_approved_while_the_owner_closes_leaves_no_membership(
    pg_app, pg_db, monkeypatch, enqueued, answer, refusal  # noqa: F811
):
    """Kamol asked to join Olim's trip (40 x 18.9 L, 12 x 10 L). Olim taps Approve on one phone while, at the depot, he
    closes the trip on another, returning all of it (or, instead of a request, Olim invites Kamol). The approve read the
    trip open, then waited for the close: it is refused with the code the same tap gets after the close
    (BOTTLE_JOIN_REQUEST_STALE for an approve, BOTTLE_SESSION_REQUIRED_TO_INVITE for an invite: the owner's words),
    never the joiner's BOTTLE_SESSION_NOT_OPEN, so the timing never picks the sentence Olim reads."""
    owner = trip_driver(pg_db, "Olim", telegram_id="900000121")
    co_driver = trip_driver(pg_db, "Kamol", telegram_id="900000122")
    unit_id = ten_litre(pg_db, trip_category(pg_db)).id
    trip_id = trip(pg_db, owner, bottles=40, units={unit_id: 12}).id
    co_driver_id = co_driver.id
    owner_headers = _bearer(owner)

    def closes(client):
        """The close's body: the 18.9 L count and one return per line on the trip."""
        body = {"bottles_returned_to_warehouse": 40, "units_returned": [{"product_id": unit_id, "quantity": 12}]}
        return client.post(CLOSE_SESSION, json=body, headers=owner_headers)

    def approves(client):
        """What the staff bot sends when the owner taps Approve on a join request."""
        return client.post(
            APPROVE_JOIN, json={"session_id": trip_id, "requester_id": co_driver_id}, headers=owner_headers
        )

    def invites(client):
        """What the staff bot sends when the owner picks a driver to invite."""
        return client.post(INVITE, json={"member_driver_id": co_driver_id}, headers=owner_headers)

    # Held inside the close after it has read (and locked) the trip, before it marks anything.
    (close_status, close_body), (approve_status, approve_body) = _double_tap(
        pg_app,
        pg_db,
        monkeypatch,
        held_method="_release_open_bindings_for_session",
        first_tap=closes,
        second_tap={"approve": approves, "invite": invites}[answer],
    )

    assert close_status == 200, close_body
    assert close_body["data"]["status"] == "closed"
    assert approve_status == 409, approve_body
    assert approve_body["error_code"] == refusal
    pg_db.session.expire_all()
    assert (
        DriverSessionMembership.query.filter_by(session_id=trip_id, status=DriverSessionMembershipStatus.ACTIVE).all()
        == []
    )
    assert pushes(enqueued) == []


@pytest.mark.parametrize("first", ["open", "join"])
def test_a_co_drivers_open_and_his_join_serialize(pg_app, pg_db, monkeypatch, enqueued, first):  # noqa: F811
    """Kamol asked to join Olim's trip. At the depot he then loads a trip of his own (10 x 18.9 L, 4 x 10 L) on one
    phone while Olim taps Approve on another. The open writes Kamol's depot anchor (his DeliveryPerson row) first; the
    join locks that row FOR UPDATE before the trip. So whichever comes second waits for the first and is refused for
    it: behind an open, the Approve finds Kamol on his own trip; behind a join, the open finds him on Olim's. Every
    answer is asserted exactly: none is a 500, and no deadlock (40P01) aborted either side."""
    owner = trip_driver(pg_db, "Olim", telegram_id="900000131")
    co_driver = trip_driver(pg_db, "Kamol", telegram_id="900000132")
    owner_id, co_driver_id = owner.id, co_driver.id
    unit_id = ten_litre(pg_db, trip_category(pg_db)).id
    owner_trip_id = trip(pg_db, owner, bottles=40).id
    owner_headers, co_driver_headers = _bearer(owner), _bearer(co_driver)
    anchor_before = _depot_anchor(co_driver_id)

    def opens(client):
        """What the staff bot sends for Kamol's Load."""
        body = {"bottles_loaded": 10, "units": [{"product_id": unit_id, "quantity": 4}]}
        return client.post(OPEN_SESSION, json=body, headers=co_driver_headers)

    def approves(client):
        """What the staff bot sends when Olim taps Approve on Kamol's request."""
        return client.post(
            APPROVE_JOIN, json={"session_id": owner_trip_id, "requester_id": co_driver_id}, headers=owner_headers
        )

    if first == "open":
        (open_status, open_body), (approve_status, approve_body) = _open_held_after_its_anchor(
            pg_app, pg_db, monkeypatch, opens=opens, approves=approves
        )
    else:
        # Held after the join has locked Kamol's DeliveryPerson row and Olim's trip and passed every rule, before it
        # writes the membership.
        (approve_status, approve_body), (open_status, open_body) = _double_tap(
            pg_app,
            pg_db,
            monkeypatch,
            held_method="_assert_can_join",
            first_tap=approves,
            second_tap=opens,
        )

    pg_db.session.expire_all()
    his_memberships = DriverSessionMembership.query.filter_by(
        member_driver_id=co_driver_id, status=DriverSessionMembershipStatus.ACTIVE
    ).all()
    his_open_trips = DriverBottleSession.query.filter_by(
        driver_user_id=co_driver_id, status=DriverBottleSessionStatus.OPEN
    ).all()
    if first == "open":
        assert open_status == 201, open_body
        assert approve_status == 409, approve_body
        assert approve_body["error_code"] == "BOTTLE_INVITEE_HAS_SESSION"
        assert [t.id for t in his_open_trips] == [open_body["data"]["id"]]
        assert his_memberships == []
        assert pushes(enqueued) == []
    else:
        assert approve_status == 200, approve_body
        assert (approve_body["data"]["session_id"], approve_body["data"]["status"]) == (owner_trip_id, "active")
        assert open_status == 409, open_body
        assert open_body["error_code"] == "BOTTLE_SESSION_MEMBERSHIP_ALREADY_ACTIVE"
        assert open_body["details"] == {"session_id": owner_trip_id, "owner_user_id": owner_id, "owner_name": "Olim"}
        assert his_open_trips == []
        assert [m.session_id for m in his_memberships] == [owner_trip_id]
        # The refused open wrote the depot anchor first; it rolled back with the refusal.
        assert _depot_anchor(co_driver_id) == anchor_before
        assert pushes(enqueued) == [(900000132, "join_approved", {"session_id": owner_trip_id, "owner_name": "Olim"})]


@pytest.mark.parametrize("first", ["open", "join"])
def test_a_driver_with_no_delivery_profile_and_his_join_serialize(
    pg_app, pg_db, monkeypatch, enqueued, first  # noqa: F811
):
    """The same race for a driver the Operators page made a driver without a delivery profile (no DeliveryPerson row):
    the staff bot lets him in, he may load, and Olim may approve his request to ride along. His open has no depot
    anchor to write and the join had no DeliveryPerson row to lock, so neither waited for the other and both
    committed: he ended on his own open trip and on Olim's at once. Both now take his users row in that rung instead,
    so the second waits for the first and is refused for it."""
    owner = trip_driver(pg_db, "Olim", telegram_id="900000151")
    kamol = _user(pg_db, "+998901251151", role=UserRole.DELIVERY_DRIVER, user_type=UserType.STAFF)
    owner_id, kamol_id = owner.id, kamol.id
    assert DeliveryPerson.query.filter_by(user_id=kamol_id).count() == 0
    owner_trip_id = trip(pg_db, owner, bottles=40).id
    owner_headers, kamol_headers = _bearer(owner), _bearer(kamol)

    def opens(client):
        """What the staff bot sends for Kamol's Load."""
        return client.post(OPEN_SESSION, json={"bottles_loaded": 10}, headers=kamol_headers)

    def approves(client):
        """What the staff bot sends when Olim taps Approve on Kamol's request."""
        return client.post(
            APPROVE_JOIN, json={"session_id": owner_trip_id, "requester_id": kamol_id}, headers=owner_headers
        )

    if first == "open":
        (open_status, open_body), (approve_status, approve_body) = _open_held_after_its_anchor(
            pg_app, pg_db, monkeypatch, opens=opens, approves=approves
        )
    else:
        # Held after the join has locked Kamol's row and Olim's trip and passed every rule, before it writes the
        # membership.
        (approve_status, approve_body), (open_status, open_body) = _double_tap(
            pg_app,
            pg_db,
            monkeypatch,
            held_method="_assert_can_join",
            first_tap=approves,
            second_tap=opens,
        )

    assert "deadlock" not in str(open_body), open_body
    assert "deadlock" not in str(approve_body), approve_body
    pg_db.session.expire_all()
    his_memberships = DriverSessionMembership.query.filter_by(
        member_driver_id=kamol_id, status=DriverSessionMembershipStatus.ACTIVE
    ).all()
    his_open_trips = DriverBottleSession.query.filter_by(
        driver_user_id=kamol_id, status=DriverBottleSessionStatus.OPEN
    ).all()
    if first == "open":
        assert open_status == 201, open_body
        assert approve_status == 409, approve_body
        assert approve_body["error_code"] == "BOTTLE_INVITEE_HAS_SESSION"
        assert [t.id for t in his_open_trips] == [open_body["data"]["id"]]
        assert his_memberships == []
    else:
        assert approve_status == 200, approve_body
        assert open_status == 409, open_body
        assert open_body["error_code"] == "BOTTLE_SESSION_MEMBERSHIP_ALREADY_ACTIVE"
        assert open_body["details"] == {"session_id": owner_trip_id, "owner_user_id": owner_id, "owner_name": "Olim"}
        assert his_open_trips == []
        assert [m.session_id for m in his_memberships] == [owner_trip_id]


def _count_held_after_its_delivery_person_lock(pg_app, pg_db, monkeypatch, *, counts, approves):
    """Run `counts` on the thread "count" and hold it right after its `_lock_delivery_persons` returns (the receiver's
    DeliveryPerson row, its first lock). Then start `approves` on the thread "approve" and prove it waits on a lock the
    count holds. `_lock_delivery_persons` is a staticmethod, so the hold is installed as one. Returns the count's
    `(status, body)`, then the approve's."""
    count_holds = threading.Event()
    release_the_count = threading.Event()
    original = BottleTrackingService._lock_delivery_persons

    def held(user_ids):
        original(user_ids)
        if threading.current_thread().name == "count" and not count_holds.is_set():
            count_holds.set()
            assert release_the_count.wait(timeout=30), "the test never released the count"

    monkeypatch.setattr(BottleTrackingService, "_lock_delivery_persons", staticmethod(held))
    answers, errors = {}, []
    count_thread = _thread("count", pg_app, counts, answers, errors, finally_set=count_holds)
    approve_thread = _thread("approve", pg_app, approves, answers, errors)

    count_thread.start()
    try:
        assert count_holds.wait(timeout=30), "the count never reached its DeliveryPerson lock"
        assert errors == [], errors
        approve_thread.start()
        assert _await_a_lock_waiter(pg_db), "the approve did not wait on the DeliveryPerson row the count holds"
        assert approve_thread.is_alive(), "the approve finished while the count held the receiver's DeliveryPerson row"
    finally:
        release_the_count.set()
        _finish(count_thread, *([approve_thread] if approve_thread.ident else []))

    assert errors == []
    return answers["count"], answers["approve"]


@pytest.mark.parametrize("first", ["count", "join"])
def test_a_count_that_opens_a_trip_and_the_receivers_join_serialize(
    pg_app, pg_db, monkeypatch, enqueued, first  # noqa: F811
):
    """Kamol asked to join Olim's trip and has nothing open. Aziz handed him 5 x 18.9 L; Kamol taps Confirm on one phone
    while Olim taps Approve on another. The count locks Kamol's DeliveryPerson row before anything else (then the
    transfer, then the trip it lands on or opens); the join locks that row FOR UPDATE before Olim's trip. So whichever
    comes second waits for the first: behind a count that opened Kamol a trip, the Approve finds him on it and is
    refused; behind a join, the count lands on Olim's trip. Kamol ends with exactly one place to be. Every answer is
    asserted exactly: none is a 500, and no deadlock (40P01) aborted either side."""
    owner = trip_driver(pg_db, "Olim", telegram_id="900000151")
    receiver = trip_driver(pg_db, "Kamol", telegram_id="900000152")
    sender = trip_driver(pg_db, "Aziz", telegram_id="900000153")
    receiver_id = receiver.id
    owner_trip_id = trip(pg_db, owner, bottles=40).id
    trip(pg_db, sender, bottles=5)
    transfer_id = BottleTrackingService().initiate_bottle_transfer(sender.id, receiver_id, full_quantity=5).id
    owner_headers, receiver_headers = _bearer(owner), _bearer(receiver)

    def counts(client):
        """The receiver's one-tap Confirm, as the staff bot sends it."""
        return client.post(CONFIRM.format(transfer_id), json={"confirmAsDeclared": True}, headers=receiver_headers)

    def approves(client):
        """What the staff bot sends when Olim taps Approve on Kamol's request."""
        return client.post(
            APPROVE_JOIN, json={"session_id": owner_trip_id, "requester_id": receiver_id}, headers=owner_headers
        )

    if first == "count":
        (count_status, count_body), (approve_status, approve_body) = _count_held_after_its_delivery_person_lock(
            pg_app, pg_db, monkeypatch, counts=counts, approves=approves
        )
    else:
        # Held after the join has locked Kamol's DeliveryPerson row and Olim's trip and passed every rule, before it
        # writes the membership.
        (approve_status, approve_body), (count_status, count_body) = _double_tap(
            pg_app,
            pg_db,
            monkeypatch,
            held_method="_assert_can_join",
            first_tap=approves,
            second_tap=counts,
        )

    assert count_status == 200, count_body
    assert count_body["data"]["status"] == "confirmed"
    pg_db.session.expire_all()
    his_open_trips = DriverBottleSession.query.filter_by(
        driver_user_id=receiver_id, status=DriverBottleSessionStatus.OPEN
    ).all()
    his_memberships = DriverSessionMembership.query.filter_by(
        member_driver_id=receiver_id, status=DriverSessionMembershipStatus.ACTIVE
    ).all()
    joins_approved = [push for push in pushes(enqueued) if push[1] == "join_approved"]
    if first == "count":
        assert approve_status == 409, approve_body
        assert approve_body["error_code"] == "BOTTLE_INVITEE_HAS_SESSION"
        [opened] = his_open_trips
        assert count_body["data"]["receiver_session_id"] == opened.id
        assert (opened.bottles_loaded, opened.bottles_transferred_in) == (0, 5)
        assert his_memberships == []
        assert joins_approved == []
    else:
        assert approve_status == 200, approve_body
        assert (approve_body["data"]["session_id"], approve_body["data"]["status"]) == (owner_trip_id, "active")
        assert count_body["data"]["receiver_session_id"] == owner_trip_id
        assert his_open_trips == []
        assert [m.session_id for m in his_memberships] == [owner_trip_id]
        assert pg_db.session.get(DriverBottleSession, owner_trip_id).bottles_transferred_in == 5
        assert joins_approved == [(900000152, "join_approved", {"session_id": owner_trip_id, "owner_name": "Olim"})]


@pytest.mark.parametrize("first", ["resolve", "join"])
def test_a_resolve_that_opens_a_trip_and_the_receivers_join_serialize(
    pg_app, pg_db, monkeypatch, enqueued, first  # noqa: F811
):
    """Kamol asked to join Olim's trip and has nothing open. Aziz handed him 5 x 18.9 L; Kamol counted 0, so nothing was
    credited and the transfer is disputed. The office resolves it at 5 while Olim taps Approve on Kamol's request. The
    resolve locks Kamol's DeliveryPerson row before the transfer and the trips (as the count does), the join locks it
    before Olim's trip: whichever comes second waits for the first, and Kamol ends with exactly one place to be.
    Behind a resolve that opened Kamol a trip the Approve is refused. Behind a join the resolve is refused (it never
    lands on a trip he co-drives, and a trip of his own would be his second place to be) and writes nothing: the
    transfer stays disputed. Every answer is asserted exactly: none is a 500, and no deadlock (40P01) aborted either
    side."""
    admin = _user(pg_db, "+998901251041", role=UserRole.ADMIN, user_type=UserType.STAFF)
    owner = trip_driver(pg_db, "Olim", telegram_id="900000161")
    receiver = trip_driver(pg_db, "Kamol", telegram_id="900000162")
    sender = trip_driver(pg_db, "Aziz", telegram_id="900000163")
    receiver_id, owner_id = receiver.id, owner.id
    owner_trip_id = trip(pg_db, owner, bottles=40).id
    sender_trip_id = trip(pg_db, sender, bottles=5).id
    service = BottleTrackingService()
    transfer_id = service.initiate_bottle_transfer(sender.id, receiver_id, full_quantity=5).id
    service.confirm_bottle_transfer(transfer_id, receiver_id, confirmed_full=0, confirmed_empty=0)
    pg_db.session.commit()
    assert DriverBottleSession.query.filter_by(driver_user_id=receiver_id).count() == 0
    owner_headers = _bearer(owner)

    def resolves(client):
        """The admin resolve modal's body."""
        return client.post(
            RESOLVE.format(transfer_id),
            json={"resolvedFull": 5, "resolvedEmpty": 0, "resolvedUnits": [], "resolutionNotes": "Counted at the depot"},
            headers=_admin_bearer(admin),
        )

    def approves(client):
        """What the staff bot sends when Olim taps Approve on Kamol's request."""
        return client.post(
            APPROVE_JOIN, json={"session_id": owner_trip_id, "requester_id": receiver_id}, headers=owner_headers
        )

    if first == "resolve":
        (resolve_status, resolve_body), (approve_status, approve_body) = _count_held_after_its_delivery_person_lock(
            pg_app, pg_db, monkeypatch, counts=resolves, approves=approves
        )
    else:
        (approve_status, approve_body), (resolve_status, resolve_body) = _double_tap(
            pg_app,
            pg_db,
            monkeypatch,
            held_method="_assert_can_join",
            first_tap=approves,
            second_tap=resolves,
        )

    pg_db.session.expire_all()
    his_open_trips = DriverBottleSession.query.filter_by(
        driver_user_id=receiver_id, status=DriverBottleSessionStatus.OPEN
    ).all()
    his_memberships = DriverSessionMembership.query.filter_by(
        member_driver_id=receiver_id, status=DriverSessionMembershipStatus.ACTIVE
    ).all()
    if first == "resolve":
        assert resolve_status == 200, resolve_body
        assert resolve_body["data"]["status"] == "resolved"
        assert approve_status == 409, approve_body
        assert approve_body["error_code"] == "BOTTLE_INVITEE_HAS_SESSION"
        [opened] = his_open_trips
        assert resolve_body["data"]["receiver_session_id"] == opened.id
        assert (opened.bottles_loaded, opened.bottles_transferred_in) == (0, 5)
        assert his_memberships == []
    else:
        assert approve_status == 200, approve_body
        assert (approve_body["data"]["session_id"], approve_body["data"]["status"]) == (owner_trip_id, "active")
        assert resolve_status == 409, resolve_body
        assert (resolve_body["error_code"], resolve_body["details"]) == (
            "BOTTLE_SESSION_MEMBERSHIP_ALREADY_ACTIVE",
            {"session_id": owner_trip_id, "owner_user_id": owner_id, "owner_name": "Olim"},
        )
        assert his_open_trips == []
        assert [m.session_id for m in his_memberships] == [owner_trip_id]
        refused = pg_db.session.get(DriverBottleTransfer, transfer_id)
        assert (refused.status, refused.receiver_session_id) == (DriverBottleTransferStatus.DISPUTED, None)
        assert pg_db.session.get(DriverBottleSession, owner_trip_id).bottles_transferred_in == 0
        assert pg_db.session.get(DriverBottleSession, sender_trip_id).bottles_transferred_out == 5


# --------------------------------------------------------------------------- #
# The receiver's Reject (spec 2026-11-18 §6.7, call 10)
# --------------------------------------------------------------------------- #


def _sender_events(enqueued, sender_telegram_id):  # noqa: F811
    """The pushes to the sender, as `(event, payload)`."""
    return [(event, payload) for chat, event, payload in pushes(enqueued) if chat == sender_telegram_id]


@pytest.mark.parametrize("first", ["reject", "confirm"])
def test_reject_and_confirm_of_one_transfer(pg_app, pg_db, monkeypatch, enqueued, first):  # noqa: F811
    """Aziz (12 full, 4 empties, 8 x 10 L) hands Bobur 10 full + 4 empties + 6 x 10 L. Bobur, on his own trip, taps
    Reject on one phone and Confirm on another. Both lock the transfer row before its PENDING check, so whichever comes
    second waits, reads what the first wrote and is refused with BOTTLE_TRANSFER_NOT_PENDING; the counters are the
    winner's. Every answer is asserted exactly: none is a 500, and no deadlock (40P01) aborted either side."""
    slack = _spy(monkeypatch, slack_alerts, "send_slack_alert", result=True)
    sender = trip_driver(pg_db, "Aziz", telegram_id="900000171")
    receiver = trip_driver(pg_db, "Bobur", telegram_id="900000172")
    small_id = ten_litre(pg_db, trip_category(pg_db)).id
    sender_trip_id = trip(pg_db, sender, bottles=12, units={small_id: 8}, collected=4).id
    landing_id = trip(pg_db, receiver, bottles=5).id
    transfer_id = (
        BottleTrackingService()
        .initiate_bottle_transfer(sender.id, receiver.id, full_quantity=10, empty_quantity=4, units={small_id: 6})
        .id
    )
    receiver_headers = _bearer(receiver)

    def rejects(client):
        """The staff bot's Reject: no body."""
        return client.post(REJECT.format(transfer_id), headers=receiver_headers)

    def confirms(client):
        """The receiver's one-tap Confirm."""
        return client.post(CONFIRM.format(transfer_id), json={"confirmAsDeclared": True}, headers=receiver_headers)

    if first == "reject":
        # Held right after the reject has locked the transfer, then the sender's trip, before it writes anything.
        (reject_status, reject_body), (confirm_status, confirm_body) = _double_tap(
            pg_app, pg_db, monkeypatch, held_method="_lock_session", first_tap=rejects, second_tap=confirms
        )
    else:
        # Held right after the confirm's PENDING check passed, where it picks the landing trip, before it credits.
        (confirm_status, confirm_body), (reject_status, reject_body) = _double_tap(
            pg_app, pg_db, monkeypatch, held_method="_receiver_session_for", first_tap=confirms, second_tap=rejects
        )

    pg_db.session.expire_all()
    sent_from = pg_db.session.get(DriverBottleSession, sender_trip_id)
    landing = pg_db.session.get(DriverBottleSession, landing_id)
    transfer = pg_db.session.get(DriverBottleTransfer, transfer_id)
    out_line = DriverBottleSessionUnit.query.filter_by(session_id=sender_trip_id, product_id=small_id).one()
    landed_units = [
        line.transferred_in for line in DriverBottleSessionUnit.query.filter_by(session_id=landing_id).all()
    ]
    if first == "reject":
        assert reject_status == 200, reject_body
        assert (reject_body["data"]["status"], reject_body["data"]["sender_trip_was_closed"]) == ("rejected", False)
        assert confirm_status == 409, confirm_body
        assert confirm_body["error_code"] == "BOTTLE_TRANSFER_NOT_PENDING"
        assert (transfer.status, transfer.receiver_session_id) == (DriverBottleTransferStatus.REJECTED, None)
        assert (sent_from.bottles_transferred_out, sent_from.empties_transferred_out, out_line.transferred_out) == (
            0,
            0,
            0,
        )
        assert (landing.bottles_transferred_in, landing.empties_transferred_in, landed_units) == (0, 0, [])
        assert [event for event, _payload in _sender_events(enqueued, 900000171)] == ["transfer_rejected"]
        assert len(slack) == 1
    else:
        assert confirm_status == 200, confirm_body
        assert confirm_body["data"]["status"] == "confirmed"
        assert reject_status == 409, reject_body
        assert reject_body["error_code"] == "BOTTLE_TRANSFER_NOT_PENDING"
        assert (transfer.status, transfer.receiver_session_id) == (DriverBottleTransferStatus.CONFIRMED, landing_id)
        assert (sent_from.bottles_transferred_out, sent_from.empties_transferred_out, out_line.transferred_out) == (
            10,
            4,
            6,
        )
        assert (landing.bottles_transferred_in, landing.empties_transferred_in, landed_units) == (10, 4, [6])
        assert [event for event, _payload in _sender_events(enqueued, 900000171)] == ["transfer_confirmed"]
        assert slack == []


@pytest.mark.parametrize("first", ["reject", "close"])
def test_reject_and_the_senders_close(pg_app, pg_db, monkeypatch, enqueued, first):  # noqa: F811
    """Aziz (7 full, 3 empties, 6 x 10 L) handed Bobur 5 full + 2 empties + 4 x 10 L, but Bobur never took them: Bobur
    taps Reject while Aziz, back at the depot, closes his trip returning everything (10 bottles, 6 x 10 L). Both lock
    the trip row, so whichever comes second waits. Behind a reject the close seals the restored counters; behind a
    close the reject puts the units back on the closed trip and recomputes both its discrepancies. Either way the trip
    ends closed with no discrepancy. Every answer is asserted exactly: none is a 500, and no deadlock (40P01)."""
    slack = _spy(monkeypatch, slack_alerts, "send_slack_alert", result=True)
    sender = trip_driver(pg_db, "Aziz", telegram_id="900000181")
    receiver = trip_driver(pg_db, "Bobur", telegram_id="900000182")
    small_id = ten_litre(pg_db, trip_category(pg_db)).id
    sender_trip_id = trip(pg_db, sender, bottles=7, units={small_id: 6}, collected=3).id
    transfer_id = (
        BottleTrackingService()
        .initiate_bottle_transfer(sender.id, receiver.id, full_quantity=5, empty_quantity=2, units={small_id: 4})
        .id
    )
    sender_headers, receiver_headers = _bearer(sender), _bearer(receiver)

    def rejects(client):
        """The staff bot's Reject: no body."""
        return client.post(REJECT.format(transfer_id), headers=receiver_headers)

    def closes(client):
        """The close's body: the 18.9 L count and one return per line on the trip."""
        body = {"bottles_returned_to_warehouse": 10, "units_returned": [{"product_id": small_id, "quantity": 6}]}
        return client.post(CLOSE_SESSION, json=body, headers=sender_headers)

    if first == "reject":
        # Held right after the reject has locked the transfer, then the trip, before it writes anything.
        (reject_status, reject_body), (close_status, close_body) = _double_tap(
            pg_app, pg_db, monkeypatch, held_method="_lock_session", first_tap=rejects, second_tap=closes
        )
    else:
        # Held inside the close after it has read (and locked) the trip, before it marks anything.
        (close_status, close_body), (reject_status, reject_body) = _double_tap(
            pg_app,
            pg_db,
            monkeypatch,
            held_method="_release_open_bindings_for_session",
            first_tap=closes,
            second_tap=rejects,
        )

    assert "deadlock" not in str(reject_body), reject_body
    assert "deadlock" not in str(close_body), close_body
    assert close_status == 200, close_body
    assert reject_status == 200, reject_body
    trip_was_closed = first == "close"
    assert (reject_body["data"]["status"], reject_body["data"]["sender_trip_was_closed"]) == (
        "rejected",
        trip_was_closed,
    )
    # The close sealed what it read: the counters the reject restored, or the transfer still out (7 bottles too many).
    assert close_body["data"]["discrepancy"] == (-7 if trip_was_closed else 0)
    pg_db.session.expire_all()
    sealed = pg_db.session.get(DriverBottleSession, sender_trip_id)
    line = DriverBottleSessionUnit.query.filter_by(session_id=sender_trip_id, product_id=small_id).one()
    assert (sealed.status, sealed.bottles_transferred_out, sealed.empties_transferred_out, sealed.discrepancy) == (
        DriverBottleSessionStatus.CLOSED,
        0,
        0,
        0,
    )
    assert (line.transferred_out, line.returned, line.discrepancy) == (0, 6, 0)
    assert pg_db.session.get(DriverBottleTransfer, transfer_id).status == DriverBottleTransferStatus.REJECTED
    assert [
        (event, payload["sender_trip_was_closed"]) for event, payload in _sender_events(enqueued, 900000181)
    ] == [("transfer_rejected", trip_was_closed)]
    assert len(slack) == 1
    # Full bottles back on a trip still open re-offer the pool; a closed trip's are at the depot.
    assert reoffers(enqueued) == (0 if trip_was_closed else 1)


@pytest.mark.parametrize("first", ["reject", "auto_close"])
def test_reject_of_one_and_confirm_of_another_from_the_same_trip(
    pg_app, pg_db, monkeypatch, enqueued, first  # noqa: F811
):
    """Aziz (10 full, 4 x 10 L) handed 5 full + 2 x 10 L to Bobur and the rest to Kamol, so his truck reads empty.
    Bobur rejects his while Kamol confirms his; that confirm would close Aziz's emptied trip on its own (T4). The
    auto-close judges the trip under its lock: behind the reject it reads Bobur's units back on board, and ahead of the
    reject (which waits for the trip lock) it still reads Bobur's transfer pending. Either way the trip stays open, with
    Bobur's units back on it. Every answer is asserted exactly: none is a 500, and no deadlock (40P01)."""
    slack = _spy(monkeypatch, slack_alerts, "send_slack_alert", result=True)
    sender = trip_driver(pg_db, "Aziz", telegram_id="900000191")
    rejecter = trip_driver(pg_db, "Bobur", telegram_id="900000192")
    confirmer = trip_driver(pg_db, "Kamol", telegram_id="900000193")
    small_id = ten_litre(pg_db, trip_category(pg_db)).id
    sender_trip_id = trip(pg_db, sender, bottles=10, units={small_id: 4}).id
    trip(pg_db, confirmer, bottles=3)
    service = BottleTrackingService()
    rejected_id = service.initiate_bottle_transfer(sender.id, rejecter.id, full_quantity=5, units={small_id: 2}).id
    confirmed_id = service.initiate_bottle_transfer(sender.id, confirmer.id, full_quantity=5, units={small_id: 2}).id
    rejecter_headers, confirmer_headers = _bearer(rejecter), _bearer(confirmer)

    def rejects(client):
        """The staff bot's Reject: no body."""
        return client.post(REJECT.format(rejected_id), headers=rejecter_headers)

    def confirms(client):
        """The receiver's one-tap Confirm."""
        return client.post(CONFIRM.format(confirmed_id), json={"confirmAsDeclared": True}, headers=confirmer_headers)

    if first == "reject":
        # Held right after the reject has locked its transfer, then the trip; the confirm's count commits, and its
        # auto-close then waits for the trip.
        (reject_status, reject_body), (confirm_status, confirm_body) = _double_tap(
            pg_app, pg_db, monkeypatch, held_method="_lock_session", first_tap=rejects, second_tap=confirms
        )
    else:
        # The confirm's count takes no trip lock through `_lock_session`; its auto-close does, after the count has
        # committed. Held right there, before it judges the trip; the reject then waits for the trip.
        (confirm_status, confirm_body), (reject_status, reject_body) = _double_tap(
            pg_app, pg_db, monkeypatch, held_method="_lock_session", first_tap=confirms, second_tap=rejects
        )

    assert "deadlock" not in str(reject_body), reject_body
    assert "deadlock" not in str(confirm_body), confirm_body
    assert reject_status == 200, reject_body
    assert (reject_body["data"]["status"], reject_body["data"]["sender_trip_was_closed"]) == ("rejected", False)
    assert confirm_status == 200, confirm_body
    assert confirm_body["data"]["status"] == "confirmed"
    pg_db.session.expire_all()
    still_open = pg_db.session.get(DriverBottleSession, sender_trip_id)
    line = DriverBottleSessionUnit.query.filter_by(session_id=sender_trip_id, product_id=small_id).one()
    assert (still_open.status, still_open.bottles_transferred_out, line.transferred_out, line.on_board) == (
        DriverBottleSessionStatus.OPEN,
        5,
        2,
        2,
    )
    # Aziz hears of both, his trip closed in neither. Both threads publish once the hold is released, in either order.
    told = [
        (event, payload["auto_closed"] if event == "transfer_confirmed" else payload["sender_trip_was_closed"])
        for event, payload in _sender_events(enqueued, 900000191)
    ]
    assert sorted(told) == [("transfer_confirmed", False), ("transfer_rejected", False)]
    assert len(slack) == 1
