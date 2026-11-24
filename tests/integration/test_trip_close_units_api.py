"""Closing the bottle trip with its one-way unit lines (spec 2026-11-18 §6.8, §6.11).

The driver's close answers every line on the trip (`units_returned`, one `{product_id, quantity}` per line) or is
refused with `BOTTLE_SESSION_RETURN_INCOMPLETE`; each line gets its own discrepancy, never added to the 18.9 L one.
The office force-close takes a missing line as 0. A trip with 10 L still on board never auto-closes after a transfer.
A reopen clears every line's return. "Has a discrepancy" is one published answer (`has_discrepancy`), and the admin
list's "only discrepancies" filter is its SQL twin.

Driven through the routes the staff bot and the admin page call, with the bodies those clients send. Only Celery
publishing is recorded (`enqueued`).
"""

import pytest

from business_app.models.bottle import DriverBottleSession, DriverBottleSessionUnit
from shared.enums import DriverBottleSessionStatus
from shared.staff_constants import BOTTLE_RETURN_COLUMN_CEILING, MAX_BOTTLES_PER_SESSION
from staff_bot.handlers.base import BaseHandler
from tests.integration.test_admin_order_edit import _attach_returnable_address, _seed_paid_cash_delivered_order
from tests.integration.test_bottle_transfer_split_api import _bot_body, seeded_copy  # noqa: F401  (seeded_copy is a fixture)
from tests.integration.test_redispatch_reschedules_to_today import enqueued  # noqa: F401  (fixture)
from tests.integration.trip_unit_builders import (  # noqa: F401  (fixtures)
    big,
    bind,
    driver,
    pushes,
    receiver,
    sender,
    small,
    staff_headers,
    ten_litre,
    trip,
)

pytestmark = pytest.mark.integration

CLOSE = "/api/v1/staff/bottles/session/close"
TRANSFERS = "/api/v1/staff/bottles/transfers"
CONFIRM = TRANSFERS + "/{}/confirm"
SESSIONS = "/api/v1/admin/bottles/sessions"
FORCE_CLOSE = SESSIONS + "/{}/force-close"
ORDER_EDIT = "/api/v1/admin/orders/{}/edit"

CLOSED = DriverBottleSessionStatus.CLOSED


def _closed(db, driver, *, discrepancy, line_discrepancy=None, line_product=None):
    """A closed trip with its discrepancy, and one line with `line_discrepancy` when `line_product` is given."""
    s = trip(db, driver, units={line_product.id: 0} if line_product else None, status=CLOSED)
    s.discrepancy = discrepancy
    for line in s.unit_lines:
        line.discrepancy = line_discrepancy
    db.session.commit()
    return s


# --------------------------------------------------------------------------- #
# The driver's close
# --------------------------------------------------------------------------- #


def test_a_missing_ten_litre_return_is_refused(client, db, driver, small):
    s = trip(db, driver, bottles=40, units={small.id: 12})

    r = client.post(CLOSE, json={"bottles_returned_to_warehouse": 30}, headers=staff_headers(driver))

    assert r.status_code == 400, r.get_data(as_text=True)
    body = r.get_json()
    assert (body["error_code"], body["details"]) == (
        "BOTTLE_SESSION_RETURN_INCOMPLETE",
        {"missing_product_ids": [small.id], "validation_errors": []},
    )
    db.session.expire_all()
    stored = db.session.get(DriverBottleSession, s.id)
    assert (stored.status, stored.bottles_returned_to_warehouse, stored.discrepancy) == (
        DriverBottleSessionStatus.OPEN,
        None,
        None,
    )
    assert [(line.returned, line.discrepancy) for line in stored.unit_lines] == [(None, None)]


def test_the_bot_tells_the_driver_to_type_every_return(client, db, driver, small, seeded_copy):  # noqa: F811
    seeded_copy("staff.error.api.bottle_session_return_incomplete")
    trip(db, driver, bottles=40, units={small.id: 12})
    r = client.post(CLOSE, json={"bottles_returned_to_warehouse": 30}, headers=staff_headers(driver))
    assert r.status_code == 400, r.get_data(as_text=True)
    body = r.get_json()

    shown = BaseHandler.__new__(BaseHandler)._resolve_api_error_message(
        "en", body.get("message"), r.status_code, body["error_code"], details=body.get("details")
    )

    assert shown == "Type the return for every line, then close again."


def test_missing_ten_litre_never_hides_behind_extra_empties(client, db, driver, small):
    s = trip(db, driver, bottles=40, units={small.id: 12}, collected=25)
    # Setup tallies: 30 x 18.9 L and 10 x 10 L delivered.
    s.bottles_delivered = 30
    s.unit_lines[0].delivered = 10
    db.session.commit()

    r = client.post(
        CLOSE,
        json={"bottles_returned_to_warehouse": 37, "units_returned": [{"product_id": small.id, "quantity": 0}]},
        headers=staff_headers(driver),
    )

    assert r.status_code == 200, r.get_data(as_text=True)
    data = r.get_json()["data"]
    assert (data["status"], data["bottles_returned_to_warehouse"]) == ("closed", 37)
    assert data["discrepancy"] == -2  # (10 full + 25 empties) - 37
    assert [(u["returned"], u["discrepancy"]) for u in data["units"]] == [(0, 2)]
    assert data["has_discrepancy"] is True


def test_the_staff_bots_own_close_body_is_what_the_endpoint_takes(client, db, driver, small, monkeypatch):
    """The body comes from the bot's client itself, so a key renamed on either side fails here."""
    trip(db, driver, bottles=40, units={small.id: 12})
    endpoint, body = _bot_body(
        monkeypatch, "close_bottle_session", 37, units_returned=[{"product_id": small.id, "quantity": 0}]
    )

    r = client.post(endpoint, json=body, headers=staff_headers(driver))

    assert r.status_code == 200, r.get_data(as_text=True)
    data = r.get_json()["data"]
    assert data["status"] == "closed"
    assert [(u["product_id"], u["returned"]) for u in data["units"]] == [(small.id, 0)]


def test_a_close_naming_a_product_the_trip_has_no_line_for_is_refused(client, db, driver, small, sample_category):
    other = ten_litre(db, sample_category)
    s = trip(db, driver, bottles=10, units={small.id: 4})

    r = client.post(
        CLOSE,
        json={
            "bottles_returned_to_warehouse": 10,
            "units_returned": [{"product_id": small.id, "quantity": 4}, {"product_id": other.id, "quantity": 1}],
        },
        headers=staff_headers(driver),
    )

    assert r.status_code == 400, r.get_data(as_text=True)
    body = r.get_json()
    assert (body["error_code"], body["details"]) == (
        "TRIP_UNIT_NOT_COUNTED",
        {"product_id": other.id, "validation_errors": []},
    )
    db.session.expire_all()
    assert db.session.get(DriverBottleSession, s.id).status == DriverBottleSessionStatus.OPEN


def test_a_line_returned_twice_is_refused_by_the_request_model(client, db, driver, small):
    s = trip(db, driver, bottles=10, units={small.id: 4})

    r = client.post(
        CLOSE,
        json={
            "bottles_returned_to_warehouse": 10,
            "units_returned": [{"product_id": small.id, "quantity": 4}, {"product_id": small.id, "quantity": 0}],
        },
        headers=staff_headers(driver),
    )

    assert r.status_code == 400, r.get_data(as_text=True)
    assert r.get_json()["errors"] == [f"units_returned: Product {small.id} is listed twice; give each product once."]
    db.session.expire_all()
    assert db.session.get(DriverBottleSession, s.id).status == DriverBottleSessionStatus.OPEN


@pytest.mark.parametrize(
    "quantity,status_code,stored",
    [
        (MAX_BOTTLES_PER_SESSION + 1, 200, ("closed", 501, -497)),
        (BOTTLE_RETURN_COLUMN_CEILING, 200, ("closed", 2_147_483_147, -2_147_483_143)),
        (BOTTLE_RETURN_COLUMN_CEILING + 1, 400, ("open", None, None)),
    ],
    ids=["past-the-load-ceiling", "at-the-storage-bound", "past-the-storage-bound"],
)
def test_a_line_return_is_bounded_by_storage_like_the_bottle_return(
    client, db, driver, small, quantity, status_code, stored
):
    s = trip(db, driver, bottles=10, units={small.id: 4})

    r = client.post(
        CLOSE,
        json={"bottles_returned_to_warehouse": 10, "units_returned": [{"product_id": small.id, "quantity": quantity}]},
        headers=staff_headers(driver),
    )

    assert r.status_code == status_code, r.get_data(as_text=True)
    db.session.expire_all()
    row = db.session.get(DriverBottleSession, s.id)
    assert (row.status.value, row.unit_lines[0].returned, row.unit_lines[0].discrepancy) == stored


# --------------------------------------------------------------------------- #
# Auto-close after a transfer
# --------------------------------------------------------------------------- #


def test_auto_close_is_suppressed_while_ten_litre_is_on_board(
    client, db, sender, receiver, small, enqueued  # noqa: F811
):
    """Aziz hands Bobur every 18.9 L full bottle and empty by amount; his 3 x 10 L stay on board, so his trip stays
    open, and his message says so."""
    s = trip(db, sender, bottles=5, units={small.id: 3}, collected=2)
    sent = client.post(
        TRANSFERS,
        json={"receiverDriverId": receiver.id, "fullQuantity": 5, "emptyQuantity": 2},
        headers=staff_headers(sender),
    )
    assert sent.status_code == 201, sent.get_data(as_text=True)
    transfer_id = sent.get_json()["data"]["id"]

    confirmed = client.post(
        CONFIRM.format(transfer_id), json={"confirmAsDeclared": True}, headers=staff_headers(receiver)
    )

    assert confirmed.status_code == 200, confirmed.get_data(as_text=True)
    assert confirmed.get_json()["data"]["status"] == "confirmed"
    db.session.expire_all()
    stored = db.session.get(DriverBottleSession, s.id)
    assert (stored.status, stored.transferable_full, stored.transferable_empties) == (
        DriverBottleSessionStatus.OPEN,
        0,
        0,
    )
    assert [(line.on_board, line.returned) for line in stored.unit_lines] == [(3, None)]
    sender_pushes = [p for p in pushes(enqueued) if p[0] == int(sender.telegram_id)]
    assert sender_pushes == [
        (
            int(sender.telegram_id),
            "transfer_confirmed",
            {
                "id": transfer_id,
                "receiver_name": "Bobur",
                "confirmed_full": 5,
                "confirmed_empty": 2,
                "auto_closed": False,
                "lines": [
                    {
                        "kind": "full",
                        "product_id": None,
                        "product_name": None,
                        "declared": 5,
                        "confirmed": 5,
                        "resolved": None,
                    },
                    {
                        "kind": "empty",
                        "product_id": None,
                        "product_name": None,
                        "declared": 2,
                        "confirmed": 2,
                        "resolved": None,
                    },
                    {
                        "kind": "product",
                        "product_id": small.id,
                        "product_name": "10 Litr suv",
                        "declared": 0,
                        "confirmed": 0,
                        "resolved": None,
                    },
                ],
            },
        )
    ]


# --------------------------------------------------------------------------- #
# The office force-close
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("returned_box,returned", [(5, 5), (None, 0)], ids=["ten-litre-typed", "no-ten-litre-box"])
def test_force_close_with_and_without_ten_litre(client, db, admin_claim_headers, driver, small, returned_box, returned):
    s = trip(db, driver, bottles=10, units={small.id: 8})
    body = {"reason": "Driver left"}
    if returned_box is not None:
        body["units_returned"] = [{"product_id": small.id, "quantity": returned_box}]

    r = client.post(FORCE_CLOSE.format(s.id), json=body, headers=admin_claim_headers)

    assert r.status_code == 200, r.get_data(as_text=True)
    data = r.get_json()["data"]
    assert (data["status"], data["bottles_returned_to_warehouse"], data["discrepancy"]) == ("force_closed", 0, 10)
    assert [(u["returned"], u["discrepancy"]) for u in data["units"]] == [(returned, 8 - returned)]
    db.session.expire_all()
    stored = DriverBottleSessionUnit.query.filter_by(session_id=s.id).one()
    assert (stored.returned, stored.discrepancy) == (returned, 8 - returned)


# --------------------------------------------------------------------------- #
# The reopen (an admin edit of a delivered order on a closed trip)
# --------------------------------------------------------------------------- #


def test_a_reopen_clears_every_lines_return(client, db, admin_auth_headers, driver, big, small, sample_user):
    address = _attach_returnable_address(db, sample_user, big)
    order = _seed_paid_cash_delivered_order(sample_user, big, quantity=4)
    order.delivery_address_id = address.id
    db.session.commit()
    s = trip(db, driver, bottles=10, units={small.id: 6}, status=CLOSED)
    s.bottles_returned_to_warehouse, s.discrepancy = 6, 0
    s.unit_lines[0].returned, s.unit_lines[0].discrepancy = 5, 1
    db.session.commit()
    bind(db, s, order, driver)

    r = client.post(
        ORDER_EDIT.format(order.id),
        json={
            "items": [{"orderItemId": order.order_items[0].id, "productId": big.id, "quantity": 2}],
            "reason": "customer kept only 2 of 4 bottles after delivery",
        },
        headers=admin_auth_headers,
    )

    assert r.status_code == 200, r.get_data(as_text=True)
    db.session.expire_all()
    stored = db.session.get(DriverBottleSession, s.id)
    assert (stored.status, stored.bottles_returned_to_warehouse, stored.discrepancy, stored.reopen_count) == (
        DriverBottleSessionStatus.OPEN,
        None,
        None,
        1,
    )
    assert [(line.loaded, line.returned, line.discrepancy) for line in stored.unit_lines] == [(6, None, None)]


# --------------------------------------------------------------------------- #
# One answer to "does this trip have a discrepancy"
# --------------------------------------------------------------------------- #


def test_only_discrepancies_lists_a_ten_litre_only_discrepancy(client, db, admin_claim_headers, driver, small):
    a = _closed(db, driver, discrepancy=0, line_discrepancy=2, line_product=small)
    b = _closed(db, driver, discrepancy=-1, line_discrepancy=0, line_product=small)
    c = _closed(db, driver, discrepancy=0, line_discrepancy=0, line_product=small)
    d = _closed(db, driver, discrepancy=0)  # closed before go-live: no line
    open_trip = trip(db, driver, bottles=10, units={small.id: 2})

    r = client.get(SESSIONS + "?only_discrepancies=true", headers=admin_claim_headers)

    assert r.status_code == 200, r.get_data(as_text=True)
    assert sorted((i["id"], i["has_discrepancy"]) for i in r.get_json()["data"]["items"]) == [
        (a.id, True),
        (b.id, True),
    ]
    assert r.get_json()["data"]["total"] == 2

    unfiltered = client.get(SESSIONS, headers=admin_claim_headers)

    assert unfiltered.status_code == 200, unfiltered.get_data(as_text=True)
    assert sorted((i["id"], i["has_discrepancy"]) for i in unfiltered.get_json()["data"]["items"]) == [
        (a.id, True),
        (b.id, True),
        (c.id, False),
        (d.id, False),
        (open_trip.id, False),
    ]


@pytest.mark.parametrize(
    "session_d,line_d,expected",
    [(None, None, False), (0, None, False), (0, 0, False), (3, 0, True), (0, -2, True), (None, 2, True)],
)
def test_has_discrepancy_and_its_sql_twin_agree(db, driver, small, session_d, line_d, expected):
    s = _closed(db, driver, discrepancy=session_d, line_discrepancy=line_d, line_product=small)
    db.session.expire_all()

    assert db.session.get(DriverBottleSession, s.id).has_discrepancy is expected
    matched = DriverBottleSession.query.filter(DriverBottleSession.id == s.id, DriverBottleSession.has_discrepancy)
    assert (matched.count() == 1) is expected
