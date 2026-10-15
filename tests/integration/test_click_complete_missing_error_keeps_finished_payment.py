"""A Click COMPLETE with no `error` field never rewrites a FINISHED payment.

`handle_complete`'s missing-error-code branch ran before the guard that protects
finished payments (COMPLETED / CANCELLED / FAILED). Its only check,
`_may_end_payment`, asks whether the ORDER has resolved, and a paid order has.
So a correctly signed but malformed callback (`error` is not part of Click's
signature), for example a replay of a transaction that already completed, set a
genuinely paid payment to CANCELLED and freed its reserved marking codes.

Every other non-success branch already sits after that guard and answers -4 / -9
without touching the payment. This pins the missing-error branch to the same rule
for every finished status, through the real signed webhook.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from business_app import db as _db
from business_app.models.payment import Payment, PaymentTransaction
from shared.enums import OrderStatus, PaymentMethod, PaymentStatus
from tests.integration.fake_gateways import TEST_CLICK_SHOP_SECRET_KEY, make_click_webhook_form

pytestmark = pytest.mark.integration

WEBHOOK_URL = "/api/v1/payments/webhook/click"


def _finished_click_payment(order, status):
    order.payment_method = PaymentMethod.CLICK
    order.status = OrderStatus.CONFIRMED
    paid = status == PaymentStatus.COMPLETED
    order.is_paid = paid
    payment = Payment(
        order_id=order.id,
        user_id=order.user_id,
        payment_method=PaymentMethod.CLICK,
        status=status,
        amount=order.total_amount,
        amount_collected=Decimal("0.00"),
        outstanding_amount=Decimal("0.00") if paid else order.total_amount,
        currency="UZS",
        payment_id=f"click_missing_error_{order.id}_{status.value}",
        paid_at=datetime.now(timezone.utc) if paid else None,
        provider_data={"click": {"click_trans_id": "950001", "click_paydoc_id": "5231150001"}},
    )
    _db.session.add(payment)
    _db.session.commit()
    return payment


def _complete_without_error(client, order, payment):
    form = make_click_webhook_form(
        action="1",
        click_trans_id="950001",
        merchant_trans_id=order.order_number,
        amount=str(int(order.total_amount)),
        secret_key=TEST_CLICK_SHOP_SECRET_KEY,
        merchant_prepare_id=str(payment.id),
        click_paydoc_id="5231150001",
    )
    form.pop("error")  # not part of Click's signature, so the form still verifies
    return client.post(WEBHOOK_URL, data=form, content_type="application/x-www-form-urlencoded")


@pytest.mark.parametrize(
    "status", [PaymentStatus.COMPLETED, PaymentStatus.FAILED, PaymentStatus.CANCELLED], ids=lambda s: s.value
)
def test_a_complete_without_an_error_code_leaves_a_finished_payment_as_it_was(
    matrix_app, matrix_client, db, order_with_address, monkeypatch, status
):
    from business_app.services.payment_fiscalization_service import PaymentFiscalizationService

    released = []
    monkeypatch.setattr(
        PaymentFiscalizationService,
        "release_reserved_marking_codes",
        lambda self, payment, **kwargs: released.append(payment.id),
    )
    order = order_with_address
    payment = _finished_click_payment(order, status)

    resp = _complete_without_error(matrix_client, order, payment)

    assert resp.get_json()["error"] == -8, resp.get_json()
    _db.session.expire_all()
    payment = Payment.query.get(payment.id)
    assert payment.status == status
    assert payment.order.is_paid is (status == PaymentStatus.COMPLETED)
    assert released == [], "a finished payment's marking codes must not be released by a malformed callback"
    # The malformed callback is still recorded: the audit trail is not part of the gate.
    assert PaymentTransaction.query.filter_by(
        payment_id=payment.id, transaction_type="click_complete_invalid_payload"
    ).count() == 1
