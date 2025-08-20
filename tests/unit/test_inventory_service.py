"""
Unit tests for Inventory Service - Critical Business Logic
Tests stock management, reservations, and inventory tracking
"""
import pytest
from decimal import Decimal
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, UTC, timedelta

from business_app.services.inventory_service import InventoryService
from business_app.models.product import Product
from business_app.models.inventory import InventoryMovement, StockReservation
from business_app.utils.constants import ProductCategory, InventoryMovementType
from business_app.utils.exceptions import InsufficientStockError, ValidationError, InventoryError


@pytest.fixture
def inventory_service(mock_notification_service):
    """Create InventoryService instance with mocked dependencies"""
    service = InventoryService()
    service.notification_service = mock_notification_service
    return service


@pytest.fixture
def low_stock_product(db):
    """Create a product with low stock for testing"""
    product = Product(
        name='Low Stock Water 19L',
        category='water',
        size='large',
        volume=Decimal('19.00'),
        base_price=Decimal('15000.00'),
        stock_quantity=5,  # Low stock
        min_stock_level=10,
        max_stock_level=100,
        is_active=True,
        created_at=datetime.now(UTC)
    )
    db.session.add(product)
    db.session.commit()
    return product


@pytest.fixture
def inventory_movements(db, sample_product):
    """Create sample inventory movements for testing"""
    movements = []
    
    # Stock in movement
    movement_in = InventoryMovement(
        product_id=sample_product.id,
        movement_type=InventoryMovementType.STOCK_IN,
        quantity=50,
        unit_cost=Decimal('10000.00'),
        reference_number='SI-001',
        notes='Initial stock',
        created_at=datetime.now(UTC)
    )
    movements.append(movement_in)
    
    # Stock out movement
    movement_out = InventoryMovement(
        product_id=sample_product.id,
        movement_type=InventoryMovementType.STOCK_OUT,
        quantity=10,
        reference_number='SO-001',
        notes='Sale',
        created_at=datetime.now(UTC) + timedelta(hours=1)
    )
    movements.append(movement_out)
    
    for movement in movements:
        db.session.add(movement)
    
    db.session.commit()
    return movements


@pytest.mark.critical
@pytest.mark.inventory
class TestStockManagement:
    """Test basic stock management operations"""
    
    def test_check_stock_availability_sufficient(self, inventory_service, sample_product):
        """Test stock availability check with sufficient stock"""
        result = inventory_service.check_availability(sample_product.id, 10)
        
        assert result is True
    
    def test_check_stock_availability_insufficient(self, inventory_service, sample_product):
        """Test stock availability check with insufficient stock"""
        result = inventory_service.check_availability(sample_product.id, 150)
        
        assert result is False
    
    def test_check_stock_availability_exact(self, inventory_service, sample_product):
        """Test stock availability check with exact quantity"""
        result = inventory_service.check_availability(sample_product.id, sample_product.stock_quantity)
        
        assert result is True
    
    def test_deduct_stock_sufficient(self, inventory_service, sample_product, db):
        """Test stock deduction with sufficient stock"""
        initial_stock = sample_product.stock_quantity
        deduction_quantity = 10
        
        result = inventory_service.deduct_stock(sample_product.id, deduction_quantity, 'Order fulfillment')
        
        assert result['success'] is True
        
        # Verify stock updated
        db.session.refresh(sample_product)
        assert sample_product.stock_quantity == initial_stock - deduction_quantity
    
    def test_deduct_stock_insufficient(self, inventory_service, sample_product):
        """Test stock deduction with insufficient stock"""
        excessive_quantity = sample_product.stock_quantity + 10
        
        with pytest.raises(InsufficientStockError, match="Insufficient stock"):
            inventory_service.deduct_stock(sample_product.id, excessive_quantity, 'Order')
    
    def test_deduct_stock_zero_quantity(self, inventory_service, sample_product):
        """Test stock deduction with zero quantity"""
        with pytest.raises(ValidationError, match="Quantity must be positive"):
            inventory_service.deduct_stock(sample_product.id, 0, 'Invalid')
    
    def test_add_stock(self, inventory_service, sample_product, db):
        """Test adding stock to product"""
        initial_stock = sample_product.stock_quantity
        addition_quantity = 25
        unit_cost = Decimal('9500.00')
        
        result = inventory_service.add_stock(
            sample_product.id,
            addition_quantity,
            unit_cost,
            'Stock replenishment'
        )
        
        assert result['success'] is True
        
        # Verify stock updated
        db.session.refresh(sample_product)
        assert sample_product.stock_quantity == initial_stock + addition_quantity
    
    def test_add_stock_exceeds_maximum(self, inventory_service, sample_product):
        """Test adding stock that exceeds maximum level"""
        excessive_quantity = sample_product.max_stock_level
        
        with pytest.raises(ValidationError, match="Stock would exceed maximum"):
            inventory_service.add_stock(sample_product.id, excessive_quantity, Decimal('10000.00'), 'Excess')
    
    def test_adjust_stock_positive(self, inventory_service, sample_product, db):
        """Test positive stock adjustment"""
        initial_stock = sample_product.stock_quantity
        adjustment = 15
        
        result = inventory_service.adjust_stock(
            sample_product.id,
            adjustment,
            'Inventory count adjustment'
        )
        
        assert result['success'] is True
        
        # Verify stock updated
        db.session.refresh(sample_product)
        assert sample_product.stock_quantity == initial_stock + adjustment
    
    def test_adjust_stock_negative(self, inventory_service, sample_product, db):
        """Test negative stock adjustment"""
        initial_stock = sample_product.stock_quantity
        adjustment = -5
        
        result = inventory_service.adjust_stock(
            sample_product.id,
            adjustment,
            'Damaged goods'
        )
        
        assert result['success'] is True
        
        # Verify stock updated
        db.session.refresh(sample_product)
        assert sample_product.stock_quantity == initial_stock + adjustment


@pytest.mark.critical
@pytest.mark.inventory
class TestStockReservations:
    """Test stock reservation system"""
    
    def test_reserve_stock_sufficient(self, inventory_service, sample_product, db):
        """Test stock reservation with sufficient stock"""
        reserve_quantity = 15
        order_id = 123
        
        result = inventory_service.reserve_stock(sample_product.id, reserve_quantity, order_id)
        
        assert result['success'] is True
        assert 'reservation_id' in result
        
        # Verify reservation created
        reservation = db.session.query(StockReservation).filter_by(
            product_id=sample_product.id,
            order_id=order_id
        ).first()
        
        assert reservation is not None
        assert reservation.quantity == reserve_quantity
        assert reservation.status == 'active'
    
    def test_reserve_stock_insufficient(self, inventory_service, sample_product):
        """Test stock reservation with insufficient stock"""
        excessive_quantity = sample_product.stock_quantity + 10
        
        with pytest.raises(InsufficientStockError, match="Cannot reserve"):
            inventory_service.reserve_stock(sample_product.id, excessive_quantity, 123)
    
    def test_release_stock_reservation(self, inventory_service, sample_product, db):
        """Test releasing stock reservation"""
        # First create a reservation
        reserve_quantity = 10
        order_id = 123
        
        result = inventory_service.reserve_stock(sample_product.id, reserve_quantity, order_id)
        reservation_id = result['reservation_id']
        
        # Release the reservation
        release_result = inventory_service.release_reservation(reservation_id)
        
        assert release_result['success'] is True
        
        # Verify reservation status updated
        reservation = db.session.query(StockReservation).get(reservation_id)
        assert reservation.status == 'released'
    
    def test_confirm_stock_reservation(self, inventory_service, sample_product, db):
        """Test confirming stock reservation (converting to actual stock deduction)"""
        # Create reservation
        reserve_quantity = 10
        order_id = 123
        initial_stock = sample_product.stock_quantity
        
        result = inventory_service.reserve_stock(sample_product.id, reserve_quantity, order_id)
        reservation_id = result['reservation_id']
        
        # Confirm reservation
        confirm_result = inventory_service.confirm_reservation(reservation_id)
        
        assert confirm_result['success'] is True
        
        # Verify stock deducted and reservation confirmed
        db.session.refresh(sample_product)
        assert sample_product.stock_quantity == initial_stock - reserve_quantity
        
        reservation = db.session.query(StockReservation).get(reservation_id)
        assert reservation.status == 'confirmed'
    
    def test_expired_reservations_cleanup(self, inventory_service, sample_product, db):
        """Test cleanup of expired reservations"""
        # Create reservation with past expiry
        past_time = datetime.now(UTC) - timedelta(hours=1)
        
        reservation = StockReservation(
            product_id=sample_product.id,
            order_id=123,
            quantity=10,
            expires_at=past_time,
            status='active'
        )
        db.session.add(reservation)
        db.session.commit()
        
        # Run cleanup
        cleaned_count = inventory_service.cleanup_expired_reservations()
        
        assert cleaned_count > 0
        
        # Verify reservation status updated
        db.session.refresh(reservation)
        assert reservation.status == 'expired'
    
    def test_get_available_stock(self, inventory_service, sample_product, db):
        """Test getting available stock (total - reserved)"""
        # Create active reservation
        reservation = StockReservation(
            product_id=sample_product.id,
            order_id=123,
            quantity=15,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            status='active'
        )
        db.session.add(reservation)
        db.session.commit()
        
        available_stock = inventory_service.get_available_stock(sample_product.id)
        
        expected_available = sample_product.stock_quantity - 15
        assert available_stock == expected_available


@pytest.mark.critical
@pytest.mark.inventory
class TestInventoryMovements:
    """Test inventory movement tracking"""
    
    def test_record_stock_in_movement(self, inventory_service, sample_product, db):
        """Test recording stock in movement"""
        quantity = 50
        unit_cost = Decimal('9800.00')
        reference = 'PO-001'
        
        movement = inventory_service.record_movement(
            product_id=sample_product.id,
            movement_type=InventoryMovementType.STOCK_IN,
            quantity=quantity,
            unit_cost=unit_cost,
            reference_number=reference,
            notes='Purchase order stock in'
        )
        
        assert movement.product_id == sample_product.id
        assert movement.quantity == quantity
        assert movement.unit_cost == unit_cost
        assert movement.reference_number == reference
    
    def test_record_stock_out_movement(self, inventory_service, sample_product, db):
        """Test recording stock out movement"""
        quantity = 10
        reference = 'ORD-001'
        
        movement = inventory_service.record_movement(
            product_id=sample_product.id,
            movement_type=InventoryMovementType.STOCK_OUT,
            quantity=quantity,
            reference_number=reference,
            notes='Order fulfillment'
        )
        
        assert movement.movement_type == InventoryMovementType.STOCK_OUT
        assert movement.quantity == quantity
    
    def test_get_movement_history(self, inventory_service, sample_product, inventory_movements):
        """Test retrieving movement history for product"""
        movements = inventory_service.get_movement_history(sample_product.id)
        
        assert len(movements) >= 2
        assert any(m.movement_type == InventoryMovementType.STOCK_IN for m in movements)
        assert any(m.movement_type == InventoryMovementType.STOCK_OUT for m in movements)
    
    def test_calculate_stock_value(self, inventory_service, sample_product, db):
        """Test calculating current stock value using FIFO/Average cost"""
        # Add some stock movements with different costs
        inventory_service.add_stock(sample_product.id, 20, Decimal('9000.00'), 'Batch 1')
        inventory_service.add_stock(sample_product.id, 30, Decimal('10000.00'), 'Batch 2')
        
        stock_value = inventory_service.calculate_stock_value(sample_product.id)
        
        assert stock_value > 0
        assert isinstance(stock_value, Decimal)


@pytest.mark.critical
@pytest.mark.inventory
class TestLowStockMonitoring:
    """Test low stock monitoring and alerts"""
    
    def test_detect_low_stock(self, inventory_service, low_stock_product):
        """Test detection of low stock products"""
        low_stock_products = inventory_service.get_low_stock_products()
        
        product_ids = [p.id for p in low_stock_products]
        assert low_stock_product.id in product_ids
    
    def test_low_stock_notification(self, inventory_service, low_stock_product):
        """Test low stock notification system"""
        result = inventory_service.check_and_notify_low_stock(low_stock_product.id)
        
        assert result['notification_sent'] is True
        
        # Verify notification service called
        inventory_service.notification_service.send_notification.assert_called()
    
    def test_stock_out_detection(self, inventory_service, sample_product, db):
        """Test detection of out-of-stock products"""
        # Set stock to zero
        sample_product.stock_quantity = 0
        db.session.commit()
        
        out_of_stock = inventory_service.get_out_of_stock_products()
        
        product_ids = [p.id for p in out_of_stock]
        assert sample_product.id in product_ids
    
    def test_reorder_point_calculation(self, inventory_service, sample_product):
        """Test automatic reorder point calculation"""
        # Mock sales velocity data
        daily_sales = 5  # units per day
        lead_time_days = 7
        safety_stock_days = 3
        
        reorder_point = inventory_service.calculate_reorder_point(
            sample_product.id,
            daily_sales,
            lead_time_days,
            safety_stock_days
        )
        
        expected_reorder = daily_sales * (lead_time_days + safety_stock_days)
        assert reorder_point == expected_reorder
    
    def test_automatic_reorder_suggestion(self, inventory_service, low_stock_product):
        """Test automatic reorder quantity suggestion"""
        reorder_suggestion = inventory_service.suggest_reorder_quantity(low_stock_product.id)
        
        assert reorder_suggestion > 0
        assert reorder_suggestion >= low_stock_product.min_stock_level


@pytest.mark.inventory
class TestInventoryReports:
    """Test inventory reporting and analytics"""
    
    def test_generate_stock_report(self, inventory_service, sample_product):
        """Test stock level report generation"""
        report = inventory_service.generate_stock_report()
        
        assert 'products' in report
        assert 'total_value' in report
        assert 'low_stock_count' in report
        assert len(report['products']) > 0
    
    def test_generate_movement_report(self, inventory_service, sample_product, inventory_movements):
        """Test movement report for date range"""
        start_date = datetime.now(UTC) - timedelta(days=1)
        end_date = datetime.now(UTC) + timedelta(days=1)
        
        report = inventory_service.generate_movement_report(start_date, end_date)
        
        assert 'movements' in report
        assert 'summary' in report
        assert len(report['movements']) > 0
    
    def test_calculate_turnover_rate(self, inventory_service, sample_product):
        """Test inventory turnover rate calculation"""
        # Mock sales data for turnover calculation
        with patch.object(inventory_service, '_get_sales_data') as mock_sales:
            mock_sales.return_value = {'total_sold': 100, 'period_days': 30}
            
            turnover_rate = inventory_service.calculate_turnover_rate(sample_product.id)
            
            assert turnover_rate > 0
    
    def test_abc_analysis(self, inventory_service, db):
        """Test ABC analysis of inventory"""
        # Create products with different values/volumes
        products = []
        for i in range(10):
            product = Product(
                name=f'Product {i}',
                category='water',
                base_price=Decimal(f'{1000 * (i + 1)}.00'),
                stock_quantity=50 - i * 3,
                is_active=True
            )
            db.session.add(product)
            products.append(product)
        
        db.session.commit()
        
        abc_analysis = inventory_service.perform_abc_analysis()
        
        assert 'category_A' in abc_analysis
        assert 'category_B' in abc_analysis
        assert 'category_C' in abc_analysis


@pytest.mark.inventory
class TestInventoryValidation:
    """Test inventory validation and business rules"""
    
    def test_negative_stock_prevention(self, inventory_service, sample_product):
        """Test prevention of negative stock"""
        excessive_quantity = sample_product.stock_quantity + 1
        
        with pytest.raises(InsufficientStockError):
            inventory_service.deduct_stock(sample_product.id, excessive_quantity, 'Test')
    
    def test_stock_level_constraints(self, inventory_service, sample_product):
        """Test stock level constraints validation"""
        # Test minimum stock validation
        assert sample_product.stock_quantity >= 0
        
        # Test maximum stock validation
        excessive_stock = sample_product.max_stock_level + 1
        with pytest.raises(ValidationError, match="exceed maximum"):
            inventory_service.add_stock(sample_product.id, excessive_stock, Decimal('10000.00'), 'Test')
    
    def test_concurrent_stock_updates(self, inventory_service, sample_product, db):
        """Test handling of concurrent stock updates"""
        import threading
        import time
        
        results = []
        
        def deduct_stock_worker():
            try:
                result = inventory_service.deduct_stock(sample_product.id, 1, 'Concurrent test')
                results.append(result)
            except Exception as e:
                results.append({'error': str(e)})
        
        # Start multiple threads trying to deduct stock
        threads = []
        for i in range(5):
            thread = threading.Thread(target=deduct_stock_worker)
            threads.append(thread)
            thread.start()
        
        # Wait for all threads
        for thread in threads:
            thread.join()
        
        # Verify no race conditions occurred
        successful_operations = [r for r in results if 'success' in r and r['success']]
        assert len(successful_operations) <= sample_product.stock_quantity


@pytest.mark.performance
@pytest.mark.inventory
class TestInventoryPerformance:
    """Test inventory operations performance"""
    
    def test_stock_check_performance(self, inventory_service, sample_product):
        """Test stock availability check performance"""
        import time
        
        start_time = time.time()
        for _ in range(100):
            inventory_service.check_availability(sample_product.id, 10)
        end_time = time.time()
        
        avg_time = (end_time - start_time) / 100
        assert avg_time < 0.01  # Should check in under 10ms
    
    def test_bulk_stock_update_performance(self, inventory_service, db):
        """Test bulk stock update performance"""
        import time
        
        # Create multiple products
        products = []
        for i in range(50):
            product = Product(
                name=f'Bulk Product {i}',
                category='water',
                base_price=Decimal('15000.00'),
                stock_quantity=100,
                is_active=True
            )
            db.session.add(product)
            products.append(product)
        
        db.session.commit()
        
        # Test bulk update
        updates = [(p.id, 10, 'Bulk test') for p in products]
        
        start_time = time.time()
        inventory_service.bulk_deduct_stock(updates)
        end_time = time.time()
        
        bulk_time = end_time - start_time
        assert bulk_time < 2.0  # Should complete bulk update in under 2 seconds