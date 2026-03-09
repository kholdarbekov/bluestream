"""
Unit tests for OrderService aligned with current implementation.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from business_app.models.delivery import Delivery
from business_app.services.order_service import OrderService
from business_app.utils.constants import DeliveryStatus, OrderStatus, PaymentMethod
from business_app.utils.exceptions import ConflictError, ValidationError


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

    def test_process_order_items_invalid_structure(self, order_service, sample_user):
        with pytest.raises(ValidationError, match="Each item must have product_id and quantity"):
            order_service._process_order_items([{"product_id": 1}], user_id=sample_user.id)

    def test_process_order_items_success(self, order_service, sample_product, sample_user, monkeypatch):
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
        ], user_id=sample_user.id)

        assert len(items) == 1
        assert items[0]["product_id"] == sample_product.id
        assert subtotal > 0

    def test_status_transition_rules(self, order_service):
        assert order_service._is_valid_status_transition(OrderStatus.PENDING, OrderStatus.CONFIRMED) is True
        assert order_service._is_valid_status_transition(OrderStatus.DELIVERED, OrderStatus.PENDING) is False

    def test_cancel_order_cancels_linked_delivery(self, order_service, sample_order, sample_user, db, monkeypatch):
        delivery = Delivery(
            order_id=sample_order.id,
            status=DeliveryStatus.SCHEDULED,
            scheduled_date=datetime.now(UTC),
            scheduled_time_slot="09:00-12:00",
        )
        db.session.add(delivery)
        sample_order.payment_method = PaymentMethod.CASH
        db.session.commit()
        monkeypatch.setattr(order_service.inventory_service, "release_reservations", lambda *_args, **_kwargs: {"success": True})

        with patch("business_app.services.corporate_contract_service.CorporateContractService.release_for_order") as release_for_order:
            with patch("business_app.services.cash_collection_service.CashCollectionService.release_reserved_prepayment_for_order") as release_reserved:
                cancelled = order_service.cancel_order(sample_order.id, user_id=sample_user.id, reason="Customer request")

        db.session.refresh(sample_order)
        db.session.refresh(delivery)

        assert cancelled.status == OrderStatus.CANCELLED
        assert sample_order.status == OrderStatus.CANCELLED
        assert delivery.status == DeliveryStatus.CANCELLED
        release_reserved.assert_called_once_with(
            order_id=sample_order.id,
            actor_user_id=sample_user.id,
            reason=f"Order moved to {OrderStatus.CANCELLED.value}",
        )
        release_for_order.assert_called_once()

    def test_cancel_order_rejects_when_delivery_in_transit(self, order_service, sample_order, sample_user, db):
        sample_order.status = OrderStatus.CONFIRMED
        delivery = Delivery(
            order_id=sample_order.id,
            status=DeliveryStatus.IN_TRANSIT,
            scheduled_date=datetime.now(UTC),
            scheduled_time_slot="09:00-12:00",
        )
        db.session.add(delivery)
        db.session.commit()

        with pytest.raises(ConflictError, match="in transit"):
            order_service.cancel_order(sample_order.id, user_id=sample_user.id, reason="Customer request")
