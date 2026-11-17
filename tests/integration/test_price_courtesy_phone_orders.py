"""The phone order quotes and charges the price-change courtesy (spec §5.6, plan 2026-11-16 Task 26).

`StaffService.price_phone_order` is the one pricing of a phone order: the operator's estimate
(`POST /api/v1/staff/operator/users/<id>/order-estimate`) replays it without a claim, and
`POST /api/v1/staff/operator/orders` runs it with `claim=True`, so what the operator read out is what the client is
charged. The create body is the one `staff_bot/handlers/operator/create_order.py` sends, plus `expected_total`.
The world comes from `price_courtesy_builders.py`; no figure here is computed by the code under test.
"""

from decimal import Decimal

import pytest

from business_app.models.order import Order, OrderItem
from business_app.services.price_change import courtesy as courtesy_module
from tests.integration.contract_pricing_factory import BANK, CATALOGUE, QUANTITY, make_contract, make_workplace
from tests.integration.price_courtesy_builders import (
    BASKET,
    COURTESY_TOTAL,
    NEW_PRICE,
    NEW_TOTAL,
    OLD_PRICE,
    courtesy_world,
    entry_of,
    water,
)
from tests.integration.reorder_reminder_builders import live_order

pytestmark = pytest.mark.integration

ESTIMATE = "/api/v1/staff/operator/users/{}/order-estimate"
CREATE = "/api/v1/staff/operator/orders"


@pytest.fixture
def client(app):
    """A fresh client per test: the session-scoped one carries JWT cookies from test to test."""
    return app.test_client()


@pytest.fixture
def w(app, db, sample_product, admin_user, operator_user):
    return courtesy_world(app, db, water(db, sample_product), admin_user)


def _basket(product, quantity=BASKET):
    return [{"product_id": product.id, "quantity": quantity}]


def _estimate(client, headers, customer_id, product, **extra):
    return client.post(
        ESTIMATE.format(customer_id),
        json={"items": _basket(product), "payment_method": "cash", **extra},
        headers=headers,
    )


def _create_body(customer, product, **extra):
    """The body the operator bot posts when it confirms (`create_order.py`), plus the total the screen showed."""
    return {
        "client_id": customer.user.id,
        "items": _basket(product),
        "delivery_address_id": customer.address.id,
        "payment_method": "cash",
        "delivery_notes": None,
        **extra,
    }


def _stored_line(db, order_id):
    db.session.expire_all()
    (line,) = OrderItem.query.filter_by(order_id=order_id).all()
    return line.unit_price, line.courtesy_quantity, line.courtesy_unit_price, line.discount_amount, line.total_price


def test_the_operator_estimate_shows_the_courtesy_and_its_status(client, db, w, operator_auth_headers):
    response = _estimate(client, operator_auth_headers, w.customer.user.id, w.product)

    assert response.status_code == 200
    data = response.get_json()["data"]
    line = data["items"][0]
    assert (
        line["courtesy_quantity"],
        line["courtesy_unit_price"],
        line["regular_quantity"],
        line["discount_amount"],
        line["total_price"],
        line["unit_price"],
    ) == (2, 18000.0, 2, 4000.0, 76000.0, 20000.0)
    assert data["total_amount"] == 76000.0
    assert data["price_courtesy"] == {"applied": True, "expires_on": w.change.courtesy_last_day.isoformat()}
    status = data["courtesy_status"]
    assert status["state"] == "available"
    assert status["caps"] == [{"product_id": w.product.id, "product_name": w.product.name, "cap": 2}]
    assert entry_of(db, w.customer.user).order_id is None


def test_the_phone_order_charges_what_the_estimate_quoted(client, db, w, operator_auth_headers):
    quoted = _estimate(client, operator_auth_headers, w.customer.user.id, w.product).get_json()["data"]["total_amount"]

    response = client.post(
        CREATE, json=_create_body(w.customer, w.product, expected_total=quoted), headers=operator_auth_headers
    )

    assert response.status_code == 201, response.get_json()
    data = response.get_json()["data"]
    assert data["total_amount"] == 76000.0
    assert _stored_line(db, data["id"]) == (NEW_PRICE, 2, OLD_PRICE, Decimal("4000.00"), COURTESY_TOTAL)
    assert entry_of(db, w.customer.user).order_id == data["id"]


def test_an_estimate_after_use_names_the_order_that_used_it(client, db, w, operator_auth_headers):
    created = client.post(CREATE, json=_create_body(w.customer, w.product), headers=operator_auth_headers)
    assert created.status_code == 201, created.get_json()
    order = db.session.get(Order, created.get_json()["data"]["id"])

    response = _estimate(client, operator_auth_headers, w.customer.user.id, w.product)

    data = response.get_json()["data"]
    assert data["price_courtesy"] is None
    assert data["total_amount"] == 80000.0
    assert data["items"][0]["courtesy_quantity"] is None
    assert data["items"][0]["regular_quantity"] is None
    assert data["items"][0]["discount_amount"] == 0.0
    assert data["courtesy_status"]["state"] == "used"
    assert data["courtesy_status"]["used_order_id"] == order.id
    assert data["courtesy_status"]["used_order_number"] == order.order_number


def test_an_estimate_drawn_before_the_switch_is_refused_at_create(client, db, w, operator_auth_headers):
    before = Order.query.count()

    refused = client.post(
        CREATE, json=_create_body(w.customer, w.product, expected_total=72000.0), headers=operator_auth_headers
    )

    assert refused.status_code == 409
    body = refused.get_json()
    assert body["error_code"] == "STATE_CHANGED"
    assert body["details"]["reason"] == "price_changed"
    assert body["details"]["quote"]["total_amount"] == 76000.0
    assert Order.query.count() == before
    db.session.expire_all()
    assert entry_of(db, w.customer.user).order_id is None

    retried = client.post(
        CREATE, json=_create_body(w.customer, w.product, expected_total=76000.0), headers=operator_auth_headers
    )
    assert retried.status_code == 201, retried.get_json()


def test_an_ineligible_client_is_quoted_and_charged_the_new_price(
    client, app, db, sample_product, admin_user, operator_user, operator_auth_headers
):
    w = courtesy_world(app, db, water(db, sample_product), admin_user, eligible=False)

    quote = _estimate(client, operator_auth_headers, w.customer.user.id, w.product).get_json()["data"]
    assert quote["total_amount"] == 80000.0
    assert quote["price_courtesy"] is None
    assert quote["courtesy_status"]["state"] == "none"

    created = client.post(
        CREATE, json=_create_body(w.customer, w.product, expected_total=80000.0), headers=operator_auth_headers
    )

    assert created.status_code == 201, created.get_json()
    assert _stored_line(db, created.get_json()["data"]["id"]) == (
        NEW_PRICE,
        None,
        None,
        Decimal("0.00"),
        NEW_TOTAL,
    )
    assert entry_of(db, w.customer.user) is None


def test_the_redrawn_quote_prices_on_the_method_the_order_charges(
    client, db, sample_product, operator_user, operator_auth_headers
):
    account, office = make_workplace(db)
    make_contract(db, account, sample_product, prepaid_units=Decimal("50.00"))
    # What a quote drawn with no rail yet showed: the catalogue price.
    stale = float(CATALOGUE * QUANTITY)
    body = {
        "client_id": account.id,
        "items": [{"product_id": sample_product.id, "quantity": QUANTITY}],
        "delivery_address_id": office.id,
        "delivery_notes": None,
        "expected_total": stale,
    }

    refused = client.post(CREATE, json=body, headers=operator_auth_headers)

    assert refused.status_code == 409
    redrawn = refused.get_json()["details"]["quote"]
    assert redrawn["total_amount"] == float(BANK * QUANTITY)
    assert redrawn["total_amount"] != stale

    accepted = client.post(CREATE, json={**body, "expected_total": redrawn["total_amount"]}, headers=operator_auth_headers)

    assert accepted.status_code == 201, accepted.get_json()
    assert accepted.get_json()["data"]["total_amount"] == float(BANK * QUANTITY)


def test_a_courtesy_used_between_the_preview_and_the_claim_is_not_charged(
    client, db, w, operator_auth_headers, monkeypatch
):
    """The coverage call previews the courtesy (`claim=False`); another order takes it before the claim runs. The
    claim prices freshly built lines, so it finds the entry used and charges the full new price."""
    real = courtesy_module.apply_courtesy
    calls = []

    def apply_courtesy(lines, *, user_id, at, is_subscription_order, claim):
        calls.append(claim)
        if claim:
            rival = live_order(db, w.customer.user, at, address=w.customer.address)
            rivalled = entry_of(db, w.customer.user)
            rivalled.order_id, rivalled.used_at = rival.id, at
            db.session.flush()
        return real(lines, user_id=user_id, at=at, is_subscription_order=is_subscription_order, claim=claim)

    monkeypatch.setattr(courtesy_module, "apply_courtesy", apply_courtesy)

    created = client.post(CREATE, json=_create_body(w.customer, w.product), headers=operator_auth_headers)

    assert created.status_code == 201, created.get_json()
    assert calls == [False, True]
    data = created.get_json()["data"]
    assert data["total_amount"] == 80000.0
    assert _stored_line(db, data["id"]) == (NEW_PRICE, None, None, Decimal("0.00"), NEW_TOTAL)
