"""POST /api/v1/payments/create — a customer cannot move an order between COD and Click.

A tier discount must NEVER reach a Click fiscal receipt:
``build_click_fiscalization_payload`` asserts
``received_card == to_tiyin(order.total_amount)`` while filling per-item
``Discount`` from ``loyalty_discount`` ALONE, so a tier-discounted total on a
fiscalized rail makes Σ(Price − Discount) ≠ received_card — a tax-committee
reconciliation failure, not a display bug.

The customer used to be able to flip the rail here, and the tier followed it.
Neither direction is a customer move any more (spec 2026-10-27 §4.3, D4, D5):
both are refused, and the order keeps its tier and its total. Staff and the
door still move rails (`test_tier_discount_self_service_only.py`).
"""

from decimal import Decimal

import pytest
from flask_jwt_extended import create_access_token

from business_app import db as _db
from business_app.models.order import Order
from shared.enums import PaymentMethod
from tests.integration.tier_discount_factory import (
    post_order,
    seed_program,
    seed_tier,
    verify_phone,
)

TIER_RATE = Decimal("7")


def _headers(app, user_id):
    with app.app_context():
        token = create_access_token(identity=str(user_id))
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _reload(order_id):
    _db.session.expire_all()
    return Order.query.get(order_id)


@pytest.fixture
def program(db):
    return seed_program(db)


@pytest.fixture
def customer(db, sample_user):
    verify_phone(db, sample_user)
    return sample_user


def _create(app, customer, product, address, method):
    resp = post_order(
        app,
        _headers(app, customer.id),
        product_id=product.id,
        address_id=address.id,
        payment_method=method,
    )
    assert resp.status_code == 201, resp.get_json()
    return _reload(resp.get_json()["data"]["order"]["id"])


@pytest.mark.parametrize("start, requested", [("cash", "click"), ("click", "cash")])
def test_a_customer_cannot_move_an_order_between_cod_and_click(
    app, db, customer, sample_product, user_address, program, start, requested
):
    seed_tier(db, program, name="Base", rate=TIER_RATE)
    order = _create(app, customer, sample_product, user_address, start)
    before = (Decimal(str(order.tier_discount)), Decimal(str(order.total_amount)))

    resp = app.test_client().post(
        "/api/v1/payments/create",
        json={"order_id": order.id, "payment_method": requested},
        headers=_headers(app, customer.id),
    )

    assert resp.status_code == 400, resp.get_json()
    assert resp.get_json()["data"]["error_code"] == "RAIL_MOVE_NOT_ALLOWED"
    order = _reload(order.id)
    assert order.payment_method == PaymentMethod(start)
    assert (Decimal(str(order.tier_discount)), Decimal(str(order.total_amount))) == before


def test_a_repeat_request_on_the_same_rail_changes_nothing(
    app, db, customer, sample_product, user_address, program
):
    """`rail_moves` is False here, so the re-price must not fire at all — a
    second Pay tap on an unchanged rail may not re-quote the order."""
    seed_tier(db, program, name="Base", rate=TIER_RATE)
    order = _create(app, customer, sample_product, user_address, "cash")
    before_discount = Decimal(str(order.tier_discount))
    before_total = Decimal(str(order.total_amount))

    resp = app.test_client().post(
        "/api/v1/payments/create",
        json={"order_id": order.id, "payment_method": "cash"},
        headers=_headers(app, customer.id),
    )

    assert resp.status_code in (200, 201), resp.get_json()
    order = _reload(order.id)
    assert Decimal(str(order.tier_discount)) == before_discount
    assert Decimal(str(order.total_amount)) == before_total
