"""reverse_click_payment_task: reverses the INCOMING duplicate charge, never the winner."""

import pytest

from business_app import db
from business_app.models.payment import PaymentTransaction
from business_app.utils.exceptions import PaymentError
from shared.enums import PaymentStatus

from tests.integration.test_payment_matrix import _seed_click_payment


def _seed_duplicate_txn(payment, click_trans_id="930002"):
    txn = PaymentTransaction(
        payment_id=payment.id,
        transaction_type="click_duplicate_charge",
        amount=payment.amount,
        status="pending_reversal",
        provider_transaction_id=click_trans_id,
        provider_reference=payment.payment_id,
        provider_response={},
        success=False,
    )
    db.session.add(txn)
    db.session.commit()
    return txn


@pytest.mark.unit
def test_reversal_success_marks_transaction_reversed(db, sample_order, monkeypatch):
    payment = _seed_click_payment(db, sample_order)
    payment.status = PaymentStatus.COMPLETED
    db.session.commit()
    txn = _seed_duplicate_txn(payment)

    calls = []
    from business_app.services.click_payment_provider_service import ClickPaymentProviderService

    def fake_reverse(self, click_payment_id):
        calls.append(click_payment_id)
        return {"error_code": 0, "payment_id": click_payment_id}

    monkeypatch.setattr(ClickPaymentProviderService, "reverse_by_click_payment_id", fake_reverse)

    from business_app.tasks.payment_tasks import reverse_click_payment_task

    result = reverse_click_payment_task.run(payment.id, "888002", "930002")
    assert result == {"status": "reversed"}
    assert calls == [888002]
    db.session.expire_all()
    assert PaymentTransaction.query.get(txn.id).status == "reversed"


@pytest.mark.unit
def test_reversal_rejection_marks_transaction_rejected(db, sample_order, monkeypatch):
    payment = _seed_click_payment(db, sample_order)
    payment.status = PaymentStatus.COMPLETED
    db.session.commit()
    txn = _seed_duplicate_txn(payment, click_trans_id="930003")

    from business_app.services.click_payment_provider_service import ClickPaymentProviderService

    def fake_reverse(self, click_payment_id):
        raise PaymentError("Click merchant API error for payment_reversal: error_code=-5017")

    monkeypatch.setattr(ClickPaymentProviderService, "reverse_by_click_payment_id", fake_reverse)

    from business_app.tasks.payment_tasks import reverse_click_payment_task

    result = reverse_click_payment_task.run(payment.id, "888003", "930003")
    assert result == {"status": "rejected"}
    db.session.expire_all()
    assert PaymentTransaction.query.get(txn.id).status == "reversal_rejected"
