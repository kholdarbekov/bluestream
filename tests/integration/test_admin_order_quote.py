"""The admin basket quote `POST /api/v1/admin/orders/quote` and the admin create guard (plan Task 25).

The operator's create-order modal asks the quote for the basket it is building, shows its lines, and posts the order
with `expected_total` = the quote's `total_amount`. A total that no longer holds is refused 409 `STATE_CHANGED` with
`details.quote` (the top-level envelope `apiErrorCode` reads). Every test drives the real routes with the bodies the
modal sends; the world comes from `price_courtesy_builders.py`.
"""

import pytest

from business_app.models.order import Order, OrderItem
from tests.integration.price_courtesy_builders import BASKET, courtesy_world, entry_of, switch, water
from tests.integration.price_change_builders import make_admin
from tests.integration.reorder_reminder_builders import claim_headers

pytestmark = pytest.mark.integration

QUOTE = "/api/v1/admin/orders/quote"
CREATE = "/api/v1/admin/orders"


@pytest.fixture
def client(app):
    """A fresh client per test: the session-scoped one carries JWT cookies from test to test."""
    return app.test_client()


@pytest.fixture
def product(db, sample_product):
    return water(db, sample_product)


def _body(w, quantity=BASKET, **extra):
    return {
        "user_id": w.customer.user.id,
        "items": [{"product_id": w.product.id, "quantity": quantity}],
        "delivery_address_id": w.customer.address.id,
        "payment_method": "cash",
        **extra,
    }


def _world(app, db, product, **kwargs):
    """The courtesy world; `w.before` holds the ids of the orders the person already has (the delivered one)."""
    w = courtesy_world(app, db, product, make_admin(db), **kwargs)
    w.before = {order.id for order in Order.query.filter(Order.user_id == w.customer.user.id).all()}
    return w


def _new_orders(db, w):
    """The orders the person has beyond the world's own."""
    db.session.expire_all()
    return [o for o in Order.query.filter(Order.user_id == w.customer.user.id).all() if o.id not in w.before]


def test_the_quote_shows_the_courtesy_lines_and_the_status(app, db, client, product):
    w = _world(app, db, product)
    headers = claim_headers(app, w.admin, "admin")

    response = client.post(QUOTE, json=_body(w), headers=headers)

    assert response.status_code == 200, response.get_json()
    data = response.get_json()["data"]
    assert data["items"] == [
        {
            "product_id": product.id,
            "product_name": product.name,
            "quantity": 4,
            "unit_price": 20000.0,
            "discount_amount": 4000.0,
            "courtesy_quantity": 2,
            "courtesy_unit_price": 18000.0,
            "regular_quantity": 2,
            "total_price": 76000.0,
            "is_contract_price": False,
            "upcoming_price": None,
        }
    ]
    assert data["total_amount"] == 76000.0
    assert data["subtotal"] == 76000.0
    assert data["payment_method"] == "cash"
    assert data["price_courtesy"] == {"applied": True, "expires_on": w.change.courtesy_last_day.isoformat()}
    assert data["courtesy_status"] == {
        "state": "available",
        "expires_on": w.change.courtesy_last_day.isoformat(),
        "caps": [{"product_id": product.id, "product_name": product.name, "cap": 2}],
        "used_order_id": None,
        "used_order_number": None,
    }
    assert _new_orders(db, w) == []
    assert entry_of(db, w.customer.user).order_id is None
    assert OrderItem.query.filter(OrderItem.courtesy_quantity.isnot(None)).count() == 0


def test_an_operator_may_quote_and_a_customer_may_not(app, db, client, product, operator_user):
    w = _world(app, db, product)

    operator = client.post(QUOTE, json=_body(w), headers=claim_headers(app, operator_user, "operator"))
    customer = client.post(QUOTE, json=_body(w), headers=claim_headers(app, w.customer.user, "customer"))

    assert operator.status_code == 200, operator.get_json()
    assert customer.status_code == 403, customer.get_json()


def test_the_quote_is_what_the_create_charges(app, db, client, product):
    w = _world(app, db, product)
    headers = claim_headers(app, w.admin, "admin")
    quoted = client.post(QUOTE, json=_body(w), headers=headers).get_json()["data"]

    created = client.post(CREATE, json=_body(w, expected_total=quoted["total_amount"]), headers=headers)

    assert created.status_code == 201, created.get_json()
    db.session.expire_all()
    [order] = _new_orders(db, w)
    [line] = order.order_items
    quoted_line = quoted["items"][0]
    assert float(line.unit_price) == quoted_line["unit_price"]
    assert line.courtesy_quantity == quoted_line["courtesy_quantity"]
    assert float(line.courtesy_unit_price) == quoted_line["courtesy_unit_price"]
    assert float(line.total_price) == quoted_line["total_price"]
    assert float(order.total_amount) == quoted["total_amount"]

    again = client.post(QUOTE, json=_body(w), headers=headers).get_json()["data"]
    assert again["courtesy_status"]["state"] == "used"
    assert again["courtesy_status"]["used_order_number"] == order.order_number
    assert again["total_amount"] == 80000.0
    assert again["price_courtesy"] is None


def test_a_quote_drawn_before_the_switch_is_refused_at_create_and_redrawn(app, db, client, product):
    w = _world(app, db, product, switched=False)
    headers = claim_headers(app, w.admin, "admin")
    before = client.post(QUOTE, json=_body(w), headers=headers).get_json()["data"]
    assert before["total_amount"] == 72000.0
    switch(db)

    refused = client.post(CREATE, json=_body(w, expected_total=before["total_amount"]), headers=headers)

    assert refused.status_code == 409, refused.get_json()
    body = refused.get_json()
    assert body["error_code"] == "STATE_CHANGED"
    assert body["details"]["reason"] == "price_changed"
    assert body["details"]["quote"]["total_amount"] == 76000.0
    assert _new_orders(db, w) == []
    assert entry_of(db, w.customer.user).order_id is None

    accepted = client.post(CREATE, json=_body(w, expected_total=body["details"]["quote"]["total_amount"]), headers=headers)
    assert accepted.status_code == 201, accepted.get_json()


def test_the_quote_refuses_what_the_create_refuses(app, db, client, product):
    w = _world(app, db, product)
    headers = claim_headers(app, w.admin, "admin")

    quote = client.post(QUOTE, json=_body(w, quantity=1), headers=headers)
    create = client.post(CREATE, json=_body(w, quantity=1), headers=headers)

    assert quote.status_code == 400, quote.get_json()
    assert create.status_code == 400, create.get_json()
    assert quote.get_json()["data"]["error_code"] == "ORDER_ITEM_BELOW_MIN_QUANTITY"
    assert create.get_json()["data"]["error_code"] == "ORDER_ITEM_BELOW_MIN_QUANTITY"
