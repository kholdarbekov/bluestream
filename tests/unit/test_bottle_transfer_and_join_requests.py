"""Bottle transfers to any active driver, and owner-approved join requests.

Driven through the real HTTP endpoints the staff bot calls, with real JWTs.
Only the Celery publish is spied on, with the signature-enforcing `_spy`.
"""
from datetime import UTC, datetime

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.bottle import DriverBottleSession, DriverBottleTransfer, DriverSessionMembership
from business_app.models.delivery import DeliveryPerson
from business_app.models.user import User
from business_app.tasks import staff_tasks
from tests.unit.test_sales_agent_tasks import _spy
from shared.enums import (
    DriverBottleSessionStatus,
    DriverSessionMembershipStatus,
    UserRole,
    UserStatus,
    UserType,
)

pytestmark = pytest.mark.unit

RECIPIENTS = "/api/v1/staff/bottles/transfers/recipients"
TRANSFERS = "/api/v1/staff/bottles/transfers"
INVITABLE = "/api/v1/staff/bottles/sessions/available-drivers"


def _headers(app, user_id: int) -> dict:
    with app.app_context():
        token = create_access_token(identity=str(user_id))
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _driver(db, phone, first, *, role=UserRole.DELIVERY_DRIVER, staff_roles=None,
            delivery_active=True, status=UserStatus.ACTIVE):
    extra = {"staff_roles": staff_roles} if staff_roles is not None else {}
    user = User(
        phone=phone, first_name=first, last_name="Driver", password_hash="x",
        role=role, user_type=UserType.STAFF, is_verified=True, telegram_id=phone[-9:],
        status=status, **extra,
    )
    db.session.add(user)
    db.session.flush()
    db.session.add(DeliveryPerson(
        user_id=user.id, full_name=f"{first} Driver", phone=phone, is_active=delivery_active,
        is_available=True, current_location_lat=41.30, current_location_lng=69.25,
        last_location_update=datetime.now(UTC),
    ))
    db.session.commit()
    return user


def _open_session(db, driver, loaded):
    session = DriverBottleSession(
        driver_user_id=driver.id, bottles_loaded=loaded, status=DriverBottleSessionStatus.OPEN,
    )
    db.session.add(session)
    db.session.commit()
    return session


def _member(db, session, driver):
    membership = DriverSessionMembership(
        session_id=session.id, session_owner_id=session.driver_user_id,
        member_driver_id=driver.id, status=DriverSessionMembershipStatus.ACTIVE,
    )
    db.session.add(membership)
    db.session.commit()
    return membership


# ---------------------------------------------------------------------------
# Task 1: one receiver rule, listed and enforced
# ---------------------------------------------------------------------------


def test_the_recipient_list_offers_exactly_the_drivers_a_transfer_accepts(app, client, db):
    sender = _driver(db, "+998901200001", "Sender")
    owner = _driver(db, "+998901200002", "Owner")
    elsewhere = _driver(db, "+998901200003", "Elsewhere")
    idle = _driver(db, "+998901200004", "Idle")
    mate = _driver(db, "+998901200005", "Mate")
    extra_role = _driver(db, "+998901200006", "Extra", role=UserRole.OPERATOR,
                         staff_roles=["operator", "delivery_driver"])
    benched = _driver(db, "+998901200007", "Benched", delivery_active=False)
    suspended = _driver(db, "+998901200008", "Suspended", status=UserStatus.INACTIVE)
    sender_session = _open_session(db, sender, 10)
    owner_session = _open_session(db, owner, 5)
    _member(db, owner_session, elsewhere)
    _member(db, sender_session, mate)

    listed = client.get(RECIPIENTS, headers=_headers(app, sender.id))

    assert listed.status_code == 200, listed.get_json()
    rows = {row["user_id"]: row for row in listed.get_json()["data"]}
    assert {uid: row["bottle_status"] for uid, row in rows.items()} == {
        owner.id: "own_session",
        elsewhere.id: "co_driver",
        idle.id: "none",
        extra_role.id: "none",
    }
    assert rows[owner.id]["name"] == "Owner Driver"
    assert rows[owner.id]["phone"] == "+998901200002"

    for candidate in (owner, elsewhere, idle, extra_role, mate, benched, suspended):
        sent = client.post(TRANSFERS, headers=_headers(app, sender.id),
                           json={"receiver_driver_id": candidate.id, "quantity": 1})
        if candidate.id in rows:
            assert sent.status_code == 201, (candidate.first_name, sent.get_json())
        else:
            assert sent.status_code == 400, (candidate.first_name, sent.get_json())
            assert sent.get_json()["error_code"] == "BOTTLE_TRANSFER_RECEIVER_INVALID"

    # Four accepted sends of one bottle each, three refused: refusals took nothing off the truck.
    db.session.refresh(sender_session)
    assert sender_session.bottles_transferred_out == 4


def test_a_recipient_deactivated_after_the_list_was_drawn_is_refused_and_nothing_leaves_the_truck(app, client, db):
    sender = _driver(db, "+998901200011", "Sender")
    receiver = _driver(db, "+998901200012", "Receiver")
    sender_session = _open_session(db, sender, 10)
    assert receiver.id in {r["user_id"] for r in client.get(RECIPIENTS, headers=_headers(app, sender.id)).get_json()["data"]}

    DeliveryPerson.query.filter_by(user_id=receiver.id).one().is_active = False
    db.session.commit()

    sent = client.post(TRANSFERS, headers=_headers(app, sender.id),
                       json={"receiver_driver_id": receiver.id, "quantity": 3})
    assert sent.status_code == 400, sent.get_json()
    assert sent.get_json()["error_code"] == "BOTTLE_TRANSFER_RECEIVER_INVALID"
    db.session.refresh(sender_session)
    assert (sender_session.bottles_transferred_out or 0) == 0
    assert DriverBottleTransfer.query.count() == 0


def test_the_invite_list_now_sees_extra_role_drivers_and_drops_deactivated_ones(app, client, db):
    owner = _driver(db, "+998901200021", "Owner")
    extra_role = _driver(db, "+998901200022", "Extra", role=UserRole.OPERATOR,
                         staff_roles=["operator", "delivery_driver"])
    benched = _driver(db, "+998901200023", "Benched", delivery_active=False)
    _open_session(db, owner, 5)

    listed = client.get(INVITABLE, headers=_headers(app, owner.id))

    assert listed.status_code == 200, listed.get_json()
    ids = {row["user_id"] for row in listed.get_json()["data"]}
    assert extra_role.id in ids
    assert benched.id not in ids


# ---------------------------------------------------------------------------
# Task 2: where confirmed bottles land
# ---------------------------------------------------------------------------


def _send(client, app, sender, receiver, qty):
    sent = client.post(TRANSFERS, headers=_headers(app, sender.id),
                       json={"receiver_driver_id": receiver.id, "quantity": qty})
    assert sent.status_code == 201, sent.get_json()
    return sent.get_json()["data"]


def _confirm(client, app, transfer_id, receiver, qty):
    return client.post(f"{TRANSFERS}/{transfer_id}/confirm", headers=_headers(app, receiver.id),
                       json={"confirmed_quantity": qty})


def _open_sessions_of(driver):
    return DriverBottleSession.query.filter_by(
        driver_user_id=driver.id, status=DriverBottleSessionStatus.OPEN,
    ).all()


def test_a_receiver_with_their_own_session_is_credited_on_it(app, client, db):
    sender = _driver(db, "+998901200101", "Sender")
    owner = _driver(db, "+998901200102", "Owner")
    _open_session(db, sender, 10)
    owner_session = _open_session(db, owner, 5)
    transfer = _send(client, app, sender, owner, 4)

    confirmed = _confirm(client, app, transfer["id"], owner, 4)

    assert confirmed.status_code == 200, confirmed.get_json()
    assert confirmed.get_json()["data"]["status"] == "confirmed"
    db.session.refresh(owner_session)
    assert (owner_session.bottles_transferred_in, owner_session.current_inventory) == (4, 9)
    assert DriverBottleTransfer.query.get(transfer["id"]).receiver_session_id == owner_session.id


def test_a_co_driver_receiver_is_credited_on_the_shared_session_and_stays_a_co_driver(app, client, db):
    sender = _driver(db, "+998901200111", "Sender")
    owner = _driver(db, "+998901200112", "Owner")
    codriver = _driver(db, "+998901200113", "Codriver")
    _open_session(db, sender, 10)
    shared = _open_session(db, owner, 5)
    membership = _member(db, shared, codriver)
    transfer = _send(client, app, sender, codriver, 3)

    confirmed = _confirm(client, app, transfer["id"], codriver, 3)

    assert confirmed.status_code == 200, confirmed.get_json()
    db.session.refresh(shared)
    assert shared.bottles_transferred_in == 3
    assert _open_sessions_of(codriver) == []
    assert DriverSessionMembership.query.get(membership.id).status == DriverSessionMembershipStatus.ACTIVE


def test_a_receiver_with_nothing_open_gets_a_session_that_starts_with_the_hand_off(app, client, db):
    sender = _driver(db, "+998901200121", "Sender")
    idle = _driver(db, "+998901200122", "Idle")
    _open_session(db, sender, 10)
    transfer = _send(client, app, sender, idle, 4)

    confirmed = _confirm(client, app, transfer["id"], idle, 4)

    assert confirmed.status_code == 200, confirmed.get_json()
    [opened] = _open_sessions_of(idle)
    assert (opened.bottles_loaded, opened.bottles_transferred_in, opened.current_inventory) == (0, 4, 4)
    assert opened.notes == f"Opened by transfer {transfer['transfer_ref']}"
    assert DriverBottleTransfer.query.get(transfer["id"]).receiver_session_id == opened.id
    # A hand-off happens on the road: the stored position is NOT moved to the depot.
    person = DeliveryPerson.query.filter_by(user_id=idle.id).one()
    assert (person.current_location_lat, person.current_location_lng) == (41.30, 69.25)

    # It is an ordinary session from here: back at the warehouse with 4, it balances.
    closed = client.post("/api/v1/staff/bottles/session/close", headers=_headers(app, idle.id),
                         json={"bottles_returned_to_warehouse": 4})
    assert closed.status_code == 200, closed.get_json()
    assert closed.get_json()["data"]["discrepancy"] == 0


def test_two_confirms_in_a_row_land_on_the_one_session_the_first_opened(app, client, db):
    sender = _driver(db, "+998901200131", "Sender")
    idle = _driver(db, "+998901200132", "Idle")
    _open_session(db, sender, 10)
    first = _send(client, app, sender, idle, 2)
    second = _send(client, app, sender, idle, 3)

    assert _confirm(client, app, first["id"], idle, 2).status_code == 200
    assert _confirm(client, app, second["id"], idle, 3).status_code == 200

    [opened] = _open_sessions_of(idle)
    assert opened.bottles_transferred_in == 5


def test_a_receiver_with_nothing_open_who_counts_zero_opens_nothing_and_the_admin_decides(app, client, db):
    sender = _driver(db, "+998901200141", "Sender")
    idle = _driver(db, "+998901200142", "Idle")
    _open_session(db, sender, 10)
    transfer = _send(client, app, sender, idle, 4)

    confirmed = _confirm(client, app, transfer["id"], idle, 0)

    assert confirmed.status_code == 200, confirmed.get_json()
    assert confirmed.get_json()["data"]["status"] == "disputed"
    assert _open_sessions_of(idle) == []
    assert DriverBottleTransfer.query.get(transfer["id"]).receiver_session_id is None


def test_a_receiver_who_joined_the_senders_truck_after_the_send_nets_to_zero(app, client, db):
    sender = _driver(db, "+998901200151", "Sender")
    later_mate = _driver(db, "+998901200152", "Mate")
    sender_session = _open_session(db, sender, 10)
    transfer = _send(client, app, sender, later_mate, 3)
    _member(db, sender_session, later_mate)

    confirmed = _confirm(client, app, transfer["id"], later_mate, 3)

    assert confirmed.status_code == 200, confirmed.get_json()
    db.session.refresh(sender_session)
    assert (sender_session.bottles_transferred_out, sender_session.bottles_transferred_in) == (3, 3)
    assert sender_session.current_inventory == 10


# ---------------------------------------------------------------------------
# Task 2 fix round 1: an admin resolving a dispute lands bottles by the same rule
# ---------------------------------------------------------------------------


def _disputed_at_zero(client, app, db, phone_base, qty=4):
    sender = _driver(db, f"+99890120{phone_base}1", "Sender")
    idle = _driver(db, f"+99890120{phone_base}2", "Idle")
    sender_session = _open_session(db, sender, 10)
    transfer = _send(client, app, sender, idle, qty)
    confirmed = _confirm(client, app, transfer["id"], idle, 0)
    assert confirmed.get_json()["data"]["status"] == "disputed"
    return sender_session, idle, transfer


def _resolve(client, admin_claim_headers, transfer_id, qty):
    return client.post(
        f"/api/v1/admin/bottles/transfers/{transfer_id}/resolve",
        headers=admin_claim_headers,
        json={"resolved_quantity": qty, "resolution_notes": "counted at the depot"},
    )


def test_resolving_a_zero_count_dispute_opens_the_receivers_session_with_the_bottles(
    app, client, db, admin_claim_headers, admin_user
):
    sender_session, idle, transfer = _disputed_at_zero(client, app, db, "21")

    resolved = _resolve(client, admin_claim_headers, transfer["id"], 4)

    assert resolved.status_code == 200, resolved.get_json()
    [opened] = _open_sessions_of(idle)
    assert (opened.bottles_loaded, opened.bottles_transferred_in) == (0, 4)
    assert opened.loaded_by_user_id == admin_user.id
    assert DriverBottleTransfer.query.get(transfer["id"]).receiver_session_id == opened.id
    db.session.refresh(sender_session)
    assert sender_session.bottles_transferred_out == 4


def test_resolving_never_lands_the_bottles_on_a_truck_the_receiver_merely_co_drives(
    app, client, db, admin_claim_headers, admin_user
):
    sender_session, idle, transfer = _disputed_at_zero(client, app, db, "24")
    third = _driver(db, "+998901202493", "Third")
    third_session = _open_session(db, third, 6)
    _member(db, third_session, idle)

    resolved = _resolve(client, admin_claim_headers, transfer["id"], 4)

    assert resolved.status_code == 200, resolved.get_json()
    db.session.refresh(third_session)
    assert third_session.bottles_transferred_in == 0
    [opened] = _open_sessions_of(idle)
    assert (opened.bottles_loaded, opened.bottles_transferred_in) == (0, 4)
    assert opened.loaded_by_user_id == admin_user.id
    assert DriverBottleTransfer.query.get(transfer["id"]).receiver_session_id == opened.id


def test_resolving_a_zero_count_dispute_credits_a_session_the_receiver_has_since_opened(
    app, client, db, admin_claim_headers
):
    sender_session, idle, transfer = _disputed_at_zero(client, app, db, "22")
    own = _open_session(db, idle, 5)

    resolved = _resolve(client, admin_claim_headers, transfer["id"], 4)

    assert resolved.status_code == 200, resolved.get_json()
    assert [s.id for s in _open_sessions_of(idle)] == [own.id]
    db.session.refresh(own)
    assert own.bottles_transferred_in == 4
    assert DriverBottleTransfer.query.get(transfer["id"]).receiver_session_id == own.id


def test_resolving_a_zero_count_dispute_to_zero_opens_nothing_and_returns_the_senders_bottles(
    app, client, db, admin_claim_headers
):
    sender_session, idle, transfer = _disputed_at_zero(client, app, db, "23")

    resolved = _resolve(client, admin_claim_headers, transfer["id"], 0)

    assert resolved.status_code == 200, resolved.get_json()
    assert _open_sessions_of(idle) == []
    db.session.refresh(sender_session)
    assert sender_session.bottles_transferred_out == 0


# ---------------------------------------------------------------------------
# Task 4: the receiver hears about it
# ---------------------------------------------------------------------------


@pytest.fixture
def pushes(monkeypatch):
    return _spy(monkeypatch, staff_tasks.push_bottle_event, "delay")


def test_a_saved_transfer_is_pushed_to_the_receiver(app, client, db, pushes):
    sender = _driver(db, "+998901200201", "Ali")
    receiver = _driver(db, "+998901200202", "Vali")
    _open_session(db, sender, 10)

    transfer = _send(client, app, sender, receiver, 4)

    assert pushes == [((int(receiver.telegram_id), "transfer_received",
                        {"id": transfer["id"], "declared_quantity": 4, "sender_name": "Ali Driver"}), {})]


def test_a_refused_transfer_pushes_nothing(app, client, db, pushes):
    sender = _driver(db, "+998901200211", "Ali")
    receiver = _driver(db, "+998901200212", "Vali")
    _open_session(db, sender, 2)

    refused = client.post(TRANSFERS, headers=_headers(app, sender.id),
                          json={"receiver_driver_id": receiver.id, "quantity": 5})

    assert refused.status_code == 400, refused.get_json()
    assert pushes == []


# ---------------------------------------------------------------------------
# Task 6: join requests
# ---------------------------------------------------------------------------

JOINABLE = "/api/v1/staff/bottles/sessions/joinable"
JOIN_REQUEST = "/api/v1/staff/bottles/session/join-request"
APPROVE = f"{JOIN_REQUEST}/approve"
DECLINE = f"{JOIN_REQUEST}/decline"
INVITE = "/api/v1/staff/bottles/session/invite"


def _active_memberships(driver):
    return DriverSessionMembership.query.filter_by(
        member_driver_id=driver.id, status=DriverSessionMembershipStatus.ACTIVE,
    ).all()


def test_the_joinable_list_refuses_a_driver_who_cannot_join_anyway(app, client, db):
    owner = _driver(db, "+998901200301", "Owner")
    codriver = _driver(db, "+998901200302", "Codriver")
    free = _driver(db, "+998901200303", "Free")
    session = _open_session(db, owner, 5)
    _member(db, session, codriver)

    as_owner = client.get(JOINABLE, headers=_headers(app, owner.id))
    as_codriver = client.get(JOINABLE, headers=_headers(app, codriver.id))
    as_free = client.get(JOINABLE, headers=_headers(app, free.id))

    assert (as_owner.status_code, as_owner.get_json()["error_code"]) == (409, "BOTTLE_SESSION_ALREADY_OPEN")
    assert (as_codriver.status_code, as_codriver.get_json()["error_code"]) == (
        409, "BOTTLE_SESSION_MEMBERSHIP_ALREADY_ACTIVE")
    assert as_free.status_code == 200
    assert [row["session_id"] for row in as_free.get_json()["data"]] == [session.id]


def test_a_request_asks_the_owner_and_adds_nobody(app, client, db, pushes):
    owner = _driver(db, "+998901200311", "Ali")
    requester = _driver(db, "+998901200312", "Vali")
    session = _open_session(db, owner, 5)

    asked = client.post(JOIN_REQUEST, headers=_headers(app, requester.id), json={"session_id": session.id})

    assert asked.status_code == 200, asked.get_json()
    assert asked.get_json()["data"]["owner_name"] == "Ali Driver"
    assert pushes == [((int(owner.telegram_id), "join_requested",
                        {"session_id": session.id, "requester_id": requester.id,
                         "requester_name": "Vali Driver"}), {})]
    assert _active_memberships(requester) == []


def test_a_request_for_a_session_that_closed_meanwhile_is_refused_and_pushes_nothing(app, client, db, pushes):
    owner = _driver(db, "+998901200321", "Ali")
    requester = _driver(db, "+998901200322", "Vali")
    session = _open_session(db, owner, 5)
    session.status = DriverBottleSessionStatus.CLOSED
    db.session.commit()

    asked = client.post(JOIN_REQUEST, headers=_headers(app, requester.id), json={"session_id": session.id})

    assert (asked.status_code, asked.get_json()["error_code"]) == (400, "BOTTLE_SESSION_NOT_OPEN")
    assert pushes == []


def test_approve_adds_the_requester_records_the_owner_and_tells_the_requester_once(app, client, db, pushes):
    owner = _driver(db, "+998901200331", "Ali")
    requester = _driver(db, "+998901200332", "Vali")
    session = _open_session(db, owner, 5)
    decision = {"session_id": session.id, "requester_id": requester.id}

    approved = client.post(APPROVE, headers=_headers(app, owner.id), json=decision)
    again = client.post(APPROVE, headers=_headers(app, owner.id), json=decision)

    assert approved.status_code == 200, approved.get_json()
    assert approved.get_json()["data"]["member_name"] == "Vali Driver"
    assert again.status_code == 200, again.get_json()
    assert again.get_json()["data"]["id"] == approved.get_json()["data"]["id"]
    [membership] = _active_memberships(requester)
    assert (membership.session_id, membership.invited_by_user_id) == (session.id, owner.id)
    assert pushes == [((int(requester.telegram_id), "join_approved",
                        {"session_id": session.id, "owner_name": "Ali Driver"}), {})]


def test_approving_a_request_for_a_session_the_owner_closed_is_stale(app, client, db, pushes):
    owner = _driver(db, "+998901200341", "Ali")
    requester = _driver(db, "+998901200342", "Vali")
    old = _open_session(db, owner, 5)
    old.status = DriverBottleSessionStatus.CLOSED
    db.session.commit()
    _open_session(db, owner, 8)  # a NEW session: Vali never asked to join this one

    stale = client.post(APPROVE, headers=_headers(app, owner.id),
                        json={"session_id": old.id, "requester_id": requester.id})

    assert (stale.status_code, stale.get_json()["error_code"]) == (409, "BOTTLE_JOIN_REQUEST_STALE")
    assert _active_memberships(requester) == [] and pushes == []


def test_only_the_owner_can_answer(app, client, db, pushes):
    owner = _driver(db, "+998901200351", "Ali")
    stranger = _driver(db, "+998901200352", "Stranger")
    requester = _driver(db, "+998901200353", "Vali")
    session = _open_session(db, owner, 5)
    _open_session(db, stranger, 5)
    decision = {"session_id": session.id, "requester_id": requester.id}

    for path in (APPROVE, DECLINE):
        refused = client.post(path, headers=_headers(app, stranger.id), json=decision)
        assert (refused.status_code, refused.get_json()["error_code"]) == (409, "BOTTLE_JOIN_REQUEST_STALE"), path
    assert pushes == []


@pytest.mark.parametrize("meanwhile, code", [
    ("opened_own", "BOTTLE_INVITEE_HAS_SESSION"),
    ("joined_other", "BOTTLE_INVITEE_IN_OTHER_SESSION"),
])
def test_approving_a_requester_who_moved_on_is_refused_in_the_owners_words(app, client, db, pushes, meanwhile, code):
    owner = _driver(db, "+998901200361", "Ali")
    other = _driver(db, "+998901200362", "Other")
    requester = _driver(db, "+998901200363", "Vali")
    session = _open_session(db, owner, 5)
    if meanwhile == "opened_own":
        _open_session(db, requester, 3)
    else:
        _member(db, _open_session(db, other, 4), requester)

    refused = client.post(APPROVE, headers=_headers(app, owner.id),
                          json={"session_id": session.id, "requester_id": requester.id})

    assert (refused.status_code, refused.get_json()["error_code"]) == (409, code)
    assert pushes == []


def test_decline_tells_the_requester_and_adds_nobody(app, client, db, pushes):
    owner = _driver(db, "+998901200371", "Ali")
    requester = _driver(db, "+998901200372", "Vali")
    session = _open_session(db, owner, 5)

    declined = client.post(DECLINE, headers=_headers(app, owner.id),
                           json={"session_id": session.id, "requester_id": requester.id})

    assert declined.status_code == 200, declined.get_json()
    assert declined.get_json()["data"] == {"requester_id": requester.id, "requester_name": "Vali Driver"}
    assert _active_memberships(requester) == []
    assert pushes == [((int(requester.telegram_id), "join_declined",
                        {"session_id": session.id, "owner_name": "Ali Driver"}), {})]


def test_declining_a_duplicate_after_approving_never_tells_a_member_they_were_declined(app, client, db, pushes):
    owner = _driver(db, "+998901200381", "Ali")
    requester = _driver(db, "+998901200382", "Vali")
    session = _open_session(db, owner, 5)
    decision = {"session_id": session.id, "requester_id": requester.id}
    assert client.post(APPROVE, headers=_headers(app, owner.id), json=decision).status_code == 200
    pushes.clear()

    declined = client.post(DECLINE, headers=_headers(app, owner.id), json=decision)

    assert (declined.status_code, declined.get_json()["error_code"]) == (409, "BOTTLE_JOIN_REQUEST_ALREADY_MEMBER")
    assert pushes == []
    assert len(_active_memberships(requester)) == 1


def test_the_instant_join_is_gone_and_invite_still_works_and_records_the_inviter(app, client, db):
    owner = _driver(db, "+998901200391", "Ali")
    invitee = _driver(db, "+998901200392", "Vali")
    session = _open_session(db, owner, 5)

    gone = client.post("/api/v1/staff/bottles/session/join", headers=_headers(app, invitee.id),
                       json={"session_id": session.id})
    invited = client.post(INVITE, headers=_headers(app, owner.id), json={"member_driver_id": invitee.id})

    assert gone.status_code in (404, 405)
    assert invited.status_code == 201, invited.get_json()
    [membership] = _active_memberships(invitee)
    assert membership.invited_by_user_id == owner.id


@pytest.mark.parametrize("path", [APPROVE, DECLINE])
@pytest.mark.parametrize("who", ["missing", "deactivated"])
def test_answering_a_request_from_a_requester_who_is_gone_is_stale(app, client, db, pushes, path, who):
    owner = _driver(db, "+998901200401", "Ali")
    session = _open_session(db, owner, 5)
    if who == "missing":
        requester_id = 987654
    else:
        requester_id = _driver(db, "+998901200402", "Vali", delivery_active=False).id

    refused = client.post(path, headers=_headers(app, owner.id),
                          json={"session_id": session.id, "requester_id": requester_id})

    assert (refused.status_code, refused.get_json()["error_code"]) == (409, "BOTTLE_JOIN_REQUEST_STALE")
    assert DriverSessionMembership.query.filter_by(member_driver_id=requester_id).all() == []
    assert pushes == []
