"""
Unit tests for CartService aligned with current implementation.
"""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from business_app.services.cart_service import CartService
from business_app.utils.exceptions import ValidationError, NotFoundError


@pytest.fixture
def cart_service(app):
    with app.app_context():
        return CartService()


@pytest.mark.unit
@pytest.mark.cart
class TestCartService:
    def test_validate_cart_items_empty_raises(self, cart_service):
        with pytest.raises(ValidationError, match="Cart cannot be empty"):
            cart_service.validate_cart_items([])

    def test_validate_cart_items_valid(self, cart_service, sample_product):
        validated, errors = cart_service.validate_cart_items([
            {"product_id": sample_product.id, "quantity": 2}
        ])

        assert len(errors) == 0
        assert len(validated) == 1
        assert validated[0]["product_id"] == sample_product.id
        assert validated[0]["quantity"] == 2

    def test_validate_cart_items_duplicate_product_returns_error(self, cart_service, sample_product):
        validated, errors = cart_service.validate_cart_items([
            {"product_id": sample_product.id, "quantity": 1},
            {"product_id": sample_product.id, "quantity": 2},
        ])

        assert len(validated) == 1
        assert any("Duplicate item" in err for err in errors)

    def test_calculate_cart_estimate_user_not_found(self, cart_service, sample_product):
        with pytest.raises(NotFoundError):
            cart_service.calculate_cart_estimate(
                user_id=999999,
                items=[{"product_id": sample_product.id, "quantity": 1}],
            )

    def test_calculate_cart_estimate_returns_pricing(self, cart_service, sample_user, sample_product):
        estimate = cart_service.calculate_cart_estimate(
            user_id=sample_user.id,
            items=[{"product_id": sample_product.id, "quantity": 2}],
        )

        assert "pricing" in estimate
        assert estimate["pricing"]["items_subtotal"] > 0
        assert "delivery" in estimate
        assert "validation" in estimate

    def test_validate_cart_items_uses_contract_price_for_entity_user(
        self,
        cart_service,
        sample_user,
        sample_product,
        db,
    ):
        sample_user.user_type = "entity"
        db.session.add(sample_user)
        db.session.commit()

        corporate_service = MagicMock()
        corporate_service.resolve_contract_pricing_for_user_product.return_value = {
            "unit_price": Decimal("12500.00"),
            "contract": None,
            "contract_price_row": None,
        }

        with patch(
            "business_app.utils.service_factory.get_corporate_contract_service",
            return_value=corporate_service,
        ):
            validated, errors = cart_service.validate_cart_items(
                [{"product_id": sample_product.id, "quantity": 2}],
                sample_user,
            )

        assert errors == []
        assert validated[0]["unit_price"] == 12500.0
        assert validated[0]["subtotal"] == 25000.0
        corporate_service.resolve_contract_pricing_for_user_product.assert_called_once_with(
            user_id=sample_user.id,
            product_id=sample_product.id,
            fallback_price=Decimal("15000.0"),
        )

    def test_validate_cart_items_uses_reservation_aware_inventory(self, cart_service, sample_product):
        cart_service._inventory_service = MagicMock()
        cart_service._inventory_service.check_product_availability.return_value = SimpleNamespace(
            is_available=False,
            reason="Insufficient stock",
            available_quantity=4,
            reserved_quantity=3,
        )

        validated, errors = cart_service.validate_cart_items([
            {"product_id": sample_product.id, "quantity": 5}
        ])

        assert validated == []
        assert len(errors) == 1
        assert "Only 4 available (reserved: 3), requested 5" in errors[0]

    def test_add_item_to_cart_rejects_insufficient_available_quantity(
        self,
        cart_service,
        sample_user,
        sample_product,
    ):
        cart_service._inventory_service = MagicMock()
        cart_service._inventory_service.check_product_availability.return_value = SimpleNamespace(
            is_available=False,
            reason="Insufficient stock",
            available_quantity=2,
            reserved_quantity=5,
        )

        with pytest.raises(ValidationError, match="Only 2 available"):
            cart_service.add_item_to_cart(
                user_id=sample_user.id,
                product_id=sample_product.id,
                quantity=3,
            )

    def test_validate_cart_items_rejects_quantity_below_min_order_quantity(
        self, cart_service, sample_product, db
    ):
        sample_product.min_order_quantity = 3
        db.session.add(sample_product)
        db.session.commit()

        validated, errors = cart_service.validate_cart_items(
            [{"product_id": sample_product.id, "quantity": 2}]
        )

        assert validated == []
        assert len(errors) == 1
        assert "minimum order quantity is 3" in errors[0]
        assert "you ordered 2" in errors[0]

    def test_validate_cart_items_passes_at_min_order_quantity(
        self, cart_service, sample_product, db
    ):
        sample_product.min_order_quantity = 3
        db.session.add(sample_product)
        db.session.commit()

        validated, errors = cart_service.validate_cart_items(
            [{"product_id": sample_product.id, "quantity": 3}]
        )

        assert errors == []
        assert len(validated) == 1
        assert validated[0]["quantity"] == 3
