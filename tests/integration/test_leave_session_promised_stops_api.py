"""A co-driver cannot leave the truck while he still owns stops its bottles are promised to (spec 2026-11-09 A8).

The crew's free full bottles subtract the stops its members have set off on (On the way, Arrived), counted by who owns
the delivery. A member who left took his stops out of that sum while their bottles stayed on the owner's truck, so the
owner could promise the same bottles again. The leave route now refuses with BOTTLE_SESSION_LEAVE_HAS_PROMISED_STOPS
and how many such stops he holds; Assigned and Picked-up stops hold no bottles (A10), so they never block a leave.

Driven through the staff bot's own routes with real JWTs: the Leave button's POST, the status taps that finish the
stops, and the session screen the owner reads.
"""

import pytest

from business_app.models.bottle import DriverBottleSession, DriverBottleSessionOrder, DriverSessionMembership
from shared.enums import DeliveryStatus, DriverBottleSessionStatus, DriverSessionMembershipStatus
from staff_bot.api_client import StaffAPIClient
from tests.unit.test_bottle_transfer_and_join_requests import _driver, _headers
from tests.unit.test_staff_error_endpoints import _assigned_delivery, _order_with_bottles

pytestmark = pytest.mark.integration

LEAVE = "/api/v1/staff/bottles/session/leave"
CURRENT_SESSION = "/api/v1/staff/bottles/session/current"
STATUS = "/api/v1/staff/delivery/{}/status"


def _crew(db):
    """The owner's trip, loaded with 10 full bottles, and his co-driver, an ACTIVE member."""
    owner = _driver(db, "+998901271001", "Owner")
    member = _driver(db, "+998901271002", "Member")
    trip = DriverBottleSession(driver_user_id=owner.id, bottles_loaded=10, status=DriverBottleSessionStatus.OPEN)
    db.session.add(trip)
    db.session.flush()
    membership = DriverSessionMembership(
        session_id=trip.id,
        session_owner_id=owner.id,
        member_driver_id=member.id,
        status=DriverSessionMembershipStatus.ACTIVE,
    )
    db.session.add(membership)
    db.session.commit()
    return owner, member, trip, membership


def _members_stop(db, customer, product, member, trip, *, bottles, status):
    """A `bottles`-bottle stop the member accepted onto the crew's trip, at `status`."""
    order = _order_with_bottles(db, customer, product, quantity=bottles)
    db.session.add(DriverBottleSessionOrder(session_id=trip.id, order_id=order.id, accepted_by_driver_id=member.id))
    return _assigned_delivery(db, order, member, status=status)


def _free_full(client, app, driver):
    """The free full bottles the session screen shows this driver."""
    response = client.get(CURRENT_SESSION, headers=_headers(app, driver.id))
    assert response.status_code == 200, response.get_json()
    return response.get_json()["data"]["free_full_bottles"]


def _tap(client, app, driver, delivery_id, status):
    response = client.put(
        STATUS.format(delivery_id), headers=_headers(app, driver.id), json=StaffAPIClient.delivery_status_body(status)
    )
    assert response.status_code == 200, response.get_json()


def test_a_member_with_stops_on_their_way_cannot_leave_until_he_has_delivered_them(
    app, client, db, sample_user, sample_product, monkeypatch
):
    monkeypatch.setitem(app.config, "BOTTLE_SESSION_ENFORCEMENT_STRICT", True)
    owner, member, trip, membership = _crew(db)
    on_the_way = _members_stop(
        db, sample_user, sample_product, member, trip, bottles=4, status=DeliveryStatus.IN_TRANSIT
    )
    at_the_door = _members_stop(db, sample_user, sample_product, member, trip, bottles=3, status=DeliveryStatus.ARRIVED)
    assert _free_full(client, app, owner) == 10 - 4 - 3

    refused = client.post(LEAVE, headers=_headers(app, member.id))

    assert refused.status_code == 409, refused.get_json()
    body = refused.get_json()
    assert (body["error_code"], body["details"]) == ("BOTTLE_SESSION_LEAVE_HAS_PROMISED_STOPS", {"count": 2})
    db.session.refresh(membership)
    assert membership.status == DriverSessionMembershipStatus.ACTIVE
    assert _free_full(client, app, owner) == 3

    _tap(client, app, member, at_the_door.id, "delivered")
    still_refused = client.post(LEAVE, headers=_headers(app, member.id))
    assert still_refused.status_code == 409, still_refused.get_json()
    assert still_refused.get_json()["details"] == {"count": 1}

    _tap(client, app, member, on_the_way.id, "arrived")
    _tap(client, app, member, on_the_way.id, "delivered")
    left = client.post(LEAVE, headers=_headers(app, member.id))

    assert left.status_code == 200, left.get_json()
    assert left.get_json()["data"]["status"] == "left"
    db.session.refresh(membership)
    assert membership.status == DriverSessionMembershipStatus.LEFT
    # The 7 bottles left the truck at the doors, so the owner's free full is what is on board.
    db.session.refresh(trip)
    assert (trip.full_bottles_on_board, _free_full(client, app, owner)) == (3, 3)


@pytest.mark.parametrize("held", [DeliveryStatus.ASSIGNED, DeliveryStatus.PICKED_UP], ids=lambda s: s.value)
def test_a_member_whose_stops_hold_no_bottles_yet_may_leave(app, client, db, sample_user, sample_product, held):
    owner, member, trip, membership = _crew(db)
    _members_stop(db, sample_user, sample_product, member, trip, bottles=4, status=held)

    left = client.post(LEAVE, headers=_headers(app, member.id))

    assert left.status_code == 200, left.get_json()
    db.session.refresh(membership)
    assert membership.status == DriverSessionMembershipStatus.LEFT
    assert _free_full(client, app, owner) == 10
