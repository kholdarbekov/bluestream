"""The trip publishes its one-way unit lines (spec 2026-11-18 §6.10), on the routes the apps call.

`GET /staff/bottles/session/current` (the driver's own screen and the load screen), `/staff/bottles/session/membership`
and `/staff/bottles/sessions/joinable` (the co-driver screens) and the admin session routes all carry `units` and
`transferable_any` beside the 18.9 L capacity keys. `free` is the line's on-board count less what the crew's stops on
their way already promise; a closed or reopened trip has none.
"""

from datetime import datetime, timezone

import pytest

from business_app.models.bottle import DriverBottleSession, DriverBottleSessionUnit
from shared.enums import DeliveryStatus, DriverBottleSessionStatus
from tests.integration.trip_unit_builders import (  # noqa: F401  (fixtures)
    big,
    co_driver,
    delivery_for,
    driver,
    member,
    order_with_lines,
    owner,
    small,
    staff_headers,
    trip,
)

pytestmark = pytest.mark.integration

CURRENT = "/api/v1/staff/bottles/session/current"
MEMBERSHIP = "/api/v1/staff/bottles/session/membership"
JOINABLE = "/api/v1/staff/bottles/sessions/joinable"


def _current(client, driver):
    response = client.get(CURRENT, headers=staff_headers(driver))
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["data"]


def test_the_current_trip_publishes_its_ten_litre_line(client, db, driver, co_driver, small, sample_user):
    s = trip(db, driver, units={small.id: 12})
    member(db, s, co_driver)
    held = order_with_lines(db, sample_user, [(small, 4)])
    delivery_for(db, held, co_driver, DeliveryStatus.IN_TRANSIT)

    data = _current(client, driver)

    assert data["units"] == [
        {
            "product_id": small.id,
            "product_name": "10 Litr suv",
            "loaded": 12,
            "delivered": 0,
            "transferred_in": 0,
            "transferred_out": 0,
            "on_board": 12,
            "transferable": 12,
            "free": 8,
            "returned": None,
            "discrepancy": None,
        }
    ]
    assert data["transferable_any"] is True


def test_a_trip_without_a_line_publishes_none(client, db, driver):
    trip(db, driver, bottles=5)

    data = _current(client, driver)

    assert data["units"] == []
    assert data["free_full_bottles"] == 5
    assert data["transferable_any"] is True


def test_a_trip_with_nothing_aboard_has_nothing_to_transfer(client, db, driver):
    trip(db, driver)

    data = _current(client, driver)

    assert data["units"] == []
    assert data["transferable_any"] is False


def test_a_reopened_trip_has_no_free_ten_litre(client, db, driver, small):
    s = trip(db, driver, units={small.id: 12})
    s.reopened_at = datetime.now(timezone.utc)
    db.session.commit()

    data = _current(client, driver)

    assert [(u["on_board"], u["free"]) for u in data["units"]] == [(12, 0)]
    assert data["free_full_bottles"] == 0


def test_a_negative_tally_shows_its_sign_and_transfers_nothing(client, db, driver, small):
    s = trip(db, driver, units={small.id: 2})
    line = DriverBottleSessionUnit.query.filter_by(session_id=s.id).one()
    line.delivered = 3
    db.session.commit()

    data = _current(client, driver)

    assert [(u["on_board"], u["transferable"], u["free"]) for u in data["units"]] == [(-1, 0, 0)]
    assert data["transferable_any"] is False


def test_a_stop_holding_both_waters_promises_each_from_its_own_line(client, db, driver, big, small, sample_user):
    trip(db, driver, bottles=10, units={small.id: 12})
    stop = order_with_lines(db, sample_user, [(big, 3), (small, 5)])
    delivery_for(db, stop, driver, DeliveryStatus.ARRIVED)

    data = _current(client, driver)

    assert data["free_full_bottles"] == 7
    assert data["units"][0]["free"] == 7
    assert data["units"][0]["on_board"] == 12


def test_the_membership_and_join_screens_list_the_owner_lines(client, db, owner, co_driver, driver, small, sample_user):
    s = trip(db, owner, units={small.id: 9})
    held = order_with_lines(db, sample_user, [(small, 2)])
    delivery_for(db, held, owner, DeliveryStatus.IN_TRANSIT)
    expected = [
        {
            "product_id": small.id,
            "product_name": "10 Litr suv",
            "loaded": 9,
            "delivered": 0,
            "transferred_in": 0,
            "transferred_out": 0,
            "on_board": 9,
            "transferable": 9,
            "free": 7,
            "returned": None,
            "discrepancy": None,
        }
    ]

    joinable = client.get(JOINABLE, headers=staff_headers(driver))
    assert joinable.status_code == 200, joinable.get_data(as_text=True)
    (listed,) = joinable.get_json()["data"]
    assert listed["session_id"] == s.id
    assert listed["units"] == expected
    assert listed["transferable_any"] is True

    member(db, s, co_driver)
    info = client.get(MEMBERSHIP, headers=staff_headers(co_driver))
    assert info.status_code == 200, info.get_data(as_text=True)
    assert info.get_json()["data"]["units"] == expected
    assert info.get_json()["data"]["transferable_any"] is True


def test_the_admin_session_detail_and_list_carry_the_lines(client, db, driver, small, admin_claim_headers):
    s = trip(db, driver, units={small.id: 6})

    detail = client.get(f"/api/v1/admin/bottles/sessions/{s.id}", headers=admin_claim_headers)
    assert detail.status_code == 200, detail.get_data(as_text=True)
    body = detail.get_json()["data"]
    assert [(u["product_name"], u["loaded"], u["on_board"], u["free"]) for u in body["units"]] == [
        ("10 Litr suv", 6, 6, 6)
    ]
    assert body["transferable_any"] is True

    listing = client.get("/api/v1/admin/bottles/sessions", headers=admin_claim_headers)
    assert listing.status_code == 200, listing.get_data(as_text=True)
    (row,) = [r for r in listing.get_json()["data"]["items"] if r["id"] == s.id]
    assert [(u["product_name"], u["on_board"]) for u in row["units"]] == [("10 Litr suv", 6)]


def test_a_closed_trip_lists_its_lines_with_nothing_free(client, db, driver, small, admin_claim_headers):
    s = trip(db, driver, units={small.id: 6}, status=DriverBottleSessionStatus.CLOSED)

    body = client.get(f"/api/v1/admin/bottles/sessions/{s.id}", headers=admin_claim_headers).get_json()["data"]

    assert [(u["on_board"], u["free"]) for u in body["units"]] == [(6, 0)]
