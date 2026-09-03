"""POST /api/v1/payments/create — the tier discount follows the rail.

A tier discount must NEVER reach a Click fiscal receipt:
``build_click_fiscalization_payload`` asserts
``received_card == to_tiyin(order.total_amount)`` while filling per-item
``Discount`` from ``loyalty_discount`` ALONE, so a tier-discounted total on a
fiscalized rail makes Σ(Price − Discount) ≠ received_card — a tax-committee
reconciliation failure, not a display bug.

Both directions are exercised through the real HTTP route the bot's Pay button
and the web checkout use.
"""

from decimal import Decimal

import pytest
from flask_jwt_extended import create_access_token

from business_app import db as _db
from business_app.models.order import Order
from business_app.models.payment import Payment
from business_app.services.loyalty_service import LoyaltyService
from business_app.utils.order_totals import compute_order_total
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


def test_moving_off_cod_revokes_the_discount_and_reprices_the_payment(
    app, db, customer, sample_product, user_address, program
):
    seed_tier(db, program, name="Base", rate=TIER_RATE)
    order = _create(app, customer, sample_product, user_address, "cash")
    granted = Decimal(str(order.tier_discount))
    assert granted > Decimal("0.00")
    undiscounted = Decimal(str(order.subtotal)) + Decimal(str(order.delivery_fee or 0))
    stale_key = Payment.query.filter_by(order_id=order.id).one().idempotency_key

    resp = app.test_client().post(
        "/api/v1/payments/create",
        json={"order_id": order.id, "payment_method": "click"},
        headers=_headers(app, customer.id),
    )

    assert resp.status_code in (200, 201), resp.get_json()
    order = _reload(order.id)
    payment = Payment.query.filter_by(order_id=order.id).one()

    assert Decimal(str(order.tier_discount)) == Decimal("0.00")
    assert Decimal(str(order.total_amount)) == undiscounted
    assert Decimal(str(payment.amount)) == Decimal(str(order.total_amount))
    assert payment.idempotency_key != stale_key
    assert payment.idempotency_key == Payment.compute_idempotency_key(
        order_id=order.id,
        user_id=order.user_id,
        amount=payment.amount,
        payment_method=PaymentMethod.CLICK,
    )


def test_moving_onto_cod_grants_the_discount_and_reprices_the_payment(
    app, db, customer, sample_product, user_address, program
):
    seed_tier(db, program, name="Base", rate=TIER_RATE)
    order = _create(app, customer, sample_product, user_address, "click")
    assert Decimal(str(order.tier_discount)) == Decimal("0.00")
    stale_key = Payment.query.filter_by(order_id=order.id).one().idempotency_key

    resp = app.test_client().post(
        "/api/v1/payments/create",
        json={"order_id": order.id, "payment_method": "cash"},
        headers=_headers(app, customer.id),
    )

    assert resp.status_code in (200, 201), resp.get_json()
    order = _reload(order.id)
    payment = Payment.query.filter_by(order_id=order.id).one()

    expected = LoyaltyService().quote_tier_discount(customer, order.subtotal, PaymentMethod.CASH).amount
    assert expected > Decimal("0.00")
    assert Decimal(str(order.tier_discount)) == expected
    assert Decimal(str(order.total_amount)) == compute_order_total(
        subtotal=Decimal(str(order.subtotal)),
        discount_amount=Decimal(str(order.discount_amount or 0)),
        delivery_fee=Decimal(str(order.delivery_fee or 0)),
        loyalty_discount=Decimal(str(order.loyalty_discount or 0)),
        tier_discount=Decimal(str(order.tier_discount)),
    )
    assert Decimal(str(payment.amount)) == Decimal(str(order.total_amount))
    assert Decimal(str(payment.outstanding_amount)) == Decimal(str(order.total_amount))
    assert payment.idempotency_key != stale_key


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
