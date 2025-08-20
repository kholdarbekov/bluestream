"""
Unit tests for Delivery Service - Critical Business Logic
Tests delivery scheduling, routing, fee calculation, and tracking
"""
import pytest
from decimal import Decimal
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, UTC, timedelta

from business_app.services.delivery_service import DeliveryService
from business_app.models.delivery import Delivery, DeliveryRoute, TimeSlot
from business_app.models.order import Order
from business_app.models.user import User
from business_app.utils.constants import DeliveryStatus, OrderStatus, UserRole
from business_app.utils.exceptions import DeliveryError, ValidationError, SchedulingError


@pytest.fixture
def delivery_service(mock_notification_service):
    """Create DeliveryService instance with mocked dependencies"""
    service = DeliveryService()
    service.notification_service = mock_notification_service
    return service


@pytest.fixture
def delivery_driver_user(db):
    """Create a delivery driver user for testing"""
    driver = User(
        email='driver@example.com',
        phone='+998901234569',
        password_hash='$2b$12$test.hash',
        first_name='Test',
        last_name='Driver',
        role=UserRole.DELIVERY_DRIVER,
        is_verified=True,
        is_active=True,
        created_at=datetime.now(UTC)
    )
    db.session.add(driver)
    db.session.commit()
    return driver


@pytest.fixture
def time_slots(db):
    """Create time slots for testing"""
    slots = []
    
    # Morning slot
    morning_slot = TimeSlot(
        name='Morning Delivery',
        start_time='09:00',
        end_time='12:00',
        is_active=True,
        max_orders=20,
        delivery_fee=Decimal('3000.00')
    )
    slots.append(morning_slot)
    
    # Afternoon slot
    afternoon_slot = TimeSlot(
        name='Afternoon Delivery',
        start_time='14:00',
        end_time='17:00',
        is_active=True,
        max_orders=25,
        delivery_fee=Decimal('3000.00')
    )
    slots.append(afternoon_slot)
    
    # Evening slot
    evening_slot = TimeSlot(
        name='Evening Delivery',
        start_time='18:00',
        end_time='21:00',
        is_active=True,
        max_orders=15,
        delivery_fee=Decimal('4000.00')  # Higher fee for evening
    )
    slots.append(evening_slot)
    
    for slot in slots:
        db.session.add(slot)
    
    db.session.commit()
    return slots


@pytest.fixture
def sample_delivery(db, sample_order, delivery_driver_user, time_slots):
    """Create a sample delivery for testing"""
    delivery = Delivery(
        order_id=sample_order.id,
        driver_id=delivery_driver_user.id,
        time_slot_id=time_slots[0].id,
        delivery_address={
            'address_line1': '123 Test Street',
            'city': 'Tashkent',
            'latitude': 41.2995,
            'longitude': 69.2401
        },
        status=DeliveryStatus.SCHEDULED,
        scheduled_date=datetime.now(UTC).date(),
        estimated_duration=30,  # minutes
        delivery_fee=Decimal('3000.00'),
        created_at=datetime.now(UTC)
    )
    db.session.add(delivery)
    db.session.commit()
    return delivery


@pytest.mark.critical
@pytest.mark.delivery
class TestDeliveryScheduling:
    """Test delivery scheduling logic"""
    
    def test_schedule_delivery_available_slot(self, delivery_service, sample_order, time_slots, db):
        """Test scheduling delivery in available time slot"""
        delivery_data = {
            'order_id': sample_order.id,
            'time_slot_id': time_slots[0].id,
            'scheduled_date': datetime.now(UTC).date() + timedelta(days=1),
            'delivery_address': {
                'address_line1': '123 Test Street',
                'city': 'Tashkent',
                'latitude': 41.2995,
                'longitude': 69.2401
            }
        }
        
        result = delivery_service.schedule_delivery(delivery_data)
        
        assert result['success'] is True
        assert 'delivery_id' in result
        
        # Verify delivery created
        delivery = db.session.query(Delivery).get(result['delivery_id'])
        assert delivery.order_id == sample_order.id
        assert delivery.status == DeliveryStatus.SCHEDULED
    
    def test_schedule_delivery_full_slot(self, delivery_service, sample_order, time_slots, db):
        """Test scheduling delivery when time slot is full"""
        # Fill up the time slot
        for i in range(time_slots[0].max_orders):
            order = Order(
                user_id=sample_order.user_id,
                order_number=f'ORD-{i}',
                status=OrderStatus.CONFIRMED,
                total_amount=Decimal('18000.00')
            )
            db.session.add(order)
            db.session.flush()
            
            delivery = Delivery(
                order_id=order.id,
                time_slot_id=time_slots[0].id,
                scheduled_date=datetime.now(UTC).date() + timedelta(days=1),
                status=DeliveryStatus.SCHEDULED
            )
            db.session.add(delivery)
        
        db.session.commit()
        
        # Try to schedule one more delivery
        delivery_data = {
            'order_id': sample_order.id,
            'time_slot_id': time_slots[0].id,
            'scheduled_date': datetime.now(UTC).date() + timedelta(days=1),
            'delivery_address': {'address_line1': '123 Test Street', 'city': 'Tashkent'}
        }
        
        with pytest.raises(SchedulingError, match="Time slot is full"):
            delivery_service.schedule_delivery(delivery_data)
    
    def test_schedule_delivery_past_date(self, delivery_service, sample_order, time_slots):
        """Test scheduling delivery for past date"""
        delivery_data = {
            'order_id': sample_order.id,
            'time_slot_id': time_slots[0].id,
            'scheduled_date': datetime.now(UTC).date() - timedelta(days=1),  # Past date
            'delivery_address': {'address_line1': '123 Test Street', 'city': 'Tashkent'}
        }
        
        with pytest.raises(ValidationError, match="Cannot schedule delivery in the past"):
            delivery_service.schedule_delivery(delivery_data)
    
    def test_get_available_time_slots(self, delivery_service, time_slots, db):
        """Test getting available time slots for a date"""
        target_date = datetime.now(UTC).date() + timedelta(days=1)
        
        available_slots = delivery_service.get_available_time_slots(target_date)
        
        assert len(available_slots) > 0
        assert all(slot['available_capacity'] > 0 for slot in available_slots)
    
    def test_reschedule_delivery(self, delivery_service, sample_delivery, time_slots, db):
        """Test rescheduling existing delivery"""
        new_date = datetime.now(UTC).date() + timedelta(days=2)
        new_slot_id = time_slots[1].id
        
        result = delivery_service.reschedule_delivery(
            sample_delivery.id,
            new_date,
            new_slot_id
        )
        
        assert result['success'] is True
        
        # Verify delivery updated
        db.session.refresh(sample_delivery)
        assert sample_delivery.scheduled_date == new_date
        assert sample_delivery.time_slot_id == new_slot_id


@pytest.mark.critical
@pytest.mark.delivery
class TestDeliveryRouting:
    """Test delivery routing and optimization"""
    
    def test_calculate_distance(self, delivery_service):
        """Test distance calculation between coordinates"""
        # Tashkent city center coordinates
        origin = {'latitude': 41.2995, 'longitude': 69.2401}
        destination = {'latitude': 41.3200, 'longitude': 69.2800}
        
        distance = delivery_service.calculate_distance(origin, destination)
        
        assert distance > 0
        assert isinstance(distance, float)
    
    def test_calculate_delivery_fee_by_zone(self, delivery_service):
        """Test delivery fee calculation based on delivery zone"""
        # Central zone (free delivery area)
        central_address = {'latitude': 41.2995, 'longitude': 69.2401}
        central_fee = delivery_service.calculate_delivery_fee(central_address, Decimal('50000.00'))
        assert central_fee == Decimal('0.00')
        
        # Inner zone
        with patch.object(delivery_service, 'calculate_distance', return_value=8.0):
            inner_address = {'latitude': 41.3200, 'longitude': 69.2800}
            inner_fee = delivery_service.calculate_delivery_fee(inner_address, Decimal('30000.00'))
            assert inner_fee == Decimal('3000.00')
        
        # Outer zone
        with patch.object(delivery_service, 'calculate_distance', return_value=15.0):
            outer_address = {'latitude': 41.4000, 'longitude': 69.3500}
            outer_fee = delivery_service.calculate_delivery_fee(outer_address, Decimal('30000.00'))
            assert outer_fee == Decimal('5000.00')
    
    def test_free_delivery_threshold(self, delivery_service):
        """Test free delivery for orders above threshold"""
        address = {'latitude': 41.3200, 'longitude': 69.2800}
        
        # Order above free delivery threshold
        high_value_fee = delivery_service.calculate_delivery_fee(address, Decimal('60000.00'))
        assert high_value_fee == Decimal('0.00')
        
        # Order below threshold
        with patch.object(delivery_service, 'calculate_distance', return_value=8.0):
            low_value_fee = delivery_service.calculate_delivery_fee(address, Decimal('30000.00'))
            assert low_value_fee > Decimal('0.00')
    
    def test_optimize_delivery_route(self, delivery_service, db):
        """Test delivery route optimization"""
        # Create multiple deliveries for same date and slot
        deliveries = []
        addresses = [
            {'latitude': 41.2995, 'longitude': 69.2401},
            {'latitude': 41.3100, 'longitude': 69.2500},
            {'latitude': 41.3200, 'longitude': 69.2600},
            {'latitude': 41.3300, 'longitude': 69.2700}
        ]
        
        for i, address in enumerate(addresses):
            delivery = Delivery(
                order_id=i + 1,
                delivery_address=address,
                status=DeliveryStatus.SCHEDULED,
                scheduled_date=datetime.now(UTC).date() + timedelta(days=1)
            )
            deliveries.append(delivery)
            db.session.add(delivery)
        
        db.session.commit()
        
        # Optimize route
        optimized_route = delivery_service.optimize_route(deliveries)
        
        assert 'route' in optimized_route
        assert 'total_distance' in optimized_route
        assert 'estimated_duration' in optimized_route
        assert len(optimized_route['route']) == len(deliveries)
    
    def test_estimate_delivery_time(self, delivery_service):
        """Test delivery time estimation"""
        address = {'latitude': 41.3200, 'longitude': 69.2800}
        
        with patch.object(delivery_service, 'calculate_distance', return_value=10.0):
            estimated_time = delivery_service.estimate_delivery_time(address)
            
            assert estimated_time > 0
            assert estimated_time <= 120  # Should be reasonable (under 2 hours)


@pytest.mark.critical
@pytest.mark.delivery
class TestDeliveryTracking:
    """Test delivery tracking and status management"""
    
    def test_assign_driver(self, delivery_service, sample_delivery, delivery_driver_user, db):
        """Test assigning driver to delivery"""
        result = delivery_service.assign_driver(sample_delivery.id, delivery_driver_user.id)
        
        assert result['success'] is True
        
        # Verify driver assigned
        db.session.refresh(sample_delivery)
        assert sample_delivery.driver_id == delivery_driver_user.id
    
    def test_start_delivery(self, delivery_service, sample_delivery, db):
        """Test starting delivery"""
        # Assign driver first
        sample_delivery.driver_id = delivery_driver_user.id
        db.session.commit()
        
        result = delivery_service.start_delivery(sample_delivery.id)
        
        assert result['success'] is True
        
        # Verify status updated
        db.session.refresh(sample_delivery)
        assert sample_delivery.status == DeliveryStatus.IN_TRANSIT
        assert sample_delivery.started_at is not None
    
    def test_complete_delivery(self, delivery_service, sample_delivery, db):
        """Test completing delivery"""
        # Set delivery as in transit
        sample_delivery.status = DeliveryStatus.IN_TRANSIT
        sample_delivery.started_at = datetime.now(UTC)
        db.session.commit()
        
        completion_data = {
            'delivery_notes': 'Delivered successfully',
            'customer_signature': 'signature_data',
            'photo_proof': 'photo_data'
        }
        
        result = delivery_service.complete_delivery(sample_delivery.id, completion_data)
        
        assert result['success'] is True
        
        # Verify status updated
        db.session.refresh(sample_delivery)
        assert sample_delivery.status == DeliveryStatus.DELIVERED
        assert sample_delivery.completed_at is not None
        assert sample_delivery.delivery_notes == 'Delivered successfully'
    
    def test_fail_delivery(self, delivery_service, sample_delivery, db):
        """Test failing delivery with reason"""
        sample_delivery.status = DeliveryStatus.IN_TRANSIT
        db.session.commit()
        
        failure_reason = 'Customer not available'
        
        result = delivery_service.fail_delivery(sample_delivery.id, failure_reason)
        
        assert result['success'] is True
        
        # Verify status updated
        db.session.refresh(sample_delivery)
        assert sample_delivery.status == DeliveryStatus.FAILED
        assert sample_delivery.failure_reason == failure_reason
    
    def test_track_delivery_location(self, delivery_service, sample_delivery, db):
        """Test updating delivery location tracking"""
        location_data = {
            'latitude': 41.3100,
            'longitude': 69.2500,
            'timestamp': datetime.now(UTC),
            'speed': 15.5,
            'heading': 180
        }
        
        result = delivery_service.update_delivery_location(sample_delivery.id, location_data)
        
        assert result['success'] is True
        
        # Verify location updated
        db.session.refresh(sample_delivery)
        assert sample_delivery.current_location is not None
    
    def test_get_delivery_status(self, delivery_service, sample_delivery):
        """Test getting delivery status with tracking info"""
        status_info = delivery_service.get_delivery_status(sample_delivery.id)
        
        assert 'status' in status_info
        assert 'estimated_arrival' in status_info
        assert 'current_location' in status_info
        assert status_info['status'] == sample_delivery.status


@pytest.mark.critical
@pytest.mark.delivery
class TestDeliveryValidation:
    """Test delivery validation and business rules"""
    
    def test_validate_delivery_address(self, delivery_service):
        """Test delivery address validation"""
        # Valid address
        valid_address = {
            'address_line1': '123 Test Street',
            'city': 'Tashkent',
            'latitude': 41.2995,
            'longitude': 69.2401
        }
        assert delivery_service._validate_delivery_address(valid_address) is True
        
        # Missing required fields
        invalid_address = {'address_line1': '123 Test Street'}
        with pytest.raises(ValidationError, match="Missing required address fields"):
            delivery_service._validate_delivery_address(invalid_address)
        
        # Invalid coordinates
        invalid_coords = {
            'address_line1': '123 Test Street',
            'city': 'Tashkent',
            'latitude': 200,  # Invalid latitude
            'longitude': 69.2401
        }
        with pytest.raises(ValidationError, match="Invalid coordinates"):
            delivery_service._validate_delivery_address(invalid_coords)
    
    def test_validate_delivery_area(self, delivery_service):
        """Test delivery area coverage validation"""
        # Inside delivery area
        inside_address = {'latitude': 41.2995, 'longitude': 69.2401}
        assert delivery_service._is_within_delivery_area(inside_address) is True
        
        # Outside delivery area
        outside_address = {'latitude': 50.0000, 'longitude': 80.0000}
        assert delivery_service._is_within_delivery_area(outside_address) is False
    
    def test_validate_time_slot_availability(self, delivery_service, time_slots):
        """Test time slot availability validation"""
        target_date = datetime.now(UTC).date() + timedelta(days=1)
        
        # Available slot
        is_available = delivery_service._is_time_slot_available(
            time_slots[0].id,
            target_date
        )
        assert is_available is True
        
        # Past date
        past_date = datetime.now(UTC).date() - timedelta(days=1)
        with pytest.raises(ValidationError):
            delivery_service._is_time_slot_available(time_slots[0].id, past_date)
    
    def test_delivery_capacity_limits(self, delivery_service, time_slots, db):
        """Test delivery capacity enforcement"""
        target_date = datetime.now(UTC).date() + timedelta(days=1)
        
        # Get current capacity
        current_count = delivery_service._get_slot_delivery_count(
            time_slots[0].id,
            target_date
        )
        
        assert current_count >= 0
        assert current_count <= time_slots[0].max_orders


@pytest.mark.delivery
class TestDeliveryReports:
    """Test delivery reporting and analytics"""
    
    def test_generate_delivery_performance_report(self, delivery_service, sample_delivery):
        """Test delivery performance report generation"""
        start_date = datetime.now(UTC).date() - timedelta(days=7)
        end_date = datetime.now(UTC).date()
        
        report = delivery_service.generate_performance_report(start_date, end_date)
        
        assert 'total_deliveries' in report
        assert 'successful_deliveries' in report
        assert 'failed_deliveries' in report
        assert 'average_delivery_time' in report
        assert 'on_time_percentage' in report
    
    def test_driver_performance_metrics(self, delivery_service, delivery_driver_user):
        """Test individual driver performance metrics"""
        metrics = delivery_service.get_driver_performance(delivery_driver_user.id)
        
        assert 'total_deliveries' in metrics
        assert 'success_rate' in metrics
        assert 'average_rating' in metrics
        assert 'on_time_percentage' in metrics
    
    def test_delivery_zone_analysis(self, delivery_service):
        """Test delivery zone performance analysis"""
        analysis = delivery_service.analyze_delivery_zones()
        
        assert 'zones' in analysis
        assert len(analysis['zones']) > 0
        
        for zone in analysis['zones']:
            assert 'zone_name' in zone
            assert 'delivery_count' in zone
            assert 'average_time' in zone
            assert 'success_rate' in zone


@pytest.mark.delivery
class TestDeliveryNotifications:
    """Test delivery notification system"""
    
    def test_delivery_scheduled_notification(self, delivery_service, sample_delivery):
        """Test notification when delivery is scheduled"""
        delivery_service.send_delivery_scheduled_notification(sample_delivery.id)
        
        # Verify notification sent
        delivery_service.notification_service.send_notification.assert_called()
    
    def test_delivery_dispatched_notification(self, delivery_service, sample_delivery):
        """Test notification when delivery is dispatched"""
        delivery_service.send_delivery_dispatched_notification(sample_delivery.id)
        
        delivery_service.notification_service.send_notification.assert_called()
    
    def test_delivery_completed_notification(self, delivery_service, sample_delivery):
        """Test notification when delivery is completed"""
        delivery_service.send_delivery_completed_notification(sample_delivery.id)
        
        delivery_service.notification_service.send_notification.assert_called()
    
    def test_delivery_failed_notification(self, delivery_service, sample_delivery):
        """Test notification when delivery fails"""
        failure_reason = 'Customer not available'
        
        delivery_service.send_delivery_failed_notification(sample_delivery.id, failure_reason)
        
        delivery_service.notification_service.send_notification.assert_called()


@pytest.mark.performance
@pytest.mark.delivery
class TestDeliveryPerformance:
    """Test delivery service performance"""
    
    def test_route_optimization_performance(self, delivery_service, db):
        """Test route optimization performance with many deliveries"""
        import time
        
        # Create many deliveries
        deliveries = []
        for i in range(20):
            delivery = Delivery(
                order_id=i + 1,
                delivery_address={
                    'latitude': 41.2995 + (i * 0.01),
                    'longitude': 69.2401 + (i * 0.01)
                },
                status=DeliveryStatus.SCHEDULED
            )
            deliveries.append(delivery)
        
        start_time = time.time()
        optimized_route = delivery_service.optimize_route(deliveries)
        end_time = time.time()
        
        optimization_time = end_time - start_time
        assert optimization_time < 5.0  # Should optimize in under 5 seconds
    
    def test_distance_calculation_performance(self, delivery_service):
        """Test distance calculation performance"""
        import time
        
        origin = {'latitude': 41.2995, 'longitude': 69.2401}
        destination = {'latitude': 41.3200, 'longitude': 69.2800}
        
        start_time = time.time()
        for _ in range(100):
            delivery_service.calculate_distance(origin, destination)
        end_time = time.time()
        
        avg_time = (end_time - start_time) / 100
        assert avg_time < 0.01  # Should calculate in under 10ms
    
    def test_delivery_scheduling_performance(self, delivery_service, time_slots, db):
        """Test delivery scheduling performance with concurrent requests"""
        import threading
        import time
        
        results = []
        
        def schedule_delivery_worker(order_id):
            try:
                delivery_data = {
                    'order_id': order_id,
                    'time_slot_id': time_slots[0].id,
                    'scheduled_date': datetime.now(UTC).date() + timedelta(days=1),
                    'delivery_address': {
                        'address_line1': '123 Test Street',
                        'city': 'Tashkent',
                        'latitude': 41.2995,
                        'longitude': 69.2401
                    }
                }
                result = delivery_service.schedule_delivery(delivery_data)
                results.append(result)
            except Exception as e:
                results.append({'error': str(e)})
        
        # Create orders for concurrent scheduling
        orders = []
        for i in range(10):
            order = Order(
                user_id=1,
                order_number=f'CONC-{i}',
                status=OrderStatus.CONFIRMED,
                total_amount=Decimal('18000.00')
            )
            db.session.add(order)
            orders.append(order)
        
        db.session.commit()
        
        # Schedule deliveries concurrently
        threads = []
        for order in orders:
            thread = threading.Thread(target=schedule_delivery_worker, args=(order.id,))
            threads.append(thread)
            thread.start()
        
        # Wait for all threads
        for thread in threads:
            thread.join()
        
        # Verify no race conditions
        successful_schedules = [r for r in results if 'success' in r and r['success']]
        assert len(successful_schedules) <= time_slots[0].max_orders