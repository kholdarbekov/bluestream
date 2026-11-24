"""The receiver rejects a water transfer (spec 2026-11-18 §6.7 "Reject", calls 9 and 10).

Only the named receiver, only while the transfer is PENDING: another driver (the sender included) gets
BOTTLE_TRANSFER_NOT_RECEIVER, a confirmed, disputed, resolved or rejected transfer BOTTLE_TRANSFER_NOT_PENDING. The
units go back to the trip they left: every line's `transferred_out` on the sender's trip drops by what it declared
(full, empties, each 10 L line), even on a closed or force-closed trip, whose two discrepancies are then recomputed.
The receiver is credited nothing and nothing opens for him. The sender gets a push, Slack gets a post every time, and
full bottles back on an OPEN trip re-offer the pool (empties or 10 L alone do not). The office never resolves a
rejected transfer.

Driven through the routes the staff bot and the admin pages call, with the bodies they send. Only the outbound calls
are recorded: the Celery publish (`enqueued`) and the Slack post (`slack`).
"""

import logging
from datetime import datetime

import pytest

from business_app.models.bottle import DriverBottleSession, DriverBottleSessionUnit, DriverBottleTransfer
from business_app.services.bottle_tracking_service import BottleTrackingService
from shared.enums import DriverBottleSessionStatus, DriverBottleTransferStatus
from tests.integration.reorder_reminder_builders import captured_logs
from tests.integration.test_redispatch_reschedules_to_today import enqueued  # noqa: F401 -- a fixture
from tests.integration.test_transfer_auto_close_and_alerts import TASHKENT, _posted_alert
from tests.integration.trip_unit_builders import (  # noqa: F401  (fixtures)
    driver,
    pushes,
    receiver,
    reoffers,
    sender,
    slack,
    small,
    staff_headers,
    trip,
)

pytestmark = pytest.mark.integration

TRANSFERS = "/api/v1/staff/bottles/transfers"
CONFIRM = TRANSFERS + "/{}/confirm"
REJECT = TRANSFERS + "/{}/reject"
CLOSE = "/api/v1/staff/bottles/session/close"
FORCE_CLOSE = "/api/v1/admin/bottles/sessions/{}/force-close"
RESOLVE = "/api/v1/admin/bottles/transfers/{}/resolve"


def _sent(client, giver, taker, *, full=0, empty=0, units=None):
    """The staff bot's send body (both numbers, then one `{product_id, quantity}` per 10 L line); its transfer's id."""
    body = {"receiver_driver_id": taker.id, "full_quantity": full, "empty_quantity": empty}
    if units is not None:
        body["units"] = [{"product_id": product_id, "quantity": quantity} for product_id, quantity in units.items()]
    sent = client.post(TRANSFERS, json=body, headers=staff_headers(giver))
    assert sent.status_code == 201, sent.get_data(as_text=True)
    return sent.get_json()["data"]["id"]


def _reject(client, taker, transfer_id):
    """The staff bot's Reject: no body."""
    return client.post(REJECT.format(transfer_id), headers=staff_headers(taker))


def _moved_out(db, session_id):
    """The sender's trip as `(full out, empties out, each line's out)`, read fresh."""
    db.session.expire_all()
    session = db.session.get(DriverBottleSession, session_id)
    return (
        session.bottles_transferred_out,
        session.empties_transferred_out,
        [line.transferred_out for line in session.unit_lines],
    )


def _status_of(db, transfer_id):
    db.session.expire_all()
    return db.session.get(DriverBottleTransfer, transfer_id).status


def _lines(small, *, full, empty, units):  # noqa: F811
    """The transfer's `lines` as published before anyone counted them: full, empty, then the 10 L line."""
    return [
        {
            "kind": "full",
            "product_id": None,
            "product_name": None,
            "declared": full,
            "confirmed": None,
            "resolved": None,
        },
        {
            "kind": "empty",
            "product_id": None,
            "product_name": None,
            "declared": empty,
            "confirmed": None,
            "resolved": None,
        },
        {
            "kind": "product",
            "product_id": small.id,
            "product_name": "10 Litr suv",
            "declared": units,
            "confirmed": None,
            "resolved": None,
        },
    ]


def _reject_alert(ref, *, sender, receiver, lines, trip_line, stamp):  # noqa: F811
    """The Slack alert, line for line, written like `_dispute_alert`. `stamp` is the Time line's local time; the rest
    is fixed by the transfer and the sender's trip."""
    return (
        f"❌ *Bottle transfer rejected* — {ref}\n"
        f"Sender: {sender} declared {lines}\n"
        f"Receiver: {receiver} rejected it, so every line is back on the sender's trip\n"
        f"Sender's trip: {trip_line}\n"
        f"Time: {stamp} | Env: testing\n"
        "Action: review it on the admin Bottle Tracking page, Bottle Transfers tab: "
        "https://admin.aqua-element.uz/delivery/bottles"
    )


def test_the_named_receiver_rejects_and_every_line_goes_back(
    client, db, enqueued, slack, sender, receiver, small  # noqa: F811
):
    """Aziz (10 full, 4 empties, 6 x 10 L) hands all of it to Bobur, who has nothing open; Bobur taps Reject."""
    s = trip(db, sender, bottles=10, units={small.id: 6}, collected=4)
    transfer_id = _sent(client, sender, receiver, full=10, empty=4, units={small.id: 6})
    assert _moved_out(db, s.id) == (10, 4, [6])
    enqueued.clear()

    before = datetime.now(TASHKENT)
    r = _reject(client, receiver, transfer_id)
    after = datetime.now(TASHKENT)

    assert r.status_code == 200, r.get_data(as_text=True)
    data = r.get_json()["data"]
    assert (data["status"], data["sender_trip_was_closed"], data["confirmed_full"], data["confirmed_empty"]) == (
        "rejected",
        False,
        None,
        None,
    )
    assert (data["receiver_session_id"], data["rejected_at"] is not None) == (None, True)
    assert data["lines"] == _lines(small, full=10, empty=4, units=6)
    assert data["units"] == [
        {"product_id": small.id, "product_name": "10 Litr suv", "declared": 6, "confirmed": None, "resolved": None}
    ]
    assert _moved_out(db, s.id) == (0, 0, [0])
    assert db.session.get(DriverBottleSession, s.id).status == DriverBottleSessionStatus.OPEN
    assert DriverBottleSession.query.filter_by(driver_user_id=receiver.id).count() == 0
    transfer = db.session.get(DriverBottleTransfer, transfer_id)
    assert (transfer.confirmed_at, transfer.confirmed_quantity, transfer.confirmed_empty_quantity) == (None, None, None)
    assert [line.confirmed for line in transfer.unit_lines] == [None]
    assert pushes(enqueued) == [
        (
            900000004,
            "transfer_rejected",
            {
                "id": transfer_id,
                "transfer_ref": transfer.transfer_ref,
                "receiver_name": "Bobur",
                "lines": _lines(small, full=10, empty=4, units=6),
                "sender_trip_was_closed": False,
            },
        )
    ]
    text, stamp = _posted_alert(slack, before, after)
    assert text == _reject_alert(
        transfer.transfer_ref,
        sender="Aziz",
        receiver="Bobur",
        lines="10 full + 4 empty + 6 × 10 Litr suv",
        trip_line="still open",
        stamp=stamp,
    )
    # Ten full bottles are back on an open trip: the pool is offered to auto-assign again.
    assert reoffers(enqueued) == 1


@pytest.mark.parametrize("who", ["sender", "third_driver"])
def test_another_driver_cannot_reject(
    client, db, enqueued, slack, sender, receiver, driver, small, who  # noqa: F811
):
    """Only Bobur, the named receiver, may reject: Aziz himself and Dilshod are refused and nothing moves."""
    s = trip(db, sender, bottles=10, units={small.id: 6}, collected=4)
    transfer_id = _sent(client, sender, receiver, full=10, empty=4, units={small.id: 6})
    enqueued.clear()

    refused = _reject(client, {"sender": sender, "third_driver": driver}[who], transfer_id)

    assert refused.status_code == 409, refused.get_data(as_text=True)
    assert refused.get_json()["error_code"] == "BOTTLE_TRANSFER_NOT_RECEIVER"
    assert _status_of(db, transfer_id) == DriverBottleTransferStatus.PENDING
    assert _moved_out(db, s.id) == (10, 4, [6])
    assert (enqueued, slack) == ([], [])


def _bring_to(client, db, admin_claim_headers, taker, small, transfer_id, status):  # noqa: F811
    """Move a pending transfer of 10 full + 4 empty + 6 x 10 L to `status` through the routes that do it: the
    receiver's Confirm, his other numbers (9 full: disputed), the office resolve of that dispute, or his Reject."""
    taker_headers = staff_headers(taker)
    if status == "confirmed":
        answer = client.post(CONFIRM.format(transfer_id), json={"confirmAsDeclared": True}, headers=taker_headers)
    elif status in ("disputed", "resolved"):
        body = {"confirmedFull": 9, "confirmedEmpty": 4, "units": [{"productId": small.id, "quantity": 6}]}
        answer = client.post(CONFIRM.format(transfer_id), json=body, headers=taker_headers)
        if status == "resolved":
            assert answer.status_code == 200, answer.get_data(as_text=True)
            body = {
                "resolvedFull": 10,
                "resolvedEmpty": 4,
                "resolvedUnits": [{"productId": small.id, "quantity": 6}],
                "resolutionNotes": "Counted again at the depot",
            }
            answer = client.post(RESOLVE.format(transfer_id), json=body, headers=admin_claim_headers)
    else:
        answer = _reject(client, taker, transfer_id)
    assert answer.status_code == 200, answer.get_data(as_text=True)
    assert _status_of(db, transfer_id).value == status


@pytest.mark.parametrize("status", ["confirmed", "disputed", "resolved", "rejected"])
def test_only_a_pending_transfer_can_be_rejected(
    client, db, enqueued, slack, admin_claim_headers, sender, receiver, small, status  # noqa: F811
):
    """Bobur already counted (or rejected) Aziz's transfer, or the office resolved it: a Reject is refused and nothing
    moves again."""
    s = trip(db, sender, bottles=12, units={small.id: 8}, collected=4)
    transfer_id = _sent(client, sender, receiver, full=10, empty=4, units={small.id: 6})
    _bring_to(client, db, admin_claim_headers, receiver, small, transfer_id, status)
    moved = _moved_out(db, s.id)
    published, posted = list(enqueued), list(slack)

    refused = _reject(client, receiver, transfer_id)

    assert refused.status_code == 409, refused.get_data(as_text=True)
    assert refused.get_json()["error_code"] == "BOTTLE_TRANSFER_NOT_PENDING"
    assert _status_of(db, transfer_id).value == status
    assert _moved_out(db, s.id) == moved
    assert (enqueued, slack) == (published, posted)


@pytest.mark.parametrize("closer", ["driver", "office"])
def test_a_reject_onto_a_closed_trip_recomputes_both_discrepancies(
    client, db, enqueued, slack, admin_claim_headers, sender, receiver, small, closer  # noqa: F811
):
    """Aziz sent everything to Bobur, then closed his trip (or the office force-closed it) returning all of it, since
    Bobur never took the bottles: the close reads 14 bottles and 6 x 10 L too many. Bobur then rejects: the units go
    back to that closed trip, and both its discrepancies are recomputed to 0. Nothing is re-offered."""
    s = trip(db, sender, bottles=10, units={small.id: 6}, collected=4)
    transfer_id = _sent(client, sender, receiver, full=10, empty=4, units={small.id: 6})
    returned = {"bottles_returned_to_warehouse": 14, "units_returned": [{"product_id": small.id, "quantity": 6}]}
    if closer == "driver":
        closed = client.post(CLOSE, json=returned, headers=staff_headers(sender))
    else:
        body = {**returned, "reason": "Driver left the trip open"}
        closed = client.post(FORCE_CLOSE.format(s.id), json=body, headers=admin_claim_headers)
    assert closed.status_code == 200, closed.get_data(as_text=True)
    db.session.expire_all()
    sealed = db.session.get(DriverBottleSession, s.id)
    assert (sealed.discrepancy, [line.discrepancy for line in sealed.unit_lines]) == (-14, [-6])
    enqueued.clear()

    before = datetime.now(TASHKENT)
    r = _reject(client, receiver, transfer_id)
    after = datetime.now(TASHKENT)

    assert r.status_code == 200, r.get_data(as_text=True)
    assert (r.get_json()["data"]["status"], r.get_json()["data"]["sender_trip_was_closed"]) == ("rejected", True)
    assert _moved_out(db, s.id) == (0, 0, [0])
    recomputed = db.session.get(DriverBottleSession, s.id)
    assert (recomputed.status, recomputed.bottles_returned_to_warehouse, recomputed.discrepancy) == (
        {"driver": DriverBottleSessionStatus.CLOSED, "office": DriverBottleSessionStatus.FORCE_CLOSED}[closer],
        14,
        0,
    )
    line = DriverBottleSessionUnit.query.filter_by(session_id=s.id).one()
    assert (line.returned, line.discrepancy) == (6, 0)
    assert DriverBottleSession.query.filter_by(driver_user_id=receiver.id).count() == 0
    transfer = db.session.get(DriverBottleTransfer, transfer_id)
    assert pushes(enqueued) == [
        (
            900000004,
            "transfer_rejected",
            {
                "id": transfer_id,
                "transfer_ref": transfer.transfer_ref,
                "receiver_name": "Bobur",
                "lines": _lines(small, full=10, empty=4, units=6),
                "sender_trip_was_closed": True,
            },
        )
    ]
    text, stamp = _posted_alert(slack, before, after)
    assert text == _reject_alert(
        transfer.transfer_ref,
        sender="Aziz",
        receiver="Bobur",
        lines="10 full + 4 empty + 6 × 10 Litr suv",
        trip_line="closed before the reject; discrepancies recomputed: bottles 0, 10 Litr suv 0",
        stamp=stamp,
    )
    assert reoffers(enqueued) == 0


def test_a_reject_of_empties_and_ten_litre_only_reoffers_nothing(
    client, db, enqueued, slack, sender, receiver, small  # noqa: F811
):
    """Aziz hands Bobur 4 empties and 6 x 10 L, no full bottle; Bobur rejects. Everything goes back to Aziz's open trip,
    and both are told, but no full bottle came back, so the pool is not re-offered (spec §6.7: full bottles only)."""
    s = trip(db, sender, units={small.id: 6}, collected=4)
    transfer_id = _sent(client, sender, receiver, empty=4, units={small.id: 6})
    enqueued.clear()

    r = _reject(client, receiver, transfer_id)

    assert r.status_code == 200, r.get_data(as_text=True)
    assert (r.get_json()["data"]["status"], r.get_json()["data"]["sender_trip_was_closed"]) == ("rejected", False)
    assert _moved_out(db, s.id) == (0, 0, [0])
    assert [(event, payload["lines"]) for _chat, event, payload in pushes(enqueued)] == [
        ("transfer_rejected", _lines(small, full=0, empty=4, units=6))
    ]
    assert len(slack) == 1
    assert reoffers(enqueued) == 0


def test_a_rejected_transfer_cannot_be_resolved(
    client, db, enqueued, slack, admin_claim_headers, sender, receiver, small  # noqa: F811
):
    """The office resolve takes only a disputed transfer: a rejected one is refused and its lines stay where the
    reject put them."""
    s = trip(db, sender, bottles=10, units={small.id: 6}, collected=4)
    transfer_id = _sent(client, sender, receiver, full=10, empty=4, units={small.id: 6})
    assert _reject(client, receiver, transfer_id).status_code == 200

    refused = client.post(
        RESOLVE.format(transfer_id),
        json={
            "resolvedFull": 10,
            "resolvedEmpty": 4,
            "resolvedUnits": [{"productId": small.id, "quantity": 6}],
            "resolutionNotes": "Bobur did take them",
        },
        headers=admin_claim_headers,
    )

    assert refused.status_code == 409, refused.get_data(as_text=True)
    assert refused.get_json()["message"] == "Can only resolve DISPUTED transfers"
    assert _status_of(db, transfer_id) == DriverBottleTransferStatus.REJECTED
    assert _moved_out(db, s.id) == (0, 0, [0])
    assert DriverBottleSession.query.filter_by(driver_user_id=receiver.id).count() == 0


def test_a_reject_whose_read_after_the_commit_fails_still_posts_slack_and_tells_the_sender(
    client, db, caplog, monkeypatch, enqueued, slack, sender, receiver, small  # noqa: F811
):
    """The reject has committed, and the first read of the transfer after it fails (the database dropped the
    connection, say). Slack still gets its post (every time) and Aziz his push: each step reads the transfer itself.
    Only the pool re-offer, which needs that read's full-bottle count, is skipped. The log line names the step and the
    commit, never "count", so triage does not take a reject for a count."""
    trip(db, sender, bottles=10, units={small.id: 6}, collected=4)
    transfer_id = _sent(client, sender, receiver, full=10, empty=4, units={small.id: 6})
    enqueued.clear()

    def read_fails(transfer):
        raise RuntimeError("server closed the connection unexpectedly")

    monkeypatch.setattr(BottleTrackingService, "_kept_as_read", staticmethod(read_fails))
    logger = logging.getLogger("business_app.services.bottle_tracking_service")
    before = datetime.now(TASHKENT)
    with captured_logs(caplog, logger):
        r = _reject(client, receiver, transfer_id)
    after = datetime.now(TASHKENT)

    assert r.status_code == 200, r.get_data(as_text=True)
    assert (r.get_json()["data"]["status"], r.get_json()["data"]["sender_trip_was_closed"]) == ("rejected", False)
    transfer = db.session.get(DriverBottleTransfer, transfer_id)
    assert pushes(enqueued) == [
        (
            900000004,
            "transfer_rejected",
            {
                "id": transfer_id,
                "transfer_ref": transfer.transfer_ref,
                "receiver_name": "Bobur",
                "lines": _lines(small, full=10, empty=4, units=6),
                "sender_trip_was_closed": False,
            },
        )
    ]
    text, stamp = _posted_alert(slack, before, after)
    assert text == _reject_alert(
        transfer.transfer_ref,
        sender="Aziz",
        receiver="Bobur",
        lines="10 full + 4 empty + 6 × 10 Litr suv",
        trip_line="still open",
        stamp=stamp,
    )
    assert reoffers(enqueued) == 0
    assert caplog.messages == [f"[BOTTLE] read after transfer {transfer_id}'s commit failed"]


def test_a_reject_of_a_missing_transfer_is_not_found(client, db, enqueued, slack, receiver):  # noqa: F811
    refused = _reject(client, receiver, 987654)

    assert refused.status_code == 404, refused.get_data(as_text=True)
    assert refused.get_json()["error_code"] == "BOTTLE_TRANSFER_NOT_FOUND"
    assert (enqueued, slack) == ([], [])
