"""Phase 4D — we no longer cancel abandoned payments from our side.

Owner ruling 2026-08-24: "If Click handles payments even after a long time then
there is no reason for us to cancel abandoned payments. Let it stay, customer
can pay whenever they can but until delivery."

That removes the mechanism that caused prod incident TG_000413_26: reconcile
declared payment 1204 dead at the 60-minute mark while the order was still live,
and we had no way to make that cancellation stick at Click — the checkout link
stayed payable, the customer used it 28 hours later, and the money had nowhere
to go. `create_payment_link` makes no Click API call at all, so there has never
been a gateway-side object we could void.

A gateway-reported cancel is also NOT terminal any more: it describes one failed
attempt, not the payability of the order. The customer can open the same link
again, and under the Phase 4A guard PREPARE only lets them while the payment is
still PENDING/PROCESSING — so writing a terminal status here would lock them out
of paying an order they still owe for.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from business_app import db
from business_app.models.payment import Payment
from shared.enums import MarkingCodeStatus, OrderStatus, PaymentMethod, PaymentStatus


def _aged_pending_click_payment(db, order, *, hours=5):
    payment = Payment(
        order_id=order.id,
        user_id=order.user_id,
        payment_method=PaymentMethod.CLICK,
        amount=order.total_amount,
        currency="UZS",
        status=PaymentStatus.PENDING,
        payment_id=f"pay-abandoned-{order.id}",
        created_at=datetime.now(timezone.utc) - timedelta(hours=hours),
        provider_data={"click": {"click_paydoc_id": "5231141285"}},
    )
    db.session.add(payment)
    db.session.commit()
    return payment


def _run_reconcile(monkeypatch, gateway_status):
    """Drive reconcile with ONLY the gateway seam mocked.

    🔴 THIS HELPER USED TO MONKEYPATCH ``PaymentService.check_payment_status``
    WHOLESALE, which meant ``update_payment_status`` — the method holding the
    entire cancel decision — never ran. Every cell below passed identically with
    the guard reverted, including the one whose name promises the customer is not
    locked out. Seamed at the Click provider instead, so the real decision runs.
    """
    from business_app.services.click_payment_provider_service import ClickPaymentProviderService
    from business_app.tasks.payment_tasks import reconcile_pending_payments

    data = {"provider_transaction_id": "999000222", "raw": {}}
    data.update(gateway_status)
    monkeypatch.setattr(
        ClickPaymentProviderService,
        "check_payment_status",
        lambda self, payment: dict(data),
    )
    return reconcile_pending_payments.run()


class TestAbandonedPaymentsAreLeftAlone:
    @pytest.mark.parametrize("order_status", [
        OrderStatus.PENDING,
        OrderStatus.CONFIRMED,
        OrderStatus.PREPARING,
        OrderStatus.OUT_FOR_DELIVERY,
    ])
    def test_payment_past_timeout_is_never_auto_cancelled(
        self, app, db, sample_order, monkeypatch, order_status
    ):
        """The incident's kill line. PENDING is included deliberately: that is the
        state order 1100 was in at 05:45 when reconcile destroyed its payment."""
        sample_order.status = order_status
        payment = _aged_pending_click_payment(db, sample_order)

        counts = _run_reconcile(monkeypatch, {"status": "not_found", "not_found": True})

        db.session.expire_all()
        assert Payment.query.get(payment.id).status == PaymentStatus.PENDING
        assert counts["cancelled"] == 0

    def test_gateway_reported_cancel_does_not_lock_the_customer_out(
        self, app, db, sample_order, monkeypatch
    ):
        """A cancelled ATTEMPT is not a dead order. Writing a terminal status
        would make the Phase 4A PREPARE guard refuse the customer's next try."""
        sample_order.status = OrderStatus.CONFIRMED
        payment = _aged_pending_click_payment(db, sample_order)

        _run_reconcile(monkeypatch, {"status": "cancelled", "not_found": False})

        db.session.expire_all()
        assert Payment.query.get(payment.id).status == PaymentStatus.PENDING

    def test_marking_codes_are_not_released_by_reconcile(
        self, app, db, sample_order, sample_product, monkeypatch
    ):
        """Releasing codes was the other half of the auto-cancel. Codes now stay
        with the order until it is delivered-and-settled or cancelled."""
        from business_app.models.product import ProductMarkingCode

        payment = _aged_pending_click_payment(db, sample_order)
        code = ProductMarkingCode(
            product_id=sample_product.id,
            code="MARK-KEEP\x1dVERIFY-KEEP",
            status=MarkingCodeStatus.RESERVED,
            order_id=sample_order.id,
        )
        db.session.add(code)
        db.session.commit()

        _run_reconcile(monkeypatch, {"status": "not_found", "not_found": True})

        db.session.expire_all()
        assert ProductMarkingCode.query.get(code.id).status == MarkingCodeStatus.RESERVED

    def test_affirmative_success_still_completes_the_payment(
        self, app, db, sample_order, monkeypatch
    ):
        """Removing the cancel must not break the reason reconcile exists."""
        sample_order.status = OrderStatus.CONFIRMED
        payment = _aged_pending_click_payment(db, sample_order)

        counts = _run_reconcile(monkeypatch, {"status": "completed", "not_found": False})

        assert counts["completed"] == 1


def test_the_timeout_autocancel_branch_is_gone_from_the_source():
    """Grep-pin: the branch and its config knob must not quietly return."""
    from pathlib import Path

    src = Path("business_app/tasks/payment_tasks.py").read_text()
    assert "Auto-cancelling payment" not in src, (
        "the timeout auto-cancel must be removed, not merely disabled"
    )
    assert "PAYMENT_TIMEOUT_MINUTES" not in src, (
        "the timeout knob must no longer drive any cancellation"
    )
