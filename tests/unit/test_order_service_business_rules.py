"""Business-rule unit tests for OrderService pricing and discount logic."""

from decimal import Decimal
from unittest.mock import patch

import pytest

from business_app.services.order_service import OrderService


@pytest.fixture
def order_service(app, mock_inventory_service):
    with app.app_context():
        return OrderService(inventory_service=mock_inventory_service)


@pytest.mark.unit
@pytest.mark.order
class TestOrderServiceDiscountRules:
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
