"""The operator, sales-agent and admin quote lines state the announced price (spec §4.3, plan Task 34).

Every test drives the route the staff client calls with the body it sends: the operator bot posts
`{"items": [...]}` (with or without `payment_method`) to `POST /api/v1/staff/operator/users/<id>/order-estimate`, the
sales bot to `POST /api/v1/staff/sales/outlets/<id>/order-estimate`, the admin modal to `POST /api/v1/admin/orders/quote`.
The expected block is the literal `price_change_builders.BLOCK`; no figure here is computed by the code under test.
"""

from decimal import Decimal

import pytest

from tests.integration.contract_pricing_factory import make_contract, make_workplace
from tests.integration.price_change_builders import BLOCK, bottles, make_admin, make_price_change
from tests.integration.reorder_reminder_builders import claim_headers
from tests.integration.test_staff_sales_visits_api import OUTLETS, WORKPLACE_PAYLOAD, _approved_outlet, _fund_business_account

pytestmark = pytest.mark.integration

OPERATOR_ESTIMATE = "/api/v1/staff/operator/users/{}/order-estimate"
ADMIN_QUOTE = "/api/v1/admin/orders/quote"


@pytest.fixture
def announced(db):
    """p19 18,000 -> 20,000 announced for 2027-02-01; p10 untouched."""
    b = bottles(db)
    make_price_change(db, [(b.p19, Decimal("20000.00"), 2, None)], created_by=make_admin(db))
    db.session.commit()
    return b


@pytest.fixture
def client(app):
    """A fresh client per test: the session-scoped one carries JWT cookies from test to test."""
    return app.test_client()


def _operator_estimate(client, headers, user, items, method):
    body = {"items": [{"product_id": p.id, "quantity": q} for p, q in items]}
    if method is not None:
        body["payment_method"] = method
    response = client.post(OPERATOR_ESTIMATE.format(user.id), json=body, headers=headers)
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["data"]


@pytest.mark.parametrize("method", [None, "cash"])
def test_the_operator_quote_states_the_announced_price(
    client, announced, sample_user, operator_auth_headers, method
):
    data = _operator_estimate(
        client, operator_auth_headers, sample_user, [(announced.p19, 2), (announced.p10, 4)], method
    )

    by_product = {line["product_id"]: line for line in data["items"]}
    assert by_product[announced.p19.id]["upcoming_price"] == BLOCK
    assert by_product[announced.p10.id]["upcoming_price"] is None


@pytest.mark.parametrize("method, expected", [(None, None), ("cash", None), ("click", BLOCK)])
def test_the_operator_quote_of_a_contract_line_states_none(
    db, client, announced, operator_auth_headers, method, expected
):
    user, _address = make_workplace(db)
    make_contract(db, user, announced.p19)

    data = _operator_estimate(client, operator_auth_headers, user, [(announced.p19, 2)], method)

    line = data["items"][0]
    assert line["upcoming_price"] == expected
    if method == "click":
        # Click charges the catalogue, so the line is not a contract price and the coming price is told.
        assert line["is_contract_price"] is False


def test_the_operator_quote_of_a_business_account_line_states_none(db, client, announced, operator_auth_headers):
    user, _address = make_workplace(db)
    make_contract(db, user, announced.p19)

    data = _operator_estimate(client, operator_auth_headers, user, [(announced.p19, 2)], "business_account")

    assert data["items"][0]["upcoming_price"] is None


def test_the_sales_agent_quote_states_it(
    db, client, announced, sales_agent_auth_headers, operator_auth_headers
):
    outlet = _approved_outlet(client, sales_agent_auth_headers, operator_auth_headers)

    response = client.post(
        f"{OUTLETS}/{outlet['id']}/order-estimate",
        json={"items": [{"product_id": announced.p19.id, "quantity": 2}, {"product_id": announced.p10.id, "quantity": 4}]},
        headers=sales_agent_auth_headers,
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    by_product = {line["product_id"]: line for line in response.get_json()["data"]["estimate"]["items"]}
    assert by_product[announced.p19.id]["upcoming_price"] == BLOCK
    assert by_product[announced.p10.id]["upcoming_price"] is None


def test_a_contract_covered_outlet_quoted_with_no_method_states_none(
    db, client, announced, sales_agent_auth_headers, operator_auth_headers
):
    outlet = _approved_outlet(client, sales_agent_auth_headers, operator_auth_headers, WORKPLACE_PAYLOAD)
    _fund_business_account(db, outlet["user_id"], announced.p19)

    response = client.post(
        f"{OUTLETS}/{outlet['id']}/order-estimate",
        json={"items": [{"product_id": announced.p19.id, "quantity": 2}]},
        headers=sales_agent_auth_headers,
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()["data"]["estimate"]["items"][0]["upcoming_price"] is None


def _admin_quote(app, db, client, user, address, product, method="cash"):
    admin = make_admin(db)
    body = {
        "user_id": user.id,
        "items": [{"product_id": product.id, "quantity": 2}],
        "delivery_address_id": address.id,
    }
    if method is not None:
        body["payment_method"] = method
    response = client.post(ADMIN_QUOTE, json=body, headers=claim_headers(app, admin, "admin"))
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["data"]


def test_the_admin_quote_states_the_announced_price(app, db, client, announced):
    user, address = make_workplace(db)

    data = _admin_quote(app, db, client, user, address, announced.p19)

    assert data["items"][0]["upcoming_price"] == BLOCK


def test_the_admin_quote_of_a_contract_client_on_cash_states_none(app, db, client, announced):
    user, address = make_workplace(db)
    make_contract(db, user, announced.p19)

    data = _admin_quote(app, db, client, user, address, announced.p19, "cash")

    assert data["items"][0]["is_contract_price"] is True
    assert data["items"][0]["upcoming_price"] is None


def test_the_admin_quote_of_a_business_account_workplace_with_no_method_states_none(app, db, client, announced):
    user, address = make_workplace(db)
    make_contract(db, user, announced.p19)

    data = _admin_quote(app, db, client, user, address, announced.p19, None)

    assert data["payment_method"] == "business_account"
    assert data["items"][0]["upcoming_price"] is None
