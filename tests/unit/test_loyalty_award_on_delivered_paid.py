"""Loyalty purchase accrual: AquaCoins awarded only when delivered AND fully paid.

Regression tests for the bug where orders that merely reached CONFIRMED (not yet
paid, not delivered) received AquaCoins. Business rule (owner-confirmed
2026-06-20): award purchase AquaCoins only when ``order.status == DELIVERED`` AND
``order.is_paid`` is True, exactly once (idempotent across the delivery edge and
the payment edge).
"""

from unittest.mock import patch

import pytest

from business_app.models.loyalty import LoyaltyTransaction
from business_app.services.loyalty_service import LoyaltyService
from business_app.services.order_service import OrderService
from business_app.utils.constants import LoyaltyTransactionType
from shared.enums import OrderStatus, PaymentMethod


@pytest.fixture
def order_service(app, mock_inventory_service):
    with app.app_context():
        return OrderService(inventory_service=mock_inventory_service)


def _guards(order_service, *, already_awarded=False):
    """Patch the compute+write helper (to observe whether an award is attempted)
    and the idempotency probe (to control the already-awarded dimension)."""
    return (
        patch.object(order_service, "_process_loyalty_points_for_order"),
        patch(
            "business_app.services.loyalty_service.LoyaltyService.has_purchase_award",
            return_value=already_awarded,
        ),
    )


@pytest.mark.unit
@pytest.mark.order
class TestMaybeAwardPurchasePoints:
    """maybe_award_purchase_points awards only on (DELIVERED and is_paid), once."""

    def test_no_award_when_confirmed_and_unpaid(self, order_service, sample_order):
        """The reported bug: a merely-CONFIRMED, unpaid order must NOT earn."""
        sample_order.status = OrderStatus.CONFIRMED
        sample_order.is_paid = False
        proc_p, idem_p = _guards(order_service)
        with proc_p as proc, idem_p:
            order_service.maybe_award_purchase_points(sample_order, commit=False)
        proc.assert_not_called()

    def test_no_award_when_paid_but_not_delivered(self, order_service, sample_order):
        """Strict rule: a prepaid order paid but not yet delivered must NOT earn."""
        sample_order.status = OrderStatus.CONFIRMED
        sample_order.is_paid = True
        sample_order.payment_method = PaymentMethod.CLICK
        proc_p, idem_p = _guards(order_service)
        with proc_p as proc, idem_p:
            order_service.maybe_award_purchase_points(sample_order, commit=False)
        proc.assert_not_called()

    def test_no_award_when_delivered_but_unpaid(self, order_service, sample_order):
        """COD delivered before cash is collected must NOT earn until is_paid flips."""
        sample_order.status = OrderStatus.DELIVERED
        sample_order.is_paid = False
        sample_order.payment_method = PaymentMethod.CASH
        proc_p, idem_p = _guards(order_service)
        with proc_p as proc, idem_p:
            order_service.maybe_award_purchase_points(sample_order, commit=False)
        proc.assert_not_called()

    def test_awards_when_delivered_and_paid(self, order_service, sample_order):
        sample_order.status = OrderStatus.DELIVERED
        sample_order.is_paid = True
        proc_p, idem_p = _guards(order_service)
        with proc_p as proc, idem_p:
            order_service.maybe_award_purchase_points(sample_order, commit=False)
        proc.assert_called_once()

    def test_idempotent_when_already_awarded(self, order_service, sample_order):
        """Delivery edge + payment edge can both fire; only the first awards."""
        sample_order.status = OrderStatus.DELIVERED
        sample_order.is_paid = True
        proc_p, idem_p = _guards(order_service, already_awarded=True)
        with proc_p as proc, idem_p:
            order_service.maybe_award_purchase_points(sample_order, commit=False)
        proc.assert_not_called()

    def test_string_status_value_is_handled(self, order_service, sample_order):
        """DB rows can surface status as a raw string; the guard must still match."""
        sample_order.status = "delivered"
        sample_order.is_paid = True
        proc_p, idem_p = _guards(order_service)
        with proc_p as proc, idem_p:
            order_service.maybe_award_purchase_points(sample_order, commit=False)
        proc.assert_called_once()


@pytest.mark.unit
class TestHasPurchaseAward:
    """Idempotency probe: only a PURCHASE award (EARNED + order_id) counts."""

    def test_false_when_no_award(self, db, sample_order):
        assert LoyaltyService().has_purchase_award(sample_order.id) is False

    def test_true_after_earned_purchase_transaction(self, db, sample_order):
        db.session.add(
            LoyaltyTransaction(
                user_id=sample_order.user_id,
                transaction_type=LoyaltyTransactionType.EARNED,
                points=60,
                description="Order purchase",
                order_id=sample_order.id,
                remaining_points=60,
            )
        )
        db.session.commit()
        assert LoyaltyService().has_purchase_award(sample_order.id) is True

    def test_adjustment_does_not_count_as_award(self, db, sample_order):
        """An order-edit clawback (ADJUSTMENT) must not look like the initial award."""
        db.session.add(
            LoyaltyTransaction(
                user_id=sample_order.user_id,
                transaction_type=LoyaltyTransactionType.ADJUSTMENT,
                points=-10,
                description="edit clawback",
                order_id=sample_order.id,
            )
        )
        db.session.commit()
        assert LoyaltyService().has_purchase_award(sample_order.id) is False

    def test_none_order_id_is_false(self, db):
        assert LoyaltyService().has_purchase_award(None) is False
