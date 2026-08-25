"""reconcile_pending_payments: it completes, and it never cancels.

SUPERSEDES the 2026-07-08 "positive-evidence" contract, which said:
  - gateway-affirmative cancelled/failed => cancel (any age)
  - ``not_found`` past timeout           => cancel
  - unknown/ambiguous past timeout       => leave PENDING + one-shot audit
  - every reconcile-cancel notifies via ``payment_autocancel_retry``

That contract was retired on 2026-08-24. Two things killed it:

1. It was unenforceable. ``create_payment_link`` makes NO Click API call — the
   checkout URL is a plain ``urlencode`` — so there has never been a gateway-side
   object our cancellation could void. We declared payments dead while leaving a
   fully payable link in the customer's hands. Prod TG_000413_26: payment 1204
   cancelled at the 60-minute mark, paid 28 hours later, 54 000 with nowhere to go.
2. It was running on manufactured evidence. ``not_found`` came from
   ``check_payment_status_by_mti``, which collapsed every non-zero Click error code
   into "absent"; Click returned ``-12`` on 279 of 279 calls in the surrounding 30
   days. (Fixed separately — see tests/unit/test_click_status_by_mti.py.)

The new contract: a payment stays PENDING until the ORDER resolves. Cancellation
now happens only where there is a real-world event to hang it on — cash at the
door, or the order itself being cancelled.

The customer-notification half went with it: there is no auto-cancel to warn
about any more. (The ``payment_autocancel_retry`` template was never seeded in
prod anyway, so that warning never reached a single customer.)
"""

from datetime import datetime, timedelta, timezone

import pytest

from business_app import db
from business_app.models.payment import Payment
from shared.enums import OrderStatus, PaymentStatus

from tests.integration.test_payment_matrix import _seed_click_payment


def _age_payment(payment, minutes):
    payment.created_at = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    db.session.commit()


@pytest.fixture
def notified(monkeypatch):
    calls = []
    from business_app.services.notification_service import NotificationService

    monkeypatch.setattr(
        NotificationService,
        "send_notification",
        lambda self, user_id, key, template_data=None, **kw: calls.append((user_id, key, template_data)),
    )
    return calls


def _run_reconcile_with_status(monkeypatch, status_payload):
    """Drive reconcile with the gateway seam mocked — and NOTHING ELSE.

    🔴 THIS HELPER USED TO MONKEYPATCH ``PaymentService.check_payment_status``
    WHOLESALE. That is the masking pattern this very file exists to guard
    against: ``update_payment_status`` holds the ENTIRE cancel decision, so a
    wholesale patch meant the ``{"status": "cancelled"}`` cell below passed
    identically with the guard reverted. It promised coverage of the rule and
    delivered none.

    Now seamed at the outermost boundary — the Click provider's status call — so
    the real ``update_payment_status`` runs and the assertions land on committed
    DB state. ``provider_transaction_id`` is defaulted because Click always
    returns one on success and ``update_payment_status`` (correctly) refuses to
    promote a "completed" without it.
    """
    from business_app.services.click_payment_provider_service import ClickPaymentProviderService

    data = {"provider_transaction_id": "999000111", "raw": {}}
    data.update(status_payload)
    monkeypatch.setattr(
        ClickPaymentProviderService,
        "check_payment_status",
        lambda self, payment: dict(data),
    )
    from business_app.tasks.payment_tasks import reconcile_pending_payments

    return reconcile_pending_payments.run()


@pytest.mark.unit
@pytest.mark.parametrize("gateway_status", [
    {"status": "not_found", "not_found": True},
    {"status": "unknown", "not_found": False, "ambiguous": True},
    {"status": "cancelled", "not_found": False},
    {"status": "failed", "not_found": False},
    {"status": "pending", "not_found": False},
])
def test_no_gateway_answer_ever_cancels(db, sample_order, monkeypatch, notified, gateway_status):
    payment = _seed_click_payment(db, sample_order)
    _age_payment(payment, minutes=600)

    counts = _run_reconcile_with_status(monkeypatch, gateway_status)

    db.session.expire_all()
    assert payment.status == PaymentStatus.PENDING, (
        f"gateway said {gateway_status['status']} — that is still not licence to cancel"
    )
    assert counts["cancelled"] == 0
    assert counts["failed"] == 0
    assert notified == [], "there is no auto-cancel left to notify anyone about"


@pytest.mark.unit
def test_affirmative_success_completes_the_payment(db, sample_order, user_address, monkeypatch, notified):
    """The reason the task still exists.

    Needs ``user_address``: now that the seam is at the gateway rather than
    wrapped around ``update_payment_status``, the REAL ``_handle_successful_payment``
    runs and confirms the order — which requires a delivery address. That is the
    point of the re-seam: the success path is genuinely exercised, not simulated.
    """
    sample_order.delivery_address_id = user_address.id
    db.session.commit()
    payment = _seed_click_payment(db, sample_order)
    _age_payment(payment, minutes=600)

    counts = _run_reconcile_with_status(monkeypatch, {"status": "completed", "not_found": False})

    db.session.expire_all()
    assert counts["completed"] == 1
    assert Payment.query.get(payment.id).status == PaymentStatus.COMPLETED


@pytest.mark.unit
def test_transport_error_leaves_pending_not_cancelled(db, sample_order, monkeypatch, notified):
    """Unchanged from the old contract, and now trivially true."""
    from business_app.utils.exceptions import PaymentError

    payment = _seed_click_payment(db, sample_order)
    _age_payment(payment, minutes=600)

    def boom(self, payment):
        raise PaymentError("Click merchant API HTTP error (GET ...): 502")

    # Seamed at the gateway too, so the REAL transport-error handling inside
    # update_payment_status is what gets exercised.
    from business_app.services.click_payment_provider_service import ClickPaymentProviderService

    monkeypatch.setattr(ClickPaymentProviderService, "check_payment_status", boom)
    from business_app.tasks.payment_tasks import reconcile_pending_payments

    counts = reconcile_pending_payments.run()

    db.session.expire_all()
    assert payment.status == PaymentStatus.PENDING
    assert counts["cancelled"] == 0


@pytest.mark.unit
def test_marking_codes_survive_reconcile(db, sample_order, sample_product, monkeypatch, notified):
    """Releasing the codes was the half of the auto-cancel that let ANOTHER order
    consume them — which is how order 1100's trio ended up on TG_000414_26's
    tax receipt, 34 minutes after we cancelled its payment."""
    from business_app.models.product import ProductMarkingCode
    from shared.enums import MarkingCodeStatus

    payment = _seed_click_payment(db, sample_order)
    _age_payment(payment, minutes=600)
    code = ProductMarkingCode(
        product_id=sample_product.id,
        code="MARK-SURVIVE\x1dVERIFY-SURVIVE",
        status=MarkingCodeStatus.RESERVED,
        order_id=sample_order.id,
    )
    db.session.add(code)
    db.session.commit()

    _run_reconcile_with_status(monkeypatch, {"status": "not_found", "not_found": True})

    db.session.expire_all()
    assert ProductMarkingCode.query.get(code.id).status == MarkingCodeStatus.RESERVED
    assert ProductMarkingCode.query.get(code.id).order_id == sample_order.id
