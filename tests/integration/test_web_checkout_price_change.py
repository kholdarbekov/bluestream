"""`/checkout` after the price change (plan 2026-11-16, Task 38): the quote the page draws carries the courtesy lines.

`checkout.js` posts `POST /api/v1/orders/cart/estimate` with the body below (its `estimatePayload`) and draws the lines
as the server priced them: the kept units, today's units, the line total, the thank-you flag and the dated price.
The page's drawing is pinned in admin_ui/src/__tests__/web/checkoutPage.test.js over these same fields.
"""

import pytest

from business_app.models.order import Order
from business_app.utils.local_windows import local_date
from tests.integration.price_courtesy_builders import (
    BASKET,
    COURTESY_TOTAL,
    NEW_PRICE,
    OLD_PRICE,
    courtesy_world,
    switch,
    water,
)

pytestmark = pytest.mark.integration

ESTIMATE = "/api/v1/orders/cart/estimate"


def test_the_quote_checkout_js_draws_carries_the_courtesy_lines(app, db, client, sample_product, admin_user):
    """Switched and eligible, no exception granted: the customer's checkout quote is 2 kept units at the old price,
    2 at the new one, 76,000 in all, with the thank-you flag and no further change coming."""
    w = courtesy_world(app, db, water(db, sample_product), admin_user)

    response = client.post(
        ESTIMATE,
        json={
            "items": [{"product_id": w.product.id, "quantity": BASKET}],
            "delivery_address_id": w.customer.address.id,
            "delivery_date": local_date().isoformat(),
            "delivery_window_start": None,
            "delivery_window_end": None,
            "payment_method": "cash",
        },
        headers=w.customer.headers,
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    quote = response.get_json()["data"]
    line = quote["items"][0]
    assert line["product_id"] == w.product.id
    assert line["quantity"] == BASKET
    assert line["courtesy_quantity"] == 2
    assert line["courtesy_unit_price"] == float(OLD_PRICE)
    assert line["unit_price"] == float(NEW_PRICE)
    assert line["regular_quantity"] == 2
    assert line["total_price"] == 76000.0 == float(COURTESY_TOTAL)
    assert line["upcoming_price"] is None
    assert quote["pricing"]["items_subtotal"] == 76000.0
    assert quote["price_courtesy"] == {"applied": True, "expires_on": w.change.courtesy_last_day.isoformat()}


# --------------------------------------------------------------------------- the order (plan Task 39)

ORDERS = "/api/v1/orders/"


@pytest.fixture
def order_client(app):
    """A fresh client per test: the session-scoped one carries JWT cookies from test to test."""
    return app.test_client()


def _checkout_body(w, payment_method, **extra):
    """The body checkout.js `orderBody` posts: the estimate's fields, the delivery note and the channel."""
    return {
        "items": [{"product_id": w.product.id, "quantity": BASKET}],
        "delivery_address_id": w.customer.address.id,
        "delivery_date": local_date().isoformat(),
        "delivery_window_start": None,
        "delivery_window_end": None,
        "payment_method": payment_method,
        "delivery_notes": None,
        "source": "web",
        **extra,
    }


def _quote(client, w, payment_method):
    """`data` of the estimate for the same checkout: what the page drew."""
    body = _checkout_body(w, payment_method)
    for key in ("delivery_notes", "source"):
        body.pop(key)
    response = client.post(ESTIMATE, json=body, headers=w.customer.headers)
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["data"]


def _orders_of(db, user):
    """The person's orders, oldest first: the world's own history is the first of them."""
    db.session.expire_all()
    return Order.query.filter(Order.user_id == user.id).order_by(Order.id).all()


@pytest.mark.parametrize("payment_method", ["cash", "click"])
def test_an_order_confirmed_across_the_switch_is_refused_with_the_new_quote(
    app, db, order_client, sample_product, admin_user, payment_method
):
    """The page drew 4 x 18,000; the switch ran while it was open. The order it posts with that total is refused
    with the new quote, which the page redraws, and nothing is placed."""
    w = courtesy_world(app, db, water(db, sample_product), admin_user, switched=False)
    before = _quote(order_client, w, payment_method)["pricing"]
    assert before["final_total"] == 72000.0
    switch(db)
    after = _quote(order_client, w, payment_method)
    history = len(_orders_of(db, w.customer.user))
    assert after["pricing"]["final_total"] != before["final_total"]

    response = order_client.post(
        ORDERS, json=_checkout_body(w, payment_method, expected_total=before["final_total"]), headers=w.customer.headers
    )

    assert response.status_code == 409, response.get_data(as_text=True)
    body = response.get_json()
    assert body["success"] is False
    assert body["data"]["reason_code"] == "STATE_CHANGED"
    assert body["data"]["reason"] == "price_changed"
    assert body["data"]["quote"]["items"][0]["unit_price"] == float(NEW_PRICE)
    assert body["data"]["quote"]["pricing"]["final_total"] == after["pricing"]["final_total"]
    assert body["data"]["quote"]["pricing"]["final_total"] != before["final_total"]
    assert len(_orders_of(db, w.customer.user)) == history


def test_the_redrawn_total_is_the_one_charged(app, db, order_client, sample_product, admin_user):
    """The tap after the redraw sends the total the 409 carried, and the order is charged exactly that."""
    w = courtesy_world(app, db, water(db, sample_product), admin_user, switched=False)
    stale = _quote(order_client, w, "cash")["pricing"]["final_total"]
    switch(db)
    refused = order_client.post(
        ORDERS, json=_checkout_body(w, "cash", expected_total=stale), headers=w.customer.headers
    )
    redrawn = refused.get_json()["data"]["quote"]["pricing"]["final_total"]

    placed = order_client.post(
        ORDERS, json=_checkout_body(w, "cash", expected_total=redrawn), headers=w.customer.headers
    )

    assert placed.status_code == 201, placed.get_data(as_text=True)
    assert float(_orders_of(db, w.customer.user)[-1].total_amount) == redrawn


def test_a_total_that_still_holds_is_placed(app, db, order_client, sample_product, admin_user):
    w = courtesy_world(app, db, water(db, sample_product), admin_user)
    shown = _quote(order_client, w, "cash")["pricing"]["final_total"]

    placed = order_client.post(
        ORDERS, json=_checkout_body(w, "cash", expected_total=shown), headers=w.customer.headers
    )

    assert placed.status_code == 201, placed.get_data(as_text=True)
    assert float(_orders_of(db, w.customer.user)[-1].total_amount) == shown
