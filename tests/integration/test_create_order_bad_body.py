"""POST /api/v1/orders/ answers 400, never 500, to a body it cannot parse.

Driven over HTTP as a verified customer. A body that is not a JSON object is the
client's mistake: it gets the invalid-request copy and nothing is logged at ERROR.
"""

import logging

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.order import Order
from business_app.utils.translations import get_translation
from tests.integration.tier_discount_factory import post_order, verify_phone

URL = "/api/v1/orders/"


@pytest.fixture
def headers(app, db, sample_user):
    verify_phone(db, sample_user)
    with app.app_context():
        token = create_access_token(identity=str(sample_user.id))
    return {"Authorization": f"Bearer {token}"}


def _invalid_request_message(app):
    with app.app_context():
        return get_translation("api.orders.error.invalid_request_data")


def _assert_plain_400(app, resp, caplog):
    assert resp.status_code == 400, resp.get_data(as_text=True)
    assert resp.get_json()["message"] == _invalid_request_message(app)
    assert Order.query.count() == 0
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_an_unparseable_body_is_a_400(app, db, headers, caplog):
    with caplog.at_level(logging.DEBUG):
        resp = app.test_client().post(URL, data="{not json", content_type="application/json", headers=headers)
    _assert_plain_400(app, resp, caplog)


def test_an_empty_body_is_a_400(app, db, headers, caplog):
    with caplog.at_level(logging.DEBUG):
        resp = app.test_client().post(URL, headers=headers)
    _assert_plain_400(app, resp, caplog)


def test_a_json_array_is_a_400(app, db, headers, caplog):
    with caplog.at_level(logging.DEBUG):
        resp = app.test_client().post(URL, json=[1, 2], headers=headers)
    _assert_plain_400(app, resp, caplog)


def test_a_json_scalar_is_a_400(app, db, headers, caplog):
    with caplog.at_level(logging.DEBUG):
        resp = app.test_client().post(URL, json="x", headers=headers)
    _assert_plain_400(app, resp, caplog)


def test_a_valid_body_still_creates_the_order(app, db, headers, sample_product, user_address):
    resp = post_order(
        app, headers, product_id=sample_product.id, address_id=user_address.id, payment_method="cash"
    )
    assert resp.status_code == 201, resp.get_json()
    assert Order.query.count() == 1
