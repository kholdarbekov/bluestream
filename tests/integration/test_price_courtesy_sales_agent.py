"""The sales agent's quote and the store's charge agree on the price-change courtesy (plan 2026-11-16, Task 27).

The agent reads `POST /api/v1/staff/sales/outlets/<id>/order-estimate` aloud and the bot echoes its `total_amount` as
`expected_total` on `POST /api/v1/staff/sales/visits/<id>/order`. A total drawn before the switch is refused with the
quote the agent must read now, and the visit stays at its `order` step. The world comes from
`price_courtesy_builders.py`; no figure here is computed by the code under test.
"""

import pytest

from business_app.models.order import Order
from business_app.models.user import UserAddress
from tests.integration.price_courtesy_builders import (
    BASKET,
    COURTESY_TOTAL,
    announced_change,
    delivered_before_notice,
    entry_of,
    switch,
    water,
)
from tests.integration.test_staff_sales_visits_api import (
    OUTLETS,
    VISITS,
    WORKPLACE_PAYLOAD,
    _approved_outlet,
    _at_order_step,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def client(app):
    """A fresh client per test: the session-scoped one carries JWT cookies from test to test."""
    return app.test_client()


@pytest.fixture
def store(client, db, sales_agent_auth_headers, operator_auth_headers, sample_product, admin_user):
    """A WORKPLACE outlet (no contract) whose user has a delivered order from before the announcement."""
    sample_product.in_sales_stock_check = True
    product = water(db, sample_product)
    outlet = _approved_outlet(client, sales_agent_auth_headers, operator_auth_headers, WORKPLACE_PAYLOAD)
    address = db.session.get(UserAddress, outlet["address_id"])
    user = address.user
    delivered_before_notice(db, user, address, product)
    return outlet, product, user, admin_user


def _items(product):
    return [{"product_id": product.id, "quantity": BASKET}]


def test_the_agents_quote_and_the_charge_agree_with_the_courtesy(
    client, db, store, sales_agent_auth_headers
):
    outlet, product, user, admin = store
    announced_change(db, product, admin)
    switch(db)

    estimate = client.post(
        f"{OUTLETS}/{outlet['id']}/order-estimate",
        json={"items": _items(product), "payment_method": "cash"},
        headers=sales_agent_auth_headers,
    )
    assert estimate.status_code == 200, estimate.get_data(as_text=True)
    quoted = estimate.get_json()["data"]["estimate"]
    line = quoted["items"][0]
    assert (line["courtesy_quantity"], line["courtesy_unit_price"], line["total_price"]) == (2, 18000.0, 76000.0)
    assert quoted["total_amount"] == 76000.0

    visit_id = _at_order_step(client, sales_agent_auth_headers, outlet["id"], product)
    placed = client.post(
        f"{VISITS}/{visit_id}/order",
        json={"items": _items(product), "payment_method": "cash", "expected_total": quoted["total_amount"]},
        headers=sales_agent_auth_headers,
    )

    assert placed.status_code == 201, placed.get_data(as_text=True)
    order = db.session.get(Order, placed.get_json()["data"]["order"]["id"])
    assert order.total_amount == COURTESY_TOTAL
    assert entry_of(db, user).order_id == order.id


def test_an_agent_quote_drawn_before_the_switch_is_refused_and_redrawn(
    client, db, store, sales_agent_auth_headers
):
    outlet, product, user, admin = store
    announced_change(db, product, admin)
    switch(db)
    visit_id = _at_order_step(client, sales_agent_auth_headers, outlet["id"], product)
    body = {"items": _items(product), "payment_method": "cash"}

    refused = client.post(
        f"{VISITS}/{visit_id}/order", json={**body, "expected_total": 72000.0}, headers=sales_agent_auth_headers
    )

    assert refused.status_code == 409, refused.get_data(as_text=True)
    error = refused.get_json()
    assert error["error_code"] == "STATE_CHANGED"
    assert error["details"]["reason"] == "price_changed"
    quote = error["details"]["quote"]
    assert quote["total_amount"] == 76000.0
    assert quote["items"]
    assert quote["client_id"] == user.id
    db.session.expire_all()
    assert Order.query.filter_by(user_id=user.id, visit_id=visit_id).count() == 0
    assert entry_of(db, user).order_id is None
    current = client.get(f"{VISITS}/current", headers=sales_agent_auth_headers)
    assert current.get_json()["data"]["visit"]["current_step"] == "order"

    retried = client.post(
        f"{VISITS}/{visit_id}/order", json={**body, "expected_total": 76000.0}, headers=sales_agent_auth_headers
    )

    assert retried.status_code == 201, retried.get_data(as_text=True)
