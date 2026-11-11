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

`with_for_update` compiles to nothing on SQLite, so this runs on a real, fully-migrated Postgres database (`pg_app`,
the shared harness in tests/integration/conftest.py). The interleaving is forced, the way
test_on_the_way_concurrency_pg_e2e.py forces its own: the first tap is held right after the read its lock protects,
before it commits, and the second tap starts only then. Each runs on its own thread, app context, session and
connection, as two gunicorn workers would. Every wait is bounded, so a broken lock fails instead of hanging. Celery
publishing is the only thing faked (`enqueued`).

Run: docker run --rm --network bluestream_default --env-file .env -v "$(pwd):/app" -w /app -e FLASK_ENV=testing
-e TESTING=true bluestream-business_app sh -c 'pip install -q pytest-xdist >/dev/null 2>&1;
pytest tests/integration/test_bottle_transfer_races_pg_e2e.py -n0 --no-cov --no-header -q'
"""

import threading
import uuid

import pytest
from flask_jwt_extended import create_access_token
from sqlalchemy import text

from business_app.models.bottle import DriverBottleSession, DriverBottleTransfer, DriverSessionMembership
from business_app.services.bottle_tracking_service import BottleTrackingService
from shared.enums import DriverBottleSessionStatus, DriverBottleTransferStatus, UserRole, UserType
from tests.integration.test_customer_cancel_races_pg import _await_a_lock_waiter, _finish, _thread, _user
from tests.integration.test_on_the_way_concurrency_pg_e2e import _crew_member
from tests.integration.test_redispatch_reschedules_to_today import enqueued  # noqa: F401 -- a fixture

pytestmark = [pytest.mark.integration, pytest.mark.e2e]

TRANSFERS = "/api/v1/staff/bottles/transfers"
CONFIRM = TRANSFERS + "/{}/confirm"
RESOLVE = "/api/v1/admin/bottles/transfers/{}/resolve"
CLOSE_SESSION = "/api/v1/staff/bottles/session/close"
FORCE_CLOSE = "/api/v1/admin/bottles/sessions/{}/force-close"


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
    """Run `first_tap`, hold it right after `owner.<held_method>` returns, start `second_tap`, and prove the second
    waits on a lock the first holds. Returns each tap's `(status, body)`."""
    first_holds = threading.Event()
    release_the_first = threading.Event()
    original = getattr(owner, held_method)

    def held(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if threading.current_thread().name == "first" and not first_holds.is_set():
            first_holds.set()
            assert release_the_first.wait(timeout=30), "the test never released the first tap"
        return result

    monkeypatch.setattr(owner, held_method, held)
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
