"""The product's bottle-trip setting and its one save check (spec 2026-11-18 §6.1).

Driven through the routes the admin Products page calls (`POST /api/v1/admin/products`,
`PUT /api/v1/admin/products/<id>`, `GET /api/v1/admin/products`) with an admin role-claim token.
`ProductService.check_product_save` refuses a returnable product counted on the trip (400 TRIP_UNIT_ON_RETURNABLE)
and any change of the setting while a trip is open (409 TRIP_UNIT_SETTING_BLOCKED, `details.open_trips`). A transfer,
pending or disputed, never blocks it, and an edit that leaves the setting as it is never asks about trips.
"""

from decimal import Decimal

import pytest

from business_app.models.bottle import DriverBottleTransfer
from business_app.models.product import Product
from shared.enums import DriverBottleSessionStatus, DriverBottleTransferStatus
from tests.integration.trip_unit_builders import (  # noqa: F401  (fixtures)
    big,
    co_driver,
    driver,
    owner,
    receiver,
    sender,
    small,
    ten_litre,
    trip,
)

pytestmark = pytest.mark.integration

PRODUCTS = "/api/v1/admin/products"


def _stored(db, product_id):
    """The product row as committed: (counted_on_bottle_trip, tracks_returnable_bottles, per unit, base price)."""
    db.session.expire_all()
    product = db.session.get(Product, product_id)
    return (
        product.counted_on_bottle_trip,
        product.tracks_returnable_bottles,
        product.returnable_bottles_per_unit,
        product.base_price,
    )


def _row(client, headers, product_id):
    """The product's row from the listing the Products page reads (its edit modal is filled from it)."""
    listing = client.get(PRODUCTS, headers=headers)
    assert listing.status_code == 200, listing.get_data(as_text=True)
    return next(item for item in listing.get_json()["data"]["items"] if item["id"] == product_id)


def _create_body(category_id, **overrides):
    """What the Products page's create modal sends for a non-returnable 10 L product."""
    body = {
        "name": "10 Litr suv yangi",
        "description": "Bir martalik idish",
        "category_id": category_id,
        "sku": "TEN-NEW-1",
        "price": 9000,
        "stock_quantity": 50,
        "min_order_quantity": 1,
        "volume": 10,
        "status": "active",
        "is_featured": False,
        "is_tryout_eligible": True,
        "in_sales_stock_check": False,
        "tracks_returnable_bottles": False,
        "returnable_bottles_per_unit": 0,
        "counted_on_bottle_trip": True,
        "images": [],
    }
    body.update(overrides)
    return body


def _transfer(db, sender_trip, sender_driver, receiver_driver, status):
    """A transfer row (setup only): the transfer routes are not what this file drives."""
    transfer = DriverBottleTransfer(
        sender_session_id=sender_trip.id,
        sender_driver_id=sender_driver.id,
        receiver_driver_id=receiver_driver.id,
        declared_quantity=3,
        status=status,
    )
    db.session.add(transfer)
    db.session.commit()
    return transfer


def test_ticking_the_setting_on_a_returnable_product_is_refused(client, db, admin_claim_headers, big):
    r = client.put(f"{PRODUCTS}/{big.id}", json={"counted_on_bottle_trip": True}, headers=admin_claim_headers)

    assert r.status_code == 400, r.get_data(as_text=True)
    assert r.get_json()["error_code"] == "TRIP_UNIT_ON_RETURNABLE"
    assert _stored(db, big.id) == (False, True, Decimal("1.00"), Decimal("15000.00"))


def test_making_a_counted_product_returnable_is_refused(client, db, admin_claim_headers, small):
    r = client.put(
        f"{PRODUCTS}/{small.id}",
        json={"tracks_returnable_bottles": True, "returnable_bottles_per_unit": 1},
        headers=admin_claim_headers,
    )

    assert r.status_code == 400, r.get_data(as_text=True)
    assert r.get_json()["error_code"] == "TRIP_UNIT_ON_RETURNABLE"
    assert _stored(db, small.id) == (True, False, Decimal("0.00"), Decimal("9000.00"))


def test_creating_a_counted_returnable_product_is_refused(client, db, admin_claim_headers, sample_category):
    body = _create_body(sample_category.id, tracks_returnable_bottles=True, returnable_bottles_per_unit=1)

    r = client.post(PRODUCTS, json=body, headers=admin_claim_headers)

    assert r.status_code == 400, r.get_data(as_text=True)
    assert r.get_json()["error_code"] == "TRIP_UNIT_ON_RETURNABLE"
    assert db.session.query(Product).filter_by(sku="TEN-NEW-1").count() == 0


@pytest.mark.parametrize("value", [True, False], ids=["tick", "untick"])
def test_a_change_while_a_trip_is_open_is_refused_with_the_open_trips(
    client, db, admin_claim_headers, small, driver, co_driver, owner, value
):
    small.counted_on_bottle_trip = not value
    db.session.commit()
    trip(db, owner, bottles=7, status=DriverBottleSessionStatus.CLOSED)
    first = trip(db, driver, bottles=5)
    second = trip(db, co_driver, bottles=2, units={small.id: 4})
    expected = [
        {
            "session_id": first.id,
            "session_ref": first.session_ref,
            "driver_user_id": driver.id,
            "driver_name": "Dilshod",
        },
        {
            "session_id": second.id,
            "session_ref": second.session_ref,
            "driver_user_id": co_driver.id,
            "driver_name": "Kamol",
        },
    ]

    r = client.put(f"{PRODUCTS}/{small.id}", json={"counted_on_bottle_trip": value}, headers=admin_claim_headers)

    assert r.status_code == 409, r.get_data(as_text=True)
    body = r.get_json()
    assert body["error_code"] == "TRIP_UNIT_SETTING_BLOCKED"
    assert body["details"]["open_trips"] == expected
    assert _stored(db, small.id)[0] is (not value)


def test_a_pending_or_disputed_transfer_does_not_block_the_tick(
    client, db, admin_claim_headers, sample_category, sender, receiver
):
    product = ten_litre(db, sample_category, counted=False)
    closed = trip(db, sender, bottles=6, status=DriverBottleSessionStatus.CLOSED)
    _transfer(db, closed, sender, receiver, DriverBottleTransferStatus.PENDING)
    _transfer(db, closed, sender, receiver, DriverBottleTransferStatus.DISPUTED)

    r = client.put(f"{PRODUCTS}/{product.id}", json={"counted_on_bottle_trip": True}, headers=admin_claim_headers)

    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["data"]["product"]["counted_on_bottle_trip"] is True
    assert _stored(db, product.id)[0] is True


def test_an_untick_with_every_trip_closed_saves(client, db, admin_claim_headers, small, driver):
    trip(db, driver, bottles=5, units={small.id: 4}, status=DriverBottleSessionStatus.CLOSED)

    r = client.put(f"{PRODUCTS}/{small.id}", json={"counted_on_bottle_trip": False}, headers=admin_claim_headers)

    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["data"]["product"]["counted_on_bottle_trip"] is False
    assert _stored(db, small.id)[0] is False


def test_creating_a_counted_product_while_a_trip_is_open_is_refused(
    client, db, admin_claim_headers, sample_category, driver
):
    open_trip = trip(db, driver, bottles=5)
    expected = [
        {
            "session_id": open_trip.id,
            "session_ref": open_trip.session_ref,
            "driver_user_id": driver.id,
            "driver_name": "Dilshod",
        }
    ]

    r = client.post(PRODUCTS, json=_create_body(sample_category.id), headers=admin_claim_headers)

    assert r.status_code == 409, r.get_data(as_text=True)
    body = r.get_json()
    assert body["error_code"] == "TRIP_UNIT_SETTING_BLOCKED"
    assert body["details"]["open_trips"] == expected
    assert db.session.query(Product).filter_by(sku="TEN-NEW-1").count() == 0


def test_an_unrelated_edit_of_a_counted_product_while_trips_are_open_passes(
    client, db, admin_claim_headers, small, driver
):
    trip(db, driver, bottles=5, units={small.id: 4})
    row = _row(client, admin_claim_headers, small.id)
    assert row["counted_on_bottle_trip"] is True
    row["price"] = 9500

    r = client.put(f"{PRODUCTS}/{small.id}", json=row, headers=admin_claim_headers)

    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["data"]["product"]["counted_on_bottle_trip"] is True
    assert _stored(db, small.id) == (True, False, Decimal("0.00"), Decimal("9500.00"))


def test_the_admin_payload_publishes_the_setting(client, db, admin_claim_headers, sample_category):
    r = client.post(PRODUCTS, json=_create_body(sample_category.id), headers=admin_claim_headers)

    assert r.status_code == 200, r.get_data(as_text=True)
    created = r.get_json()["data"]["product"]
    assert created["counted_on_bottle_trip"] is True
    assert _row(client, admin_claim_headers, created["id"])["counted_on_bottle_trip"] is True
    assert _stored(db, created["id"])[0] is True


def test_the_edit_modal_save_of_a_returnable_product_passes_while_trips_are_open(
    client, db, admin_claim_headers, big, small, driver
):
    """The edit modal sends the setting on every save, unticked for a returnable bottle (Products.js shows the switch
    for every product). An unchanged setting never asks about trips, so editing the 18.9 L bottle mid-shift saves."""
    trip(db, driver, bottles=5, units={small.id: 4})
    row = _row(client, admin_claim_headers, big.id)
    assert row["counted_on_bottle_trip"] is False
    row["price"] = 15500

    r = client.put(f"{PRODUCTS}/{big.id}", json=row, headers=admin_claim_headers)

    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["data"]["product"]["counted_on_bottle_trip"] is False
    assert _stored(db, big.id) == (False, True, Decimal("1.00"), Decimal("15500.00"))


def test_unticking_a_counted_product_in_the_save_that_makes_it_returnable_saves(
    client, db, admin_claim_headers, small, driver
):
    """The edit modal's body when the admin switches Tracks Returnable Bottles on and unticks the setting: the untick
    travels with it, so the product is saved returnable and no longer counted (every trip closed)."""
    trip(db, driver, bottles=5, units={small.id: 4}, status=DriverBottleSessionStatus.CLOSED)
    row = _row(client, admin_claim_headers, small.id)
    row.update(tracks_returnable_bottles=True, returnable_bottles_per_unit=1, counted_on_bottle_trip=False)

    r = client.put(f"{PRODUCTS}/{small.id}", json=row, headers=admin_claim_headers)

    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["data"]["product"]["counted_on_bottle_trip"] is False
    assert _stored(db, small.id) == (False, True, Decimal("1.00"), Decimal("9000.00"))
