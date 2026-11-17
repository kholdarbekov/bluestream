"""A direct price edit is refused while a price change holds the product; the admin row publishes the charged
price and the lock (spec 2026-11-16 §5.2, §5.13).

Driven through the routes the admin UI calls: the change is scheduled with the Task 8 route, the product is read
from `GET /api/v1/admin/products` (the call `Products.js` makes; there is no single-product GET) and written back
with `PUT /api/v1/admin/products/<id>`. Price-change rows come from the route, not from builders (Task 13).
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from flask import current_app

from business_app import db as _db
from business_app.models.price_change import PriceChange
from business_app.models.product import Product
from business_app.services.price_change.schedule import PRICE_LOCKED_MESSAGE, PRODUCT_PRICE_LOCKED_BY_PRICE_CHANGE
from business_app.utils.local_windows import local_date
from tests.integration.reorder_reminder_builders import make_product

pytestmark = pytest.mark.integration

CHANGES = "/api/v1/admin/price-changes"
PRODUCTS = "/api/v1/admin/products"


def _client():
    return current_app.test_client()


def _schedule(headers, product):
    today = local_date()
    body = {
        "announce_on": (today + timedelta(days=7)).isoformat(),
        "change_on": (today + timedelta(days=35)).isoformat(),
        "items": [{"product_id": product.id, "new_price": 20000, "courtesy_cap": 2}],
    }
    response = _client().post(CHANGES, json=body, headers=headers)
    assert response.status_code == 201, response.get_data(as_text=True)
    return response.get_json()["data"]["price_change"]["id"]


def _row(headers, product_id):
    listing = _client().get(PRODUCTS, headers=headers)
    assert listing.status_code == 200, listing.get_data(as_text=True)
    return next(item for item in listing.get_json()["data"]["items"] if item["id"] == product_id)


def _put(headers, product_id, body):
    return _client().put(f"{PRODUCTS}/{product_id}", json=body, headers=headers)


def _stored(product_id):
    _db.session.expire_all()
    product = _db.session.get(Product, product_id)
    return product.base_price, product.discount_price


@pytest.mark.parametrize(
    "body",
    [{"price": 21000}, {"base_price": 21000}, {"discount_price": 14000}],
    ids=["price", "base_price", "discount_price"],
)
def test_a_direct_price_edit_is_refused_while_a_change_is_pending(
    db, manager_claim_headers, admin_auth_headers, sample_product, body
):
    _schedule(manager_claim_headers, sample_product)

    response = _put(admin_auth_headers, sample_product.id, body)

    assert response.status_code == 400, response.get_data(as_text=True)
    payload = response.get_json()
    assert payload["success"] is False
    assert payload["message"] == "Validation failed"
    assert payload["errors"] == [f"price: {PRICE_LOCKED_MESSAGE}"]
    assert payload["data"]["error_code"] == PRODUCT_PRICE_LOCKED_BY_PRICE_CHANGE == "PRODUCT_PRICE_LOCKED_BY_PRICE_CHANGE"
    assert _stored(sample_product.id) == (Decimal("15000.00"), None)


def test_the_message_is_the_spec_sentence():
    assert PRICE_LOCKED_MESSAGE == "A price change is scheduled for this product. Change its price on the Price changes page."


def test_the_modals_own_round_trip_still_saves(db, manager_claim_headers, admin_auth_headers, sample_product):
    _schedule(manager_claim_headers, sample_product)
    row = _row(admin_auth_headers, sample_product.id)
    row["name"] = "Renamed Water"

    response = _put(admin_auth_headers, sample_product.id, row)

    assert response.status_code == 200, response.get_data(as_text=True)
    _db.session.expire_all()
    assert _db.session.get(Product, sample_product.id).name == "Renamed Water"
    assert _stored(sample_product.id) == (Decimal("15000.00"), None)


def test_an_echoed_unchanged_price_with_a_rename_saves(db, manager_claim_headers, admin_auth_headers, sample_product):
    _schedule(manager_claim_headers, sample_product)

    response = _put(
        admin_auth_headers, sample_product.id, {"name": "Echo", "price": 15000, "discount_price": None}
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    _db.session.expire_all()
    assert _db.session.get(Product, sample_product.id).name == "Echo"


def test_a_product_outside_the_change_is_editable(
    db, manager_claim_headers, admin_auth_headers, sample_product, sample_category
):
    other = make_product(db, sample_category, "Other Water")
    _schedule(manager_claim_headers, sample_product)

    response = _put(admin_auth_headers, other.id, {"price": 16000})

    assert response.status_code == 200, response.get_data(as_text=True)
    assert _stored(other.id) == (Decimal("16000.00"), None)


def test_the_lock_lifts_when_the_change_is_cancelled(db, manager_claim_headers, admin_auth_headers, sample_product):
    pc_id = _schedule(manager_claim_headers, sample_product)
    assert _put(admin_auth_headers, sample_product.id, {"price": 17000}).status_code == 400

    cancelled = _client().post(f"{CHANGES}/{pc_id}/cancel", json={}, headers=manager_claim_headers)
    assert cancelled.status_code == 200, cancelled.get_data(as_text=True)
    response = _put(admin_auth_headers, sample_product.id, {"price": 17000})

    assert response.status_code == 200, response.get_data(as_text=True)
    assert _stored(sample_product.id) == (Decimal("17000.00"), None)


def test_the_admin_row_publishes_the_charged_price_and_the_lock(
    db, manager_claim_headers, admin_auth_headers, sample_product, sample_category
):
    sample_product.discount_price = Decimal("14000.00")
    db.session.commit()
    held = make_product(db, sample_category, "Held Water")
    pc_id = _schedule(manager_claim_headers, held)
    price_change = _db.session.get(PriceChange, pc_id)

    discounted = _row(admin_auth_headers, sample_product.id)
    pending = _row(admin_auth_headers, held.id)

    assert (discounted["price"], discounted["current_price"], discounted["catalogue_unit_price"]) == (
        15000.0,
        14000.0,
        14000.0,
    )
    assert discounted["price_change_pending"] is None
    assert pending["price_change_pending"] == {
        "price_change_id": pc_id,
        "change_on": price_change.change_on.isoformat(),
    }
