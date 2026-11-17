"""The cart and the checkout estimate quote the courtesy through the one pricing path (spec §5.6, plan Task 22).

Every test drives the route a real client calls with the body it sends: the bot and `checkout.js` post
`{"items": [{"product_id", "quantity"}], "payment_method": "cash"}` to `POST /api/v1/orders/cart/estimate`; the twin
`POST /api/v1/cart/estimate` takes `cart_items`; the signed-in cart is `POST /api/v1/cart/items` then `GET /api/v1/cart/`.
The world comes from `price_courtesy_builders.py`; no figure here is computed by the code under test.
"""

from decimal import Decimal

import pytest

from business_app.models.price_change import PriceChangeStatus
from business_app.models.product import PriceRule
from business_app.utils.constants import PriceRuleType
from tests.integration.price_courtesy_builders import (
    BASKET,
    COURTESY_TOTAL,
    NEW_PRICE,
    NEW_TOTAL,
    OLD_PRICE,
    OLD_TOTAL,
    courtesy_world,
    delivered_before_notice,
    entry_of,
    person,
    water,
)

pytestmark = pytest.mark.integration

ESTIMATE = "/api/v1/orders/cart/estimate"
TWIN_ESTIMATE = "/api/v1/cart/estimate"
COURTESY_LINE = (20000.0, 2, 18000.0, 2, 4000.0, 76000.0, 76000.0)
LINE_KEYS = (
    "unit_price",
    "courtesy_quantity",
    "courtesy_unit_price",
    "regular_quantity",
    "discount_amount",
    "subtotal",
    "total_price",
)


def _line(line):
    return tuple(line[key] for key in LINE_KEYS)


@pytest.fixture
def w(app, db, client, sample_product, admin_user):
    return courtesy_world(app, db, water(db, sample_product), admin_user)


def _estimate(client, w, **body):
    payload = {"items": [{"product_id": w.product.id, "quantity": BASKET}], "payment_method": "cash", **body}
    return client.post(ESTIMATE, json=payload, headers=w.customer.headers)


def test_the_estimate_shows_the_courtesy_lines_for_an_eligible_customer(client, w):
    response = _estimate(client, w)

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert _line(data["items"][0]) == COURTESY_LINE
    assert data["pricing"]["items_subtotal"] == data["pricing"]["final_total"] == float(COURTESY_TOTAL)
    assert data["price_courtesy"] == {"applied": True, "expires_on": w.change.courtesy_last_day.isoformat()}


def test_the_twin_cart_estimate_route_answers_the_same_figures(client, w):
    response = client.post(
        TWIN_ESTIMATE,
        json={"cart_items": [{"product_id": w.product.id, "quantity": BASKET}], "payment_method": "cash"},
        headers=w.customer.headers,
    )

    assert response.status_code == 200
    estimate = response.get_json()["data"]["estimate"]
    assert _line(estimate["items"][0]) == COURTESY_LINE
    assert estimate["pricing"]["items_subtotal"] == estimate["pricing"]["final_total"] == float(COURTESY_TOTAL)
    assert estimate["price_courtesy"] == {"applied": True, "expires_on": w.change.courtesy_last_day.isoformat()}


def test_the_cart_shows_the_courtesy_lines(client, w):
    added = client.post(
        "/api/v1/cart/items", json={"product_id": w.product.id, "quantity": BASKET}, headers=w.customer.headers
    )
    assert added.status_code == 200

    response = client.get("/api/v1/cart/", headers=w.customer.headers)

    assert response.status_code == 200
    cart = response.get_json()["data"]["cart"]
    item = cart["cart_items"][0]
    assert (
        item["unit_price"],
        item["courtesy_quantity"],
        item["courtesy_unit_price"],
        item["regular_quantity"],
        item["discount_amount"],
        item["total_price"],
    ) == (20000.0, 2, 18000.0, 2, 4000.0, 76000.0)
    assert cart["subtotal"] == cart["estimated_total"] == float(COURTESY_TOTAL)
    assert cart["price_courtesy"]["applied"] is True


def test_the_validate_route_publishes_the_courtesy_line(client, w):
    response = client.post(
        "/api/v1/cart/validate",
        json={"cart_items": [{"product_id": w.product.id, "quantity": BASKET}]},
        headers=w.customer.headers,
    )

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert _line(data["items"][0]) == COURTESY_LINE
    assert data["subtotal"] == float(COURTESY_TOTAL)


def test_a_quote_never_consumes_the_courtesy(client, db, w):
    first = _estimate(client, w).get_json()["data"]
    second = _estimate(client, w).get_json()["data"]

    assert _line(first["items"][0]) == _line(second["items"][0]) == COURTESY_LINE
    assert entry_of(db, w.customer.user).order_id is None


def test_an_ineligible_customer_sees_the_new_price_and_nothing_else(client, app, db, sample_product, admin_user):
    w = courtesy_world(app, db, water(db, sample_product), admin_user, eligible=False)

    data = _estimate(client, w).get_json()["data"]

    assert _line(data["items"][0]) == (20000.0, None, None, None, 0.0, 80000.0, 80000.0)
    assert data["pricing"]["items_subtotal"] == data["pricing"]["final_total"] == float(NEW_TOTAL)
    assert data["price_courtesy"] is None


def test_an_active_price_rule_no_longer_changes_the_cart_or_the_estimate(client, app, db, sample_product):
    product = water(db, sample_product)
    buyer = person(app, db, "+998901515002")
    db.session.add(
        PriceRule(
            product_id=product.id,
            rule_type=PriceRuleType.SEASONAL_DISCOUNT,
            name="Season",
            min_quantity=1,
            discount_type="fixed",
            discount_value=Decimal("2000.00"),
            is_active=True,
        )
    )
    db.session.commit()

    estimate = client.post(
        ESTIMATE,
        json={"items": [{"product_id": product.id, "quantity": BASKET}], "payment_method": "cash"},
        headers=buyer.headers,
    ).get_json()["data"]
    client.post("/api/v1/cart/items", json={"product_id": product.id, "quantity": BASKET}, headers=buyer.headers)
    cart = client.get("/api/v1/cart/", headers=buyer.headers).get_json()["data"]["cart"]

    assert estimate["items"][0]["unit_price"] == float(OLD_PRICE)
    assert estimate["pricing"]["final_total"] == float(OLD_TOTAL)
    assert cart["cart_items"][0]["unit_price"] == float(OLD_PRICE)
    assert cart["estimated_total"] == float(OLD_TOTAL)


def test_quick_reorder_states_the_charged_price(client, app, db, sample_product):
    product = water(db, sample_product)
    product.discount_price = Decimal("17000.00")
    db.session.commit()
    buyer = person(app, db, "+998901515003")
    delivered_before_notice(db, buyer.user, buyer.address, product)

    response = client.get("/api/v1/orders/quick-reorder", headers=buyer.headers)

    assert response.status_code == 200
    suggestion = response.get_json()["data"]["quick_reorder_suggestions"][0]
    assert suggestion["product_id"] == product.id
    assert suggestion["current_price"] == 17000.0
