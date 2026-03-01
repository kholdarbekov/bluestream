"""Business-rule unit tests for OrderService pricing and discount logic."""

from decimal import Decimal
from unittest.mock import Mock, patch

import pytest

from business_app.services.order_service import OrderService
from business_app.utils.constants import OrderStatus
from business_app.utils.exceptions import ConflictError, NotFoundError


@pytest.fixture
def order_service(app, mock_inventory_service):
    with app.app_context():
        return OrderService(inventory_service=mock_inventory_service)


@pytest.mark.unit
@pytest.mark.order
class TestOrderServiceDiscountRules:
    def test_apply_discount_raises_not_found_when_order_missing(self, order_service, db):
        with pytest.raises(NotFoundError, match="Order not found"):
            order_service.apply_discount(order_id=999999, discount_amount=1000)

    def test_apply_discount_rejects_non_pending_orders(self, order_service, sample_order, db):
        sample_order.status = OrderStatus.CONFIRMED
        db.session.commit()

        with pytest.raises(ConflictError, match="Cannot apply discount to confirmed order"):
            order_service.apply_discount(order_id=sample_order.id, discount_amount=500)

    def test_apply_discount_caps_amount_to_subtotal(self, order_service, sample_order, db):
        subtotal = Decimal(sample_order.subtotal)
        order_service.apply_discount(order_id=sample_order.id, discount_amount=subtotal + Decimal("999.00"))
        db.session.refresh(sample_order)

        assert Decimal(sample_order.discount_amount) == subtotal
        assert Decimal(sample_order.total_amount) == Decimal(sample_order.delivery_fee)

    def test_apply_discount_uses_loyalty_service_for_discount_code(self, order_service, sample_order, db):
        with patch("business_app.services.loyalty_service.LoyaltyService") as loyalty_cls:
            loyalty_instance = loyalty_cls.return_value
            loyalty_instance.validate_discount_code.return_value = Decimal("1200.00")

            updated = order_service.apply_discount(
                order_id=sample_order.id,
                discount_code="SAVE1200",
            )

        assert Decimal(updated.discount_amount) == Decimal("1200.00")
        assert updated.discount_code == "SAVE1200"
        loyalty_instance.validate_discount_code.assert_called_once_with("SAVE1200", sample_order.user_id)
        db.session.refresh(sample_order)
        assert sample_order.discount_code == "SAVE1200"

    @pytest.mark.parametrize("discount_amount", [0, -100, None])
    def test_apply_discount_no_change_for_non_positive_amount(self, order_service, sample_order, db, discount_amount):
        original_discount = Decimal(sample_order.discount_amount)
        original_total = Decimal(sample_order.total_amount)

        updated = order_service.apply_discount(order_id=sample_order.id, discount_amount=discount_amount)

        assert Decimal(updated.discount_amount) == original_discount
        assert Decimal(updated.total_amount) == original_total

    def test_process_loyalty_points_awards_using_eligible_amount(self, order_service, sample_order):
        sample_order.total_amount = Decimal("52000.00")

        with patch("business_app.services.corporate_contract_service.CorporateContractService") as contract_cls, patch(
            "business_app.services.loyalty_service.LoyaltyService"
        ) as loyalty_cls:
            contract_cls.return_value.get_loyalty_eligible_amount_for_order.return_value = Decimal("35000.00")
            loyalty_instance = loyalty_cls.return_value
            loyalty_instance.calculate_points_for_purchase.return_value = 140

            order_service._process_loyalty_points_for_order(sample_order)

        contract_cls.return_value.get_loyalty_eligible_amount_for_order.assert_called_once_with(sample_order)
        loyalty_instance.calculate_points_for_purchase.assert_called_once_with(sample_order.user_id, 35000)
        loyalty_instance.award_points.assert_called_once()
        assert sample_order.loyalty_points_earned == 140

    def test_process_loyalty_points_skips_ineligible_contract_only_orders(self, order_service, sample_order):
        sample_order.loyalty_points_earned = 99

        with patch("business_app.services.corporate_contract_service.CorporateContractService") as contract_cls, patch(
            "business_app.services.loyalty_service.LoyaltyService"
        ) as loyalty_cls:
            contract_cls.return_value.get_loyalty_eligible_amount_for_order.return_value = Decimal("0.00")

            order_service._process_loyalty_points_for_order(sample_order)

        loyalty_instance = loyalty_cls.return_value
        loyalty_instance.calculate_points_for_purchase.assert_not_called()
        loyalty_instance.award_points.assert_not_called()
        assert sample_order.loyalty_points_earned == 0

    def test_process_loyalty_points_keeps_non_contract_orders_unchanged(self, order_service, sample_order):
        sample_order.total_amount = Decimal("47000.00")

        with patch("business_app.services.corporate_contract_service.CorporateContractService") as contract_cls, patch(
            "business_app.services.loyalty_service.LoyaltyService"
        ) as loyalty_cls:
            contract_cls.return_value.get_loyalty_eligible_amount_for_order.return_value = Decimal("47000.00")
            loyalty_instance = loyalty_cls.return_value
            loyalty_instance.calculate_points_for_purchase.return_value = 188

            order_service._process_loyalty_points_for_order(sample_order)

        loyalty_instance.calculate_points_for_purchase.assert_called_once_with(sample_order.user_id, 47000)
        loyalty_instance.award_points.assert_called_once()
        assert sample_order.loyalty_points_earned == 188
