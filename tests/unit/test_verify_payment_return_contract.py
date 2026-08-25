"""Pin `PaymentService.verify_payment`'s return contract (B2 secondary item).

B1 round 3 root-caused the Click lockout to this method: `success` means only
"the payment is COMPLETED right now", so a healthy PENDING payment comes back as
`{"success": False, "error": "Verification failed"}`. The single caller
(`process_payment_verification`) is now gated on `order_is_resolved`, which fully
contains it — but a SECOND caller that reads the boolean naively would
reintroduce the same permanent lockout.

The shape was NOT changed. It cannot be changed safely: the existing caller
writes `error` into `payment.failure_reason` and into the `PaymentTransaction`
audit row, so any new string persists differently; and no additive field makes a
naive `result["success"]` read safe, because `success` is the field a new caller
naturally reaches for. Flipping `success` to True for a pending payment would
make the caller mark it COMPLETED — catastrophic.

So this module pins the contract instead, in both directions:

* what `verify_payment` returns for a healthy PENDING payment (including that
  `status` — NOT `success` — is the field that discriminates), and
* that the existing caller's persisted outcome is unchanged.

If someone changes the shape, these fail and force them to look at every caller.
"""

from decimal import Decimal

import pytest

from business_app.models.order import Order
from business_app.models.payment import Payment
from business_app.services.click_payment_provider_service import ClickPaymentProviderService
from business_app.services.payment_service import PaymentService
from shared.enums import OrderStatus, PaymentMethod, PaymentStatus


def _gateway_says(monkeypatch, status: str):
    """Mock ONLY the outermost gateway seam, as B1's tests do.

    `PaymentService.check_payment_status` / `update_payment_status` stay REAL —
    mocking them is the blind spot that let the original defect ship.
    """
    monkeypatch.setattr(
        ClickPaymentProviderService,
        "check_payment_status",
        lambda self, payment: {"status": status, "error_note": "", "raw": {}},
    )


@pytest.fixture
def live_order_with_pending_click_payment(db, sample_user):
    order = Order(
        user_id=sample_user.id,
        order_number="B2-VERIFY-CONTRACT",
        status=OrderStatus.CONFIRMED,
        payment_method=PaymentMethod.CLICK,
        total_amount=Decimal("54000.00"),
        subtotal=Decimal("54000.00"),
        is_paid=False,
    )
    db.session.add(order)
    db.session.commit()

    payment = Payment(
        order_id=order.id,
        user_id=sample_user.id,
        payment_method=PaymentMethod.CLICK,
        amount=order.total_amount,
        currency="UZS",
        status=PaymentStatus.PENDING,
        payment_id="PAY_B2_VERIFY_CONTRACT",
        provider_data={"click": {"click_paydoc_id": "20240101000011"}},
    )
    db.session.add(payment)
    db.session.commit()
    return order, payment


@pytest.mark.unit
@pytest.mark.payment
class TestVerifyPaymentReturnContract:
    def test_a_healthy_pending_payment_reports_success_false(
        self, db, live_order_with_pending_click_payment, monkeypatch
    ):
        """The trap itself, stated out loud: nothing failed, yet success is False."""
        _order, payment = live_order_with_pending_click_payment
        _gateway_says(monkeypatch, "pending")

        result = PaymentService().verify_payment(payment.id)

        assert result["success"] is False, (
            "unchanged on purpose - the caller writes result['error'] to the DB, "
            "so the shape cannot move without moving persisted data"
        )
        assert result["error"] == "Verification failed"

    def test_status_not_success_is_the_field_that_discriminates(
        self, db, live_order_with_pending_click_payment, monkeypatch
    ):
        """A second caller must branch on `status`, which already tells the truth."""
        _order, payment = live_order_with_pending_click_payment
        _gateway_says(monkeypatch, "pending")

        result = PaymentService().verify_payment(payment.id)

        assert result["status"] == PaymentStatus.PENDING.value, (
            "'pending' and 'failed' must remain distinguishable in the payload, "
            "even though both collapse to success=False"
        )

    def test_a_genuine_gateway_failure_is_distinguishable_from_pending(
        self, db, live_order_with_pending_click_payment, monkeypatch
    ):
        """Same `success`, different `status` — which is the whole point."""
        _order, payment = live_order_with_pending_click_payment
        payment.status = PaymentStatus.FAILED
        db.session.commit()
        _gateway_says(monkeypatch, "failed")

        result = PaymentService().verify_payment(payment.id)

        assert result["success"] is False
        assert result["status"] != PaymentStatus.PENDING.value

    def test_verify_payment_never_writes_a_terminal_status_itself(
        self, db, live_order_with_pending_click_payment, monkeypatch
    ):
        """Reading the verdict must not be a way to END a payment.

        Ending a payment is the ORDER's job (`order_is_resolved`). If
        `verify_payment` ever started persisting a terminal status, that would be
        yet another expression of payment finality.
        """
        _order, payment = live_order_with_pending_click_payment
        _gateway_says(monkeypatch, "pending")

        PaymentService().verify_payment(payment.id)

        db.session.expire_all()
        assert Payment.query.get(payment.id).status == PaymentStatus.PENDING


@pytest.mark.unit
@pytest.mark.payment
class TestExistingCallerBehaviourIsUnchanged:
    """The B2 constraint: no behaviour change at `process_payment_verification`."""

    def test_a_live_order_keeps_its_pending_payment_untouched(
        self, db, live_order_with_pending_click_payment, monkeypatch
    ):
        from business_app.tasks.payment_tasks import process_payment_verification

        _order, payment = live_order_with_pending_click_payment
        _gateway_says(monkeypatch, "pending")

        process_payment_verification.run(payment.id)

        db.session.expire_all()
        payment = Payment.query.get(payment.id)
        assert payment.status == PaymentStatus.PENDING
        assert payment.failure_reason is None

    def test_a_resolved_order_still_records_the_generic_failure_reason(
        self, db, live_order_with_pending_click_payment, monkeypatch
    ):
        """The exact string that reaches the DB, pinned.

        This is why `error` cannot simply be reworded for the pending case: it
        lands in `payment.failure_reason` and in the audit row.
        """
        from business_app.tasks.payment_tasks import process_payment_verification

        order, payment = live_order_with_pending_click_payment
        order.status = OrderStatus.CANCELLED
        db.session.commit()
        _gateway_says(monkeypatch, "pending")

        process_payment_verification.run(payment.id)

        db.session.expire_all()
        payment = Payment.query.get(payment.id)
        assert payment.status == PaymentStatus.FAILED
        assert payment.failure_reason == "Verification failed"
