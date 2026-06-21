"""Call-site wiring for delivered-AND-paid purchase accrual.

Verifies that AquaCoins are NOT awarded at CONFIRMED, ARE awarded at the
delivered edge, and are (re)evaluated at the payment edge for both COD cash
collection and prepaid payment success. Complements the guard unit tests in
``tests/unit/test_loyalty_award_on_delivered_paid.py``.
"""

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest

from business_app.models.payment import Payment
from business_app.services.cash_collection_service import CashCollectionService
from business_app.services.order_service import OrderService
from business_app.services.payment_service import PaymentService
from shared.enums import OrderStatus, PaymentMethod, PaymentStatus


@pytest.fixture
def order_service(app, mock_inventory_service):
    with app.app_context():
        return OrderService(inventory_service=mock_inventory_service)


@pytest.mark.integration
@pytest.mark.order
class TestStatusChangeAwardWiring:
    def test_confirmed_does_not_award(self, order_service, sample_order):
        """A non-cash order reaching CONFIRMED must NOT earn AquaCoins."""
        sample_order.payment_method = PaymentMethod.CLICK
        with patch("business_app.services.delivery_service.DeliveryService"), patch.object(
            order_service, "_confirm_inventory_for_order"
        ), patch.object(order_service, "maybe_award_purchase_points") as award, patch.object(
            order_service, "_process_loyalty_points_for_order"
        ) as proc:
            order_service._handle_status_change_actions(sample_order, OrderStatus.CONFIRMED, commit=False)
        award.assert_not_called()
        proc.assert_not_called()

    def test_delivered_routes_through_guard(self, order_service, sample_order):
        """Delivery edge must evaluate the (delivered AND paid) award for ALL orders."""
        sample_order.payment_method = PaymentMethod.CLICK
        sample_order.status = OrderStatus.DELIVERED
        sample_order.is_paid = True
        with patch("business_app.services.delivery_service.DeliveryService"), patch(
            "business_app.services.loyalty_service.LoyaltyService"
        ), patch("business_app.services.corporate_contract_service.CorporateContractService"), patch.object(
            order_service, "maybe_award_purchase_points"
        ) as award:
            order_service._handle_status_change_actions(sample_order, OrderStatus.DELIVERED, commit=False)
        award.assert_called_once()

    def test_delivered_cod_routes_through_guard(self, order_service, sample_order):
        sample_order.payment_method = PaymentMethod.CASH
        sample_order.status = OrderStatus.DELIVERED
        # update_order_status sets this transient attr via _update_status_fields
        # before _handle_status_change_actions runs; mirror it here.
        sample_order.delivered_at = datetime.now(timezone.utc)
        with patch("business_app.services.delivery_service.DeliveryService"), patch(
            "business_app.services.loyalty_service.LoyaltyService"
        ), patch("business_app.services.corporate_contract_service.CorporateContractService"), patch(
            "business_app.services.cash_collection_service.CashCollectionService"
        ), patch.object(order_service, "_confirm_inventory_for_order"), patch.object(
            order_service, "maybe_award_purchase_points"
        ) as award:
            order_service._handle_status_change_actions(sample_order, OrderStatus.DELIVERED, commit=False)
        award.assert_called_once()


@pytest.mark.integration
@pytest.mark.order
class TestPaymentEdgeAwardWiring:
    def _cod_payment(self, db, order, *, collected):
        payment = Payment(
            order_id=order.id,
            user_id=order.user_id,
            payment_method=PaymentMethod.CASH,
            amount=order.total_amount,
            currency="UZS",
            status=PaymentStatus.PENDING,
            amount_collected=collected,
            outstanding_amount=order.total_amount - collected,
        )
        db.session.add(payment)
        db.session.commit()
        return payment

    def test_cod_full_collection_on_delivered_order_triggers_award(self, db, sample_order):
        """COD cash collected after delivery flips is_paid → award is evaluated."""
        sample_order.status = OrderStatus.DELIVERED
        sample_order.payment_method = PaymentMethod.CASH
        sample_order.is_paid = False
        db.session.commit()
        payment = self._cod_payment(db, sample_order, collected=sample_order.total_amount)

        with patch.object(OrderService, "maybe_award_purchase_points") as award:
            # A completed cash payment records its collector (ARCH-006).
            CashCollectionService().sync_payment_projection(payment, collected_by=sample_order.user_id)

        assert sample_order.is_paid is True
        award.assert_called_once()

    def test_cod_partial_collection_does_not_trigger_award(self, db, sample_order):
        sample_order.status = OrderStatus.DELIVERED
        sample_order.payment_method = PaymentMethod.CASH
        sample_order.is_paid = False
        db.session.commit()
        payment = self._cod_payment(db, sample_order, collected=Decimal("1000.00"))

        with patch.object(OrderService, "maybe_award_purchase_points") as award:
            CashCollectionService().sync_payment_projection(payment)

        assert sample_order.is_paid is False
        award.assert_not_called()

    def test_prepaid_payment_success_on_delivered_order_triggers_award(self, db, sample_order):
        """Prepaid payment completing after delivery flips is_paid → award is evaluated."""
        sample_order.status = OrderStatus.DELIVERED
        sample_order.payment_method = PaymentMethod.CLICK
        sample_order.is_paid = False
        db.session.commit()
        payment = Payment(
            order_id=sample_order.id,
            user_id=sample_order.user_id,
            payment_method=PaymentMethod.CLICK,
            amount=sample_order.total_amount,
            currency="UZS",
            status=PaymentStatus.COMPLETED,
            amount_collected=sample_order.total_amount,
            outstanding_amount=Decimal("0.00"),
            paid_at=datetime.now(timezone.utc),
        )
        db.session.add(payment)
        db.session.commit()

        with patch.object(OrderService, "maybe_award_purchase_points") as award:
            PaymentService()._handle_successful_payment(payment, trigger_notifications=False)

        award.assert_called_once()
