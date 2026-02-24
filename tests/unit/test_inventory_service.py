"""
Unit tests for InventoryService aligned with current implementation.
"""

import pytest

from business_app.services.inventory_service import InventoryService, InventoryOperationType
from business_app.utils.exceptions import NotFoundError


@pytest.fixture
def inventory_service(app):
    with app.app_context():
        return InventoryService()


@pytest.mark.unit
@pytest.mark.inventory
class TestInventoryService:
    def test_check_product_availability_success(self, inventory_service, sample_product):
        result = inventory_service.check_product_availability(sample_product.id, 1)

        assert result.is_available is True
        assert result.product_id == sample_product.id

    def test_check_product_availability_missing_product(self, inventory_service, db):
        with pytest.raises(NotFoundError):
            inventory_service.check_product_availability(999999, 1)

    def test_reserve_inventory_returns_failure_when_unavailable(self, inventory_service, sample_product, monkeypatch):
        unavailable = inventory_service.check_product_availability(sample_product.id, sample_product.stock_quantity + 1)
        monkeypatch.setattr(inventory_service, "check_multiple_products_availability", lambda *_args, **_kwargs: [unavailable])

        result = inventory_service.reserve_inventory(order_id=1, items=[{"product_id": sample_product.id, "quantity": 9999}])

        assert result["success"] is False
        assert "Insufficient inventory" in result["reason"]

    def test_adjust_inventory_success(self, inventory_service, sample_product, db):
        old_stock = sample_product.stock_quantity

        result = inventory_service.adjust_inventory(
            product_id=sample_product.id,
            quantity_change=5,
            operation_type=InventoryOperationType.MANUAL_ADJUSTMENT,
            reason="test adjustment",
            user_id=None,
        )

        db.session.refresh(sample_product)
        assert result["success"] is True
        assert sample_product.stock_quantity == old_stock + 5

    def test_get_inventory_status(self, inventory_service, sample_product):
        status = inventory_service.get_inventory_status(sample_product.id)

        assert status["product_id"] == sample_product.id
        assert "available_quantity" in status
        assert "is_in_stock" in status
