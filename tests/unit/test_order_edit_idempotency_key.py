"""_recompute_totals keeps payment.idempotency_key in sync with the edited amount."""

from decimal import Decimal

import pytest

from business_app.models.payment import Payment
from business_app.services.order_edit_service import OrderEditPlan, OrderEditService

from tests.integration.test_payment_matrix import _seed_click_payment


@pytest.mark.unit
def test_recompute_totals_rederives_payment_idempotency_key(app, db, sample_order):
    """After an amount-changing edit, the payment's idempotency key must match the
    new amount — a stale key would collide/miss on the next create_payment call."""
    payment = _seed_click_payment(db, sample_order)
    stale_key = Payment.compute_idempotency_key(
        order_id=sample_order.id,
        user_id=payment.user_id,
        amount=payment.amount,
        payment_method=payment.payment_method,
    )
    payment.idempotency_key = stale_key
    original_amount = payment.amount
    db.session.commit()

    # Act: drive _recompute_totals with a changed order total. sample_order is
    # unpaid (is_paid defaults to False) and has no order_items, so bumping
    # delivery_fee is the simplest way to move total_amount without needing
    # item rows.
    order = sample_order
    order.delivery_fee = Decimal("9000.00")
    plan = OrderEditPlan(
        order_id=order.id,
        is_post_delivery=False,
        items_before=[],
        items_after=[],
        item_changes=[],
        totals_before={},
        totals_after={},
    )
    svc = OrderEditService()
    svc._recompute_totals(order, plan, actor_user_id=order.user_id)
    db.session.commit()

    db.session.expire_all()
    payment = Payment.query.filter_by(order_id=sample_order.id).one()
    assert payment.amount != original_amount  # sanity: the edit changed the amount
    expected = Payment.compute_idempotency_key(
        order_id=sample_order.id,
        user_id=payment.user_id,
        amount=payment.amount,  # the NEW amount written by _recompute_totals
        payment_method=payment.payment_method,
    )
    assert payment.idempotency_key == expected
    assert payment.idempotency_key != stale_key
