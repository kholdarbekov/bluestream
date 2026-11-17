"""The cart and the estimate lines state the announced price (spec §4.3, plan Task 33).

Every test drives the route the client calls with the body it sends: the bot and `checkout.js` post
`{"items": [{"product_id", "quantity"}]}` (with or without `payment_method`) to `POST /api/v1/orders/cart/estimate`;
the signed-in cart is `POST /api/v1/cart/items` then `GET /api/v1/cart/`. The expected block is the literal
`price_change_builders.BLOCK`; no figure here is computed by the code under test.
"""

import pytest

from tests.integration.contract_pricing_factory import customer_headers, make_contract, make_workplace
from tests.integration.price_change_builders import BLOCK, bottles, make_admin, make_price_change
from tests.integration.price_courtesy_builders import courtesy_world, water
from decimal import Decimal

pytestmark = pytest.mark.integration

ESTIMATE = "/api/v1/orders/cart/estimate"
CART = "/api/v1/cart/"


@pytest.fixture
def announced(db):
    """p19 18,000 -> 20,000 announced for 2027-02-01; p10 untouched."""
    b = bottles(db)
    make_price_change(db, [(b.p19, Decimal("20000.00"), 2, None)], created_by=make_admin(db))
    db.session.commit()
    return b


def _add(client, headers, product, quantity):
    added = client.post("/api/v1/cart/items", json={"product_id": product.id, "quantity": quantity}, headers=headers)
    assert added.status_code in (200, 201), added.get_data(as_text=True)


def _cart_lines(client, headers):
    response = client.get(CART, headers=headers)
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["data"]["cart"]


def _estimate(client, headers, items, payment_method="absent"):
    body = {"items": [{"product_id": p.id, "quantity": q} for p, q in items]}
    if payment_method != "absent":
        body["payment_method"] = payment_method
    response = client.post(ESTIMATE, json=body, headers=headers)
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["data"]


def test_the_cart_lines_state_the_announced_price(app, db, client, announced, sample_user):
    headers = customer_headers(app, sample_user)
    _add(client, headers, announced.p19, 2)
    _add(client, headers, announced.p10, 4)

    cart = _cart_lines(client, headers)

    by_product = {line["product_id"]: line for line in cart["cart_items"]}
    assert by_product[announced.p19.id]["upcoming_price"] == BLOCK
    assert by_product[announced.p10.id]["upcoming_price"] is None
    assert by_product[announced.p19.id]["courtesy_quantity"] is None
    assert cart["price_courtesy"] is None


@pytest.mark.parametrize("method", [None, "cash", "click"])
def test_the_estimate_lines_state_it_on_every_rail_and_before_one_is_chosen(
    app, db, client, announced, sample_user, method
):
    headers = customer_headers(app, sample_user)
    data = _estimate(client, headers, [(announced.p19, 2), (announced.p10, 4)], method if method else "absent")

    by_product = {line["product_id"]: line for line in data["items"]}
    assert by_product[announced.p19.id]["upcoming_price"] == BLOCK
    assert by_product[announced.p10.id]["upcoming_price"] is None


@pytest.mark.parametrize("method, expected", [("absent", None), ("cash", None), ("business_account", None), ("click", BLOCK)])
def test_a_contract_customers_lines_state_no_coming_price(app, db, client, announced, method, expected):
    user, _address = make_workplace(db)
    make_contract(db, user, announced.p19)
    headers = customer_headers(app, user)

    estimate = _estimate(client, headers, [(announced.p19, 2)], method)
    assert estimate["items"][0]["upcoming_price"] == expected

    if method == "absent":
        _add(client, headers, announced.p19, 2)
        cart = _cart_lines(client, headers)
        assert cart["cart_items"][0]["upcoming_price"] is None


def test_after_the_switch_no_line_states_a_coming_price(app, db, client, sample_product, admin_user):
    w = courtesy_world(app, db, water(db, sample_product), admin_user)
    headers = w.customer.headers
    _add(client, headers, w.product, 4)

    line = _cart_lines(client, headers)["cart_items"][0]

    assert line["upcoming_price"] is None
    assert (line["courtesy_quantity"], line["courtesy_unit_price"], line["regular_quantity"]) == (2, 18000.0, 2)
    estimate = _estimate(client, headers, [(w.product, 4)], "cash")
    assert estimate["items"][0]["upcoming_price"] is None


def _validate(client, headers, items):
    body = {"cart_items": [{"product_id": p.id, "quantity": q} for p, q in items]}
    response = client.post("/api/v1/cart/validate", json=body, headers=headers)
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["data"]["items"]


def test_validate_states_no_coming_price_when_nothing_is_announced(app, db, client, sample_user):
    b = bottles(db)
    db.session.commit()
    headers = customer_headers(app, sample_user)

    items = _validate(client, headers, [(b.p19, 2)])

    assert [line["upcoming_price"] for line in items] == [None]


def test_validate_states_the_announced_price(app, db, client, announced, sample_user):
    headers = customer_headers(app, sample_user)

    items = _validate(client, headers, [(announced.p19, 2), (announced.p10, 4)])

    by_product = {line["product_id"]: line for line in items}
    assert by_product[announced.p19.id]["upcoming_price"] == BLOCK
    assert by_product[announced.p10.id]["upcoming_price"] is None
