"""Pre-method screens show the catalogue and publish the hint (spec 2026-10-09 D5, §4.4)."""

import pytest

from business_app import db as _db
from shared.enums import UserRole
from tests.integration.contract_pricing_factory import (
    BANK,
    CASH,
    CATALOGUE,
    admin_headers,
    customer_headers,
    make_contract,
    make_workplace,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def world(db, sample_product):
    user, address = make_workplace(db)
    _contract, row = make_contract(db, user, sample_product)
    return user, row


def _served_product(app, user, product):
    resp = app.test_client().get(f"/api/v1/products/{product.id}", headers=customer_headers(app, user))
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()["data"]["product"]


def test_a_product_card_states_the_catalogue_and_flags_the_contract_price(app, db, world, sample_product):
    user, _row = world
    product = _served_product(app, user, sample_product)
    assert product["pricing"]["current_price"] == float(CATALOGUE)
    assert product["pricing"]["contract_price_at_checkout"] is True


def test_no_hint_when_the_row_carries_no_contract_price(app, db, world, sample_product):
    user, row = world
    row.business_account_unit_price = None
    row.cash_unit_price = None
    _db.session.commit()
    assert _served_product(app, user, sample_product)["pricing"]["contract_price_at_checkout"] is False


def test_no_hint_for_a_customer_without_a_contract(app, db, sample_user, sample_product):
    assert _served_product(app, sample_user, sample_product)["pricing"]["contract_price_at_checkout"] is False


def test_the_cart_states_the_catalogue_and_flags_the_contract_price(app, db, world, sample_product):
    user, _row = world
    client = app.test_client()
    added = client.post(
        "/api/v1/cart/items",
        json={"product_id": sample_product.id, "quantity": 2},
        headers=customer_headers(app, user),
    )
    assert added.status_code in (200, 201), added.get_data(as_text=True)

    resp = client.get("/api/v1/cart/", headers=customer_headers(app, user))
    assert resp.status_code == 200, resp.get_data(as_text=True)
    cart = resp.get_json()["data"]["cart"]
    assert cart["subtotal"] == float(CATALOGUE) * 2
    assert cart["contract_price_at_checkout"] is True


@pytest.mark.parametrize(
    "method, price, source",
    [("cash", CASH, "contract_cash"), ("business_account", BANK, "contract_business_account"), (None, CATALOGUE, "fallback")],
)
def test_the_admin_picker_prices_the_chosen_method(app, db, admin_user, world, sample_product, method, price, source):
    user, _row = world
    params = {"per_page": 100, "is_active": "true", "pricing_user_id": user.id}
    if method is not None:
        params["payment_method"] = method
    resp = app.test_client().get(
        "/api/v1/admin/products", query_string=params, headers=admin_headers(app, admin_user)
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    item = next(p for p in resp.get_json()["data"]["items"] if p["id"] == sample_product.id)
    assert item["effective_unit_price"] == float(price)
    assert item["pricing_source"] == source
