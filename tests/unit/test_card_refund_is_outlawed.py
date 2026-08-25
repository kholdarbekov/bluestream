"""B4a — a card/Click payment is NEVER reversed, and there is no code path left
that could reverse one.

THE OWNER'S RULE (2026-08-24): "we never ever cancel card / click paid payments.
We can cancel the order itself, and in that case the payment will settle as
prepaid customer balance."

The rule is enforced AT THE METHOD, not at the callers. A caller-side carve-out
leaves the reversal one ``if`` away from firing; a rail gate inside
``process_refund`` converts the rule from *currently unbroken* to *unbreakable* —
and the gateway call it guarded is deleted outright, so there is nothing left for
the next caller to find.

PAYME IS OUT OF SCOPE AND UNAFFECTED BY CONSTRUCTION. Every Payme payment is
created and looked up as ``PaymentMethod.PAYME``, so a ``{CLICK, CARD}`` gate
cannot be reached by Payme's protocol-mandated ``CancelTransaction`` — a
merchant-agreement obligation the gateway initiates, which still gets exactly the
status flip and paid-projection sync its protocol response needs.
"""

from decimal import Decimal

import pytest

from business_app.models.payment import CashCollectionEvent, Payment
from business_app.services.click_payment_provider_service import ClickPaymentProviderService
from business_app.services.payment_service import PaymentService
from business_app.utils.exceptions import ValidationError
from shared.enums import OrderStatus, PaymentMethod, PaymentStatus


@pytest.fixture
def payment_service():
    return PaymentService()


def _complete(db, payment, method):
    payment.payment_method = method
    # ARCH-006 `ck_payments_cash_completed_requires_collector`: a COMPLETED cash
    # payment must name its collector, and the CHECK fires on the same UPDATE
    # that sets the status — so this has to be stamped first, not after.
    if method == PaymentMethod.CASH:
        payment.collected_by = payment.user_id
    payment.status = PaymentStatus.COMPLETED
    payment.amount_collected = payment.amount
    payment.outstanding_amount = Decimal("0.00")
    if payment.order is not None:
        payment.order.payment_method = method
        payment.order.status = OrderStatus.CONFIRMED
        payment.order.is_paid = True
    db.session.commit()
    return payment


@pytest.mark.unit
@pytest.mark.payment
class TestTheRailGate:
    @pytest.mark.parametrize("method", [PaymentMethod.CLICK, PaymentMethod.CARD])
    def test_a_fiscalized_rail_refund_is_refused(self, app, db, payment_service, sample_payment, method):
        payment = _complete(db, sample_payment, method)

        with app.app_context():
            with pytest.raises(ValidationError):
                payment_service.process_refund(payment.id, payment.amount, reason="anything")

        db.session.refresh(payment)
        assert payment.status is PaymentStatus.COMPLETED, "the money stays where the bank put it"
        assert payment.order.is_paid is True

    def test_payme_is_not_refused_and_still_gets_its_protocol_bookkeeping(
        self, app, db, payment_service, sample_payment
    ):
        """§6.1: Payme must be byte-for-byte unaffected. Same method, same
        signature, same status flip, same paid-projection sync — and NO credit
        event, because the money really does go back to the customer's card."""
        payment = _complete(db, sample_payment, PaymentMethod.PAYME)

        with app.app_context():
            success = payment_service.process_refund(payment.id, payment.amount, reason="Payme Cancel: 5")

        assert success is True
        db.session.refresh(payment)
        assert payment.status is PaymentStatus.CANCELLED
        assert payment.order.is_paid is False
        assert CashCollectionEvent.query.filter_by(customer_id=payment.user_id).count() == 0, (
            "Payme money is returned at the gateway; it must not ALSO become customer credit"
        )

    def test_cash_and_loyalty_rails_still_reach_the_bookkeeping_branch(self, app, db, payment_service, sample_payment):
        """§6.7: the ``else`` branch is shared by payme, cash and loyalty points.
        A blanket refusal would break all three."""
        payment = _complete(db, sample_payment, PaymentMethod.CASH)

        with app.app_context():
            success = payment_service.process_refund(payment.id, payment.amount, reason="cash back at the door")

        assert success is True
        db.session.refresh(payment)
        assert payment.status is PaymentStatus.CANCELLED


@pytest.mark.unit
@pytest.mark.payment
class TestTheOutboundRailsAreGone:
    def test_the_click_refund_client_no_longer_exists(self):
        """Its only production caller was ``process_refund``'s deleted branch.
        Leaving a live gateway-reversal method in the service for the next caller
        to find is exactly the failure the rule is written against."""
        assert not hasattr(ClickPaymentProviderService, "refund_payment")

    def test_duplicate_charge_reversal_is_deliberately_KEPT(self):
        """§6.6: ``reverse_by_click_payment_id`` is the duplicate-charge tool —
        the customer was charged twice for one order and one charge was never
        ours. That is a different problem from a refund and it stays."""
        assert hasattr(ClickPaymentProviderService, "reverse_by_click_payment_id")

        from business_app.tasks import payment_tasks

        assert hasattr(payment_tasks, "reverse_click_payment_task")

    def test_the_undispatched_refund_task_is_gone(self):
        from business_app.tasks import payment_tasks

        assert not hasattr(payment_tasks, "process_refund")

    def test_the_service_level_refund_request_helper_is_gone(self):
        assert not hasattr(PaymentService, "request_refund")

    def test_the_published_payment_method_no_longer_promises_refunds(self):
        """``supports_refunds`` is a typed field of ``GET /payments/methods`` that
        both admin_ui and the bot read. It published the opposite of the rule."""
        from shared.payment_methods import PAYMENT_METHOD_CATALOG

        click = next(m for m in PAYMENT_METHOD_CATALOG if m["method"] == "click")
        assert click["supports_refunds"] is False

    def test_the_admin_refund_route_is_deleted_not_merely_refusing(self, app):
        """§4: the rule BINDS ADMINS TOO. A refusing admin route would be the
        escape hatch the owner said to remove — still listed, still discoverable,
        one ``if`` from working. The admin's lawful lever is cancelling the ORDER.
        """
        endpoints = {rule.endpoint for rule in app.url_map.iter_rules()}
        assert "admin.refund_payment" not in endpoints
