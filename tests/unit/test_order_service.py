"""
Unit tests for Order Service - Critical Business Logic
Tests order creation, validation, calculations, and lifecycle management
"""
import pytest
from decimal import Decimal
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, UTC, timedelta

from business_app.services.order_service import OrderService
from business_app.models.order import Order, OrderItem
from business_app.models.product import Product
from business_app.models.user import User
from business_app.utils.constants import OrderStatus, ProductCategory
from business_app.utils.exceptions import OrderError, ValidationError, InsufficientStockError


@pytest.fixture
def order_service(mock_inventory_service, mock_delivery_service, mock_notification_service):
    """Create OrderService instance with mocked dependencies"""
    service = OrderService()
    service.inventory_service = mock_inventory_service
    service.delivery_service = mock_delivery_service
    service.notification_service = mock_notification_service
    return service


@pytest.fixture
def multiple_products(db):
    """Create multiple products for testing"""
    products = []
    
    # Water products
    water_19l = Product(
        name='Pure Water 19L',
        category='water',
        size='large',
        volume=Decimal('19.00'),
        base_price=Decimal('15000.00'),
        stock_quantity=50,
        is_active=True
    )
    products.append(water_19l)
    
    water_5l = Product(
        name='Pure Water 5L',
        category='water',
        size='medium',
        volume=Decimal('5.00'),
        base_price=Decimal('8000.00'),
        stock_quantity=100,
        is_active=True
    )
    products.append(water_5l)
    
    # Accessories
    dispenser = Product(
        name='Water Dispenser',
        category='accessories',
        size='large',
        base_price=Decimal('120000.00'),
        stock_quantity=10,
        is_active=True
    )
    products.append(dispenser)
    
    for product in products:
        db.session.add(product)
    
    db.session.commit()
    return products


@pytest.mark.critical
@pytest.mark.order
class TestOrderCreation:
    """Test order creation and validation"""
    
    def test_create_order_valid_data(self, order_service, sample_user, multiple_products, db):
        """Test creating order with valid data"""
        order_data = {
            'user_id': sample_user.id,
            'items': [
                {
                    'product_id': multiple_products[0].id,
                    'quantity': 2,
                    'unit_price': multiple_products[0].base_price
                },
                {
                    'product_id': multiple_products[1].id,
                    'quantity': 1,
                    'unit_price': multiple_products[1].base_price
                }
            ],
            'delivery_address': {
                'address_line1': '123 Test Street',
                'city': 'Tashkent',
                'latitude': 41.2995,
                'longitude': 69.2401
            },
            'notes': 'Test order'
        }
        
        with patch.object(order_service, '_generate_order_number', return_value='ORD-2024-001'):
            order = order_service.create_order(order_data)
            
            assert order.user_id == sample_user.id
            assert order.order_number == 'ORD-2024-001'
            assert order.status == OrderStatus.PENDING
            assert len(order.items) == 2
            
            # Verify calculations
            expected_subtotal = (Decimal('15000.00') * 2) + (Decimal('8000.00') * 1)
            assert order.subtotal == expected_subtotal
    
    def test_create_order_empty_items(self, order_service, sample_user):
        """Test order creation with empty items"""
        order_data = {
            'user_id': sample_user.id,
            'items': [],
            'delivery_address': {
                'address_line1': '123 Test Street',
                'city': 'Tashkent'
            }
        }
        
        with pytest.raises(ValidationError, match="Order must contain at least one item"):
            order_service.create_order(order_data)
    
    def test_create_order_invalid_quantity(self, order_service, sample_user, multiple_products):
        """Test order creation with invalid quantity"""
        order_data = {
            'user_id': sample_user.id,
            'items': [
                {
                    'product_id': multiple_products[0].id,
                    'quantity': 0,  # Invalid quantity
                    'unit_price': multiple_products[0].base_price
                }
            ],
            'delivery_address': {
                'address_line1': '123 Test Street',
                'city': 'Tashkent'
            }
        }
        
        with pytest.raises(ValidationError, match="Quantity must be greater than 0"):
            order_service.create_order(order_data)
    
    def test_create_order_excessive_quantity(self, order_service, sample_user, multiple_products):
        """Test order creation with quantity exceeding limits"""
        order_data = {
            'user_id': sample_user.id,
            'items': [
                {
                    'product_id': multiple_products[0].id,
                    'quantity': 101,  # Exceeds max quantity per item (100)
                    'unit_price': multiple_products[0].base_price
                }
            ],
            'delivery_address': {
                'address_line1': '123 Test Street',
                'city': 'Tashkent'
            }
        }
        
        with pytest.raises(ValidationError, match="Quantity exceeds maximum allowed"):
            order_service.create_order(order_data)
    
    def test_create_order_inactive_product(self, order_service, sample_user, db):
        """Test order creation with inactive product"""
        # Create inactive product
        inactive_product = Product(
            name='Inactive Product',
            category='water',
            base_price=Decimal('10000.00'),
            stock_quantity=10,
            is_active=False  # Inactive
        )
        db.session.add(inactive_product)
        db.session.commit()
        
        order_data = {
            'user_id': sample_user.id,
            'items': [
                {
                    'product_id': inactive_product.id,
                    'quantity': 1,
                    'unit_price': inactive_product.base_price
                }
            ],
            'delivery_address': {
                'address_line1': '123 Test Street',
                'city': 'Tashkent'
            }
        }
        
        with pytest.raises(ValidationError, match="Product is not available"):
            order_service.create_order(order_data)


@pytest.mark.critical
@pytest.mark.order
class TestOrderCalculations:
    """Test order price calculations"""
    
    def test_calculate_subtotal(self, order_service, multiple_products):
        """Test subtotal calculation"""
        items = [
            {
                'product_id': multiple_products[0].id,
                'quantity': 2,
                'unit_price': multiple_products[0].base_price
            },
            {
                'product_id': multiple_products[1].id,
                'quantity': 3,
                'unit_price': multiple_products[1].base_price
            }
        ]
        
        subtotal = order_service._calculate_subtotal(items)
        expected = (Decimal('15000.00') * 2) + (Decimal('8000.00') * 3)
        assert subtotal == expected
    
    def test_calculate_delivery_fee_by_distance(self, order_service):
        """Test delivery fee calculation based on distance"""
        # Central zone (free delivery)
        central_address = {'latitude': 41.2995, 'longitude': 69.2401}
        central_fee = order_service._calculate_delivery_fee(central_address, Decimal('50000.00'))
        assert central_fee == Decimal('0.00')
        
        # Inner zone
        inner_address = {'latitude': 41.3200, 'longitude': 69.2800}
        with patch.object(order_service.delivery_service, 'calculate_distance', return_value=8.0):
            inner_fee = order_service._calculate_delivery_fee(inner_address, Decimal('30000.00'))
            assert inner_fee == Decimal('3000.00')
        
        # Outer zone
        outer_address = {'latitude': 41.4000, 'longitude': 69.3500}
        with patch.object(order_service.delivery_service, 'calculate_distance', return_value=15.0):
            outer_fee = order_service._calculate_delivery_fee(outer_address, Decimal('30000.00'))
            assert outer_fee == Decimal('5000.00')
    
    def test_free_delivery_threshold(self, order_service):
        """Test free delivery for orders above threshold"""
        address = {'latitude': 41.3200, 'longitude': 69.2800}
        
        # Order above free delivery threshold (50,000 UZS)
        high_value_fee = order_service._calculate_delivery_fee(address, Decimal('60000.00'))
        assert high_value_fee == Decimal('0.00')
        
        # Order below threshold
        with patch.object(order_service.delivery_service, 'calculate_distance', return_value=8.0):
            low_value_fee = order_service._calculate_delivery_fee(address, Decimal('30000.00'))
            assert low_value_fee == Decimal('3000.00')
    
    def test_apply_loyalty_discount(self, order_service, sample_user, db):
        """Test loyalty points discount application"""
        # Set user loyalty points
        sample_user.loyalty_points = 1000
        db.session.commit()
        
        order_total = Decimal('20000.00')
        discount_amount = Decimal('5000.00')  # 500 points = 5000 UZS
        
        applied_discount = order_service._apply_loyalty_discount(
            sample_user.id, order_total, discount_amount
        )
        
        assert applied_discount == discount_amount
        
        # Verify points deducted
        db.session.refresh(sample_user)
        assert sample_user.loyalty_points == 500  # 1000 - 500
    
    def test_apply_loyalty_discount_insufficient_points(self, order_service, sample_user, db):
        """Test loyalty discount with insufficient points"""
        # Set low loyalty points
        sample_user.loyalty_points = 100
        db.session.commit()
        
        order_total = Decimal('20000.00')
        discount_amount = Decimal('5000.00')  # 500 points needed, but only 100 available
        
        with pytest.raises(ValidationError, match="Insufficient loyalty points"):
            order_service._apply_loyalty_discount(
                sample_user.id, order_total, discount_amount
            )
    
    def test_calculate_final_total(self, order_service):
        """Test final total calculation"""
        subtotal = Decimal('30000.00')
        delivery_fee = Decimal('3000.00')
        discount_amount = Decimal('2000.00')
        loyalty_discount = Decimal('1000.00')
        
        total = order_service._calculate_final_total(
            subtotal, delivery_fee, discount_amount, loyalty_discount
        )
        
        expected_total = Decimal('30000.00') + Decimal('3000.00') - Decimal('2000.00') - Decimal('1000.00')
        assert total == expected_total
    
    def test_minimum_order_amount(self, order_service, sample_user, multiple_products):
        """Test minimum order amount validation"""
        order_data = {
            'user_id': sample_user.id,
            'items': [
                {
                    'product_id': multiple_products[1].id,
                    'quantity': 1,
                    'unit_price': Decimal('5000.00')  # Below minimum order amount
                }
            ],
            'delivery_address': {
                'address_line1': '123 Test Street',
                'city': 'Tashkent'
            }
        }
        
        with pytest.raises(ValidationError, match="Order amount below minimum"):
            order_service.create_order(order_data)


@pytest.mark.critical
@pytest.mark.order
class TestOrderStatusManagement:
    """Test order status lifecycle management"""
    
    def test_confirm_order_success(self, order_service, sample_order, db):
        """Test successful order confirmation"""
        # Mock inventory service to confirm stock availability
        order_service.inventory_service.check_availability.return_value = True
        order_service.inventory_service.reserve_stock.return_value = True
        
        result = order_service.confirm_order(sample_order.id)
        
        assert result['success'] is True
        
        # Verify status updated
        db.session.refresh(sample_order)
        assert sample_order.status == OrderStatus.CONFIRMED
        assert sample_order.confirmed_at is not None
    
    def test_confirm_order_insufficient_stock(self, order_service, sample_order, db):
        """Test order confirmation with insufficient stock"""
        # Mock inventory service to return insufficient stock
        order_service.inventory_service.check_availability.return_value = False
        
        with pytest.raises(InsufficientStockError, match="Insufficient stock"):
            order_service.confirm_order(sample_order.id)
        
        # Order status should remain pending
        db.session.refresh(sample_order)
        assert sample_order.status == OrderStatus.PENDING
    
    def test_cancel_order_pending(self, order_service, sample_order, db):
        """Test cancelling pending order"""
        result = order_service.cancel_order(sample_order.id, "Customer requested cancellation")
        
        assert result['success'] is True
        
        # Verify status updated
        db.session.refresh(sample_order)
        assert sample_order.status == OrderStatus.CANCELLED
        assert sample_order.cancelled_at is not None
        assert "Customer requested" in sample_order.cancellation_reason
    
    def test_cancel_order_confirmed_within_window(self, order_service, sample_order, db):
        """Test cancelling confirmed order within cancellation window"""
        # Set order as confirmed recently
        sample_order.status = OrderStatus.CONFIRMED
        sample_order.confirmed_at = datetime.now(UTC) - timedelta(minutes=30)  # 30 minutes ago
        db.session.commit()
        
        result = order_service.cancel_order(sample_order.id, "Changed mind")
        
        assert result['success'] is True
        
        # Verify status updated
        db.session.refresh(sample_order)
        assert sample_order.status == OrderStatus.CANCELLED
    
    def test_cancel_order_confirmed_outside_window(self, order_service, sample_order, db):
        """Test cancelling confirmed order outside cancellation window"""
        # Set order as confirmed more than 1 hour ago
        sample_order.status = OrderStatus.CONFIRMED
        sample_order.confirmed_at = datetime.now(UTC) - timedelta(hours=2)
        db.session.commit()
        
        with pytest.raises(OrderError, match="Cannot cancel order after"):
            order_service.cancel_order(sample_order.id, "Too late")
    
    def test_cancel_order_in_preparation(self, order_service, sample_order, db):
        """Test cancelling order already in preparation"""
        sample_order.status = OrderStatus.PREPARING
        db.session.commit()
        
        with pytest.raises(OrderError, match="Cannot cancel order in preparation"):
            order_service.cancel_order(sample_order.id, "Cannot cancel")
    
    def test_update_order_status_valid_transition(self, order_service, sample_order, db):
        """Test valid order status transitions"""
        # Pending -> Confirmed
        order_service.update_order_status(sample_order.id, OrderStatus.CONFIRMED)
        db.session.refresh(sample_order)
        assert sample_order.status == OrderStatus.CONFIRMED
        
        # Confirmed -> Preparing
        order_service.update_order_status(sample_order.id, OrderStatus.PREPARING)
        db.session.refresh(sample_order)
        assert sample_order.status == OrderStatus.PREPARING
        
        # Preparing -> Out for Delivery
        order_service.update_order_status(sample_order.id, OrderStatus.OUT_FOR_DELIVERY)
        db.session.refresh(sample_order)
        assert sample_order.status == OrderStatus.OUT_FOR_DELIVERY
        
        # Out for Delivery -> Delivered
        order_service.update_order_status(sample_order.id, OrderStatus.DELIVERED)
        db.session.refresh(sample_order)
        assert sample_order.status == OrderStatus.DELIVERED
    
    def test_update_order_status_invalid_transition(self, order_service, sample_order):
        """Test invalid order status transitions"""
        # Cannot go from Pending directly to Delivered
        with pytest.raises(ValidationError, match="Invalid status transition"):
            order_service.update_order_status(sample_order.id, OrderStatus.DELIVERED)
        
        # Cannot go backwards from Confirmed to Pending
        sample_order.status = OrderStatus.CONFIRMED
        with pytest.raises(ValidationError, match="Invalid status transition"):
            order_service.update_order_status(sample_order.id, OrderStatus.PENDING)


@pytest.mark.critical
@pytest.mark.order
class TestInventoryIntegration:
    """Test order integration with inventory management"""
    
    def test_stock_reservation_on_order_creation(self, order_service, sample_user, multiple_products, db):
        """Test stock reservation when order is created"""
        order_data = {
            'user_id': sample_user.id,
            'items': [
                {
                    'product_id': multiple_products[0].id,
                    'quantity': 2,
                    'unit_price': multiple_products[0].base_price
                }
            ],
            'delivery_address': {
                'address_line1': '123 Test Street',
                'city': 'Tashkent'
            }
        }
        
        order_service.inventory_service.check_availability.return_value = True
        order_service.inventory_service.reserve_stock.return_value = True
        
        with patch.object(order_service, '_generate_order_number', return_value='ORD-001'):
            order = order_service.create_order(order_data)
        
        # Verify stock reservation was called
        order_service.inventory_service.reserve_stock.assert_called_once()
        call_args = order_service.inventory_service.reserve_stock.call_args[0]
        assert call_args[0] == multiple_products[0].id
        assert call_args[1] == 2
    
    def test_stock_deduction_on_order_confirmation(self, order_service, sample_order, db):
        """Test stock deduction when order is confirmed"""
        order_service.inventory_service.check_availability.return_value = True
        order_service.inventory_service.deduct_stock.return_value = True
        
        order_service.confirm_order(sample_order.id)
        
        # Verify stock deduction was called
        order_service.inventory_service.deduct_stock.assert_called_once()
    
    def test_stock_release_on_order_cancellation(self, order_service, sample_order, db):
        """Test stock release when order is cancelled"""
        order_service.inventory_service.release_reserved_stock.return_value = True
        
        order_service.cancel_order(sample_order.id, "Customer cancellation")
        
        # Verify stock release was called
        order_service.inventory_service.release_reserved_stock.assert_called_once()


@pytest.mark.order
class TestOrderModification:
    """Test order modification functionality"""
    
    def test_modify_order_items_pending(self, order_service, sample_order, multiple_products, db):
        """Test modifying order items while order is pending"""
        # Add new item to pending order
        new_item_data = {
            'product_id': multiple_products[1].id,
            'quantity': 1,
            'unit_price': multiple_products[1].base_price
        }
        
        order_service.inventory_service.check_availability.return_value = True
        
        result = order_service.add_item_to_order(sample_order.id, new_item_data)
        
        assert result['success'] is True
        
        # Verify item added
        db.session.refresh(sample_order)
        assert len(sample_order.items) == 1  # Originally had 0, now has 1
    
    def test_modify_order_items_confirmed(self, order_service, sample_order, db):
        """Test that confirmed orders cannot be modified"""
        sample_order.status = OrderStatus.CONFIRMED
        db.session.commit()
        
        new_item_data = {
            'product_id': 1,
            'quantity': 1,
            'unit_price': Decimal('10000.00')
        }
        
        with pytest.raises(OrderError, match="Cannot modify confirmed order"):
            order_service.add_item_to_order(sample_order.id, new_item_data)
    
    def test_update_delivery_address_pending(self, order_service, sample_order, db):
        """Test updating delivery address for pending order"""
        new_address = {
            'address_line1': '456 New Street',
            'city': 'Samarkand',
            'latitude': 39.6542,
            'longitude': 66.9597
        }
        
        result = order_service.update_delivery_address(sample_order.id, new_address)
        
        assert result['success'] is True
        
        # Verify address updated
        db.session.refresh(sample_order)
        assert sample_order.delivery_address['address_line1'] == '456 New Street'


@pytest.mark.performance
@pytest.mark.order
class TestOrderPerformance:
    """Test order processing performance"""
    
    def test_bulk_order_creation_performance(self, order_service, sample_user, multiple_products, db):
        """Test performance of creating multiple orders"""
        import time
        
        order_data_template = {
            'user_id': sample_user.id,
            'items': [
                {
                    'product_id': multiple_products[0].id,
                    'quantity': 1,
                    'unit_price': multiple_products[0].base_price
                }
            ],
            'delivery_address': {
                'address_line1': '123 Test Street',
                'city': 'Tashkent'
            }
        }
        
        order_service.inventory_service.check_availability.return_value = True
        order_service.inventory_service.reserve_stock.return_value = True
        
        start_time = time.time()
        
        # Create 10 orders
        orders = []
        for i in range(10):
            with patch.object(order_service, '_generate_order_number', return_value=f'ORD-{i}'):
                order = order_service.create_order(order_data_template.copy())
                orders.append(order)
        
        end_time = time.time()
        processing_time = end_time - start_time
        
        # Should create 10 orders in under 5 seconds
        assert processing_time < 5.0
        assert len(orders) == 10
    
    def test_order_calculation_performance(self, order_service, multiple_products):
        """Test performance of order calculations with many items"""
        import time
        
        # Create order with many items
        items = []
        for i in range(50):  # 50 items
            items.append({
                'product_id': multiple_products[0].id,
                'quantity': 1,
                'unit_price': multiple_products[0].base_price
            })
        
        start_time = time.time()
        subtotal = order_service._calculate_subtotal(items)
        end_time = time.time()
        
        calculation_time = end_time - start_time
        
        # Should calculate subtotal in under 1 second
        assert calculation_time < 1.0
        assert subtotal == multiple_products[0].base_price * 50