"""The customer's courtesy status and the manager exception over HTTP (spec 2026-11-16 §5.6, plan Task 29).

`GET/POST /api/v1/admin/customers/<id>/price-courtesy`: staff read the status, a manager or admin grants the old
price once per person. The grant button reads `allowed_actions.grant`, which is the rule the write itself runs.
"""

from datetime import timedelta

import pytest

from business_app.models.price_change import CourtesySource, PriceCourtesy
from business_app.utils.timezone_utils import get_utc_now
from tests.integration.contract_pricing_factory import make_contract, make_workplace
from tests.integration.price_courtesy_builders import (
    BASKET,
    courtesy_world,
    water,
)
from tests.integration.reorder_reminder_builders import claim_headers, make_customer

pytestmark = pytest.mark.integration

URL = "/api/v1/admin/customers/{}/price-courtesy"
ESTIMATE = "/api/v1/orders/cart/estimate"
REASON = "Cancelled after the switch"
NO_GRANT = {"allowed": False, "block_reason": None}


@pytest.fixture
def w(app, db, sample_product, admin_user):
    return courtesy_world(app, db, water(db, sample_product), admin_user)


def _data(response):
    return response.get_json()["data"]


def test_staff_read_the_courtesy_status(client, app, w, operator_user):
    response = client.get(URL.format(w.customer.user.id), headers=claim_headers(app, operator_user, "operator"))

    assert response.status_code == 200
    data = _data(response)
    assert data["courtesy_status"] == {
        "state": "available",
        "expires_on": w.change.courtesy_last_day.isoformat(),
        "caps": [{"product_id": w.product.id, "product_name": w.product.name, "cap": 2}],
        "used_order_id": None,
        "used_order_number": None,
    }
    assert data["allowed_actions"] == {"grant": NO_GRANT}

    refused = client.get(URL.format(w.customer.user.id), headers=w.customer.headers)
    assert refused.status_code == 403
    claimed = client.get(URL.format(w.customer.user.id), headers=claim_headers(app, w.customer.user, "customer"))
    assert claimed.status_code == 403


def test_a_customer_with_no_applied_change_reads_none(client, manager_claim_headers, app, db, sample_product, admin_user):
    w = courtesy_world(app, db, water(db, sample_product), admin_user, switched=False)

    response = client.get(URL.format(w.customer.user.id), headers=manager_claim_headers)

    assert response.status_code == 200
    assert _data(response)["courtesy_status"] == {
        "state": "none",
        "expires_on": None,
        "caps": [],
        "used_order_id": None,
        "used_order_number": None,
    }


def test_a_manager_grants_the_courtesy_to_a_customer_the_rule_left_out(
    client, app, db, sample_product, admin_user, manager_user, manager_claim_headers
):
    w = courtesy_world(app, db, water(db, sample_product), admin_user, eligible=False)
    assert PriceCourtesy.query.count() == 0

    response = client.post(URL.format(w.customer.user.id), json={"reason": REASON}, headers=manager_claim_headers)

    assert response.status_code == 201
    data = _data(response)
    assert data["courtesy_status"]["state"] == "available"
    assert data["allowed_actions"]["grant"]["allowed"] is False
    assert data["allowed_actions"]["grant"]["block_reason"]
    row = PriceCourtesy.query.one()
    assert (row.user_id, row.source, row.reason, row.granted_by_id, row.price_change_id) == (
        w.customer.user.id,
        CourtesySource.MANAGER,
        REASON,
        manager_user.id,
        w.change.id,
    )
    estimate = client.post(
        ESTIMATE,
        json={"items": [{"product_id": w.product.id, "quantity": BASKET}], "payment_method": "cash"},
        headers=w.customer.headers,
    )
    assert estimate.status_code == 200
    assert _data(estimate)["items"][0]["courtesy_quantity"] == 2


def test_an_operator_cannot_grant(client, app, db, w, operator_user):
    target = make_customer(db)

    response = client.post(
        URL.format(target.id), json={"reason": REASON}, headers=claim_headers(app, operator_user, "operator")
    )

    assert response.status_code == 403
    assert PriceCourtesy.query.filter_by(user_id=target.id).count() == 0


def test_the_courtesy_is_granted_once_per_person(client, app, db, sample_product, admin_user, manager_claim_headers):
    w = courtesy_world(app, db, water(db, sample_product), admin_user, eligible=False)
    url = URL.format(w.customer.user.id)

    first = client.post(url, json={"reason": REASON}, headers=manager_claim_headers)
    second = client.post(url, json={"reason": REASON}, headers=manager_claim_headers)

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.get_json()["error_code"] == "PRICE_COURTESY_ALREADY_EXISTS"
    assert PriceCourtesy.query.count() == 1


def test_a_grant_needs_a_reason(client, app, db, sample_product, admin_user, manager_claim_headers):
    w = courtesy_world(app, db, water(db, sample_product), admin_user, eligible=False)
    url = URL.format(w.customer.user.id)

    missing = client.post(url, json={}, headers=manager_claim_headers)
    blank = client.post(url, json={"reason": "   "}, headers=manager_claim_headers)

    assert missing.status_code == 400
    assert blank.status_code == 400
    assert blank.get_json()["error_code"] == "PRICE_COURTESY_REASON_REQUIRED"
    assert PriceCourtesy.query.count() == 0


def test_a_manager_sees_the_grant_action_for_an_eligible_stranger(client, app, db, w, manager_claim_headers):
    stranger = make_customer(db)

    response = client.get(URL.format(stranger.id), headers=manager_claim_headers)

    assert response.status_code == 200
    assert _data(response)["courtesy_status"]["state"] == "none"
    assert _data(response)["allowed_actions"] == {"grant": {"allowed": True, "block_reason": None}}


def test_an_admin_sees_the_grant_action_too(client, w, db, admin_claim_headers):
    stranger = make_customer(db)

    response = client.get(URL.format(stranger.id), headers=admin_claim_headers)

    assert _data(response)["allowed_actions"] == {"grant": {"allowed": True, "block_reason": None}}


def test_a_manager_sees_no_grant_while_no_window_is_open(
    client, app, db, sample_product, admin_user, manager_claim_headers
):
    w = courtesy_world(app, db, water(db, sample_product), admin_user, switched=False)

    response = client.get(URL.format(w.customer.user.id), headers=manager_claim_headers)

    assert _data(response)["allowed_actions"] == {"grant": NO_GRANT}


def test_a_contract_account_inside_an_open_window_keeps_its_sentence(
    client, app, db, w, manager_claim_headers
):
    account, _office = make_workplace(db)
    contract, _row = make_contract(db, account, w.product)
    contract.start_date = w.change.applied_at - timedelta(days=30)
    db.session.commit()

    response = client.get(URL.format(account.id), headers=manager_claim_headers)

    assert response.status_code == 200
    assert _data(response)["allowed_actions"] == {
        "grant": {"allowed": False, "block_reason": "Accounts under a contract are outside the courtesy."}
    }


def test_an_unknown_customer_is_404_for_staff_and_managers(
    client, app, db, w, operator_user, manager_claim_headers
):
    url = URL.format(999_999)

    responses = [
        client.get(url, headers=claim_headers(app, operator_user, "operator")),
        client.get(url, headers=manager_claim_headers),
        client.post(url, json={"reason": REASON}, headers=manager_claim_headers),
    ]

    for response in responses:
        assert response.status_code == 404
        assert response.get_json()["error_code"] == "PRICE_COURTESY_USER_NOT_FOUND"
    assert PriceCourtesy.query.filter_by(user_id=999_999).count() == 0
