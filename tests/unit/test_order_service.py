"""
Unit tests for OrderService aligned with current implementation.
"""

from types import SimpleNamespace

import pytest

from business_app.services.order_service import OrderService
from business_app.utils.constants import OrderStatus
from business_app.utils.exceptions import ValidationError


@pytest.fixture
def order_service(app, mock_inventory_service):
    with app.app_context():
        return OrderService(inventory_service=mock_inventory_service)


@pytest.mark.unit
@pytest.mark.order
class TestOrderService:
    def test_validate_order_data_missing_items(self, order_service):
        with pytest.raises(ValidationError, match="Missing required field: items"):
            order_service._validate_order_data({"delivery_address": {"street": "X", "latitude": 1, "longitude": 1}})

    def test_validate_order_data_missing_address_field(self, order_service):
        with pytest.raises(ValidationError, match="Missing required address field"):
            order_service._validate_order_data({"items": [{"product_id": 1, "quantity": 1}], "delivery_address": {"street": "X"}})

    def test_process_order_items_invalid_structure(self, order_service):
        with pytest.raises(ValidationError, match="Each item must have product_id and quantity"):
            order_service._process_order_items([{"product_id": 1}])

    def test_process_order_items_success(self, order_service, sample_product, monkeypatch):
        availability = [
            SimpleNamespace(
                product_id=sample_product.id,
                requested_quantity=2,
                available_quantity=sample_product.stock_quantity,
                reserved_quantity=0,
                is_available=True,
                reason="Available",
            )
        ]
        monkeypatch.setattr(order_service.inventory_service, "check_multiple_products_availability", lambda *_args, **_kwargs: availability)

        items, subtotal = order_service._process_order_items([
            {"product_id": sample_product.id, "quantity": 2}
        ])

        assert len(items) == 1
        assert items[0]["product_id"] == sample_product.id
        assert subtotal > 0

    def test_status_transition_rules(self, order_service):
        assert order_service._is_valid_status_transition(OrderStatus.PENDING, OrderStatus.CONFIRMED) is True
        assert order_service._is_valid_status_transition(OrderStatus.DELIVERED, OrderStatus.PENDING) is False
