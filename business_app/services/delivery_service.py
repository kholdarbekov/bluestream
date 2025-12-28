"""
Delivery service for the Water Business Platform
Handles delivery scheduling, route optimization, and tracking
"""
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Tuple
from flask import current_app
import math

from business_app.models.delivery import Delivery, DeliveryRoute, DeliveryTimeSlot
from business_app.models.order import Order
from business_app.models.user import User
from business_app.models.delivery import DeliveryStatusHistory
from business_app.utils.exceptions import ValidationError, NotFoundError, DeliveryError
from business_app.utils.constants import DeliveryStatus, DeliveryType, TASHKENT_COORDINATES, DELIVERY_ZONES
from business_app.utils.helpers import calculate_distance, generate_tracking_code, estimate_delivery_time, get_time_slots
from business_app import db


class DeliveryService:
    """Service for managing deliveries"""
    
    def __init__(self):
        self.default_delivery_fee = current_app.config.get('DEFAULT_DELIVERY_FEE', 5000)
        self.free_delivery_threshold = current_app.config.get('FREE_DELIVERY_THRESHOLD', 50000)
        self.max_delivery_distance = current_app.config.get('DELIVERY_RADIUS_KM', 20)
        self.store_latitude = TASHKENT_COORDINATES['latitude']
        self.store_longitude = TASHKENT_COORDINATES['longitude']
    
    def create_delivery(self, order_id: int, delivery_type: DeliveryType = DeliveryType.STANDARD,
                       scheduled_time_slot: str = None) -> Delivery:
        """
        Create delivery for an order

        Args:
            order_id: Order ID
            delivery_type: Type of delivery
            scheduled_time_slot: Scheduled delivery time slot

        Returns:
            Delivery object

        Raises:
            NotFoundError: If order not found
            ValidationError: If delivery cannot be created
        """
        order = Order.query.get(order_id)
        if not order:
            raise NotFoundError("Order not found")
        
        # Check if delivery already exists
        existing_delivery = Delivery.query.filter_by(order_id=order_id).first()
        if existing_delivery:
            raise ValidationError("Delivery already exists for this order")
        
        # Calculate delivery distance
        distance = calculate_distance(
            self.store_latitude, self.store_longitude,
            order.delivery_address_latitude, order.delivery_address_longitude
        )
        
        # Check if within delivery range
        if distance > self.max_delivery_distance:
            raise DeliveryError(f"Delivery address is outside our delivery range ({self.max_delivery_distance} km)")
        
        # Determine delivery zone
        zone = self._get_delivery_zone(distance)
        
        # Estimate delivery time
        estimated_time = self._calculate_estimated_delivery_time(distance, delivery_type)
        
        # Create delivery record
        delivery = Delivery(
            order_id=order_id,
            status=DeliveryStatus.SCHEDULED,
            distance_km=round(distance, 2),
            estimated_delivery_time=estimated_time,
            scheduled_date=order.delivery_date or datetime.now(),
            scheduled_time_slot=scheduled_time_slot or "09:00-12:00"
        )
        
        db.session.add(delivery)
        db.session.commit()
        
        # Schedule delivery assignment
        self._schedule_delivery_assignment(delivery.id)
        
        return delivery
    
    def assign_delivery_driver(self, delivery_id: int, driver_id: int) -> Delivery:
        """Assign delivery to a driver"""
        delivery = Delivery.query.get(delivery_id)
        if not delivery:
            raise NotFoundError("Delivery not found")
        
        driver = User.query.filter_by(id=driver_id, role='delivery_driver').first()
        if not driver:
            raise NotFoundError("Driver not found")
        
        # Check if driver is available
        if not self._is_driver_available(driver_id):
            raise ValidationError("Driver is not available")
        
        # Assign driver
        delivery.driver_id = driver_id
        delivery.status = DeliveryStatus.ASSIGNED
        delivery.assigned_at = datetime.now(timezone.utc)
        
        db.session.commit()
        
        # Notify driver
        self._notify_driver(delivery)
        
        # Optimize route if driver has multiple deliveries
        self._optimize_driver_route(driver_id)
        
        return delivery
    
    def update_delivery_status(self, delivery_id: int, new_status: DeliveryStatus,
                              driver_id: int = None, notes: str = None,
                              current_location: Tuple[float, float] = None) -> Delivery:
        """Update delivery status"""
        delivery = Delivery.query.get(delivery_id)
        if not delivery:
            raise NotFoundError("Delivery not found")
        
        # Validate status transition
        if not self._is_valid_delivery_status_transition(delivery.status, new_status):
            raise ValidationError(f"Cannot change status from {delivery.status.value} to {new_status.value}")
        
        # Update delivery
        old_status = delivery.status
        delivery.status = new_status
        delivery.updated_at = datetime.now(timezone.utc)
        
        # Update status-specific fields
        self._update_delivery_status_fields(delivery, new_status, current_location)
        
        # Create status history
        self._create_delivery_status_history(delivery_id, old_status, new_status, driver_id, notes)
        
        db.session.commit()
        
        # Handle status-specific actions
        self._handle_delivery_status_change(delivery, new_status)
        
        return delivery
    
    def calculate_delivery_fee(self, latitude: float, longitude: float, order_total: int) -> int:
        """Calculate delivery fee based on location and order total"""
        if order_total >= self.free_delivery_threshold:
            return 0
        
        # distance = calculate_distance(
        #     self.store_latitude, self.store_longitude,
        #     latitude, longitude
        # )
        
        # # Get zone-based fee
        # zone = self._get_delivery_zone(distance)
        # zone_info = DELIVERY_ZONES.get(zone, DELIVERY_ZONES['OUTER'])
        
        # return zone_info['fee']

        # We are offering free delivery for all orders for now
        return 0
    
    def get_available_time_slots(self, date: datetime = None, delivery_type: DeliveryType = DeliveryType.STANDARD) -> List[str]:
        """Get available delivery time slots for a date"""
        if date is None:
            date = datetime.now().date()
        
        # Get base time slots
        time_slots = get_time_slots()
        
        # For express delivery, filter to next few hours
        if delivery_type == DeliveryType.EXPRESS:
            now = datetime.now()
            if date == now.date():
                # Only show slots 2+ hours from now for express
                current_time = now.time()
                time_slots = [slot for slot in time_slots 
                            if self._parse_time_slot(slot)[0] >= (now + timedelta(hours=2)).time()]
        
        # Check capacity for each slot
        available_slots = []
        for slot in time_slots:
            if self._check_slot_capacity(date, slot):
                available_slots.append(slot)
        
        return available_slots
    
    def track_delivery(self, tracking_code: str) -> Dict[str, Any]:
        """Get delivery tracking information"""
        delivery = Delivery.query.filter_by(tracking_code=tracking_code).first()
        if not delivery:
            raise NotFoundError("Delivery not found")
        
        return {
            'tracking_code': delivery.tracking_code,
            'status': delivery.status.value,
            'order_number': delivery.order.order_number,
            'estimated_delivery_time': delivery.estimated_delivery_time.isoformat() if delivery.estimated_delivery_time else None,
            'current_location': {
                'latitude': delivery.current_latitude,
                'longitude': delivery.current_longitude
            } if delivery.current_latitude and delivery.current_longitude else None,
            'delivery_address': {
                'street': delivery.delivery_address_street,
                'city': delivery.delivery_address_city
            },
            'driver': {
                'name': f"{delivery.driver.first_name} {delivery.driver.last_name}",
                'phone': delivery.driver.phone
            } if delivery.driver else None,
            'timeline': [
                {
                    'status': history.new_status.value,
                    'timestamp': history.changed_at.isoformat(),
                    'notes': history.notes
                }
                for history in delivery.status_history
            ]
        }
    
    def get_delivery_metrics(self, start_date: datetime = None, end_date: datetime = None) -> Dict[str, Any]:
        """Get delivery performance metrics"""
        query = Delivery.query
        
        if start_date:
            query = query.filter(Delivery.created_at >= start_date)
        if end_date:
            query = query.filter(Delivery.created_at <= end_date)
        
        deliveries = query.all()
        
        # Calculate metrics
        total_deliveries = len(deliveries)
        completed_deliveries = len([d for d in deliveries if d.status == DeliveryStatus.DELIVERED])
        failed_deliveries = len([d for d in deliveries if d.status == DeliveryStatus.FAILED])
        
        # Average delivery time
        completed_with_times = [d for d in deliveries if d.delivered_at and d.assigned_at]
        avg_delivery_time = None
        if completed_with_times:
            total_time = sum((d.delivered_at - d.assigned_at).total_seconds() for d in completed_with_times)
            avg_delivery_time = total_time / len(completed_with_times) / 60  # in minutes
        
        # On-time delivery rate
        on_time_deliveries = len([d for d in completed_with_times 
                                if d.delivered_at <= d.estimated_delivery_time])
        on_time_rate = (on_time_deliveries / len(completed_with_times)) * 100 if completed_with_times else 0
        
        return {
            'total_deliveries': total_deliveries,
            'completed_deliveries': completed_deliveries,
            'failed_deliveries': failed_deliveries,
            'completion_rate': (completed_deliveries / total_deliveries) * 100 if total_deliveries > 0 else 0,
            'failure_rate': (failed_deliveries / total_deliveries) * 100 if total_deliveries > 0 else 0,
            'average_delivery_time_minutes': round(avg_delivery_time, 2) if avg_delivery_time else None,
            'on_time_delivery_rate': round(on_time_rate, 2),
            'zone_breakdown': self._get_zone_breakdown(deliveries)
        }
    
    def optimize_routes(self, date: datetime = None) -> Dict[str, Any]:
        """Optimize delivery routes for a given date"""
        if date is None:
            date = datetime.now().date()
        
        # Get pending deliveries for the date
        start_of_day = datetime.combine(date, datetime.min.time())
        end_of_day = datetime.combine(date, datetime.max.time())
        
        deliveries = Delivery.query.filter(
            Delivery.status.in_([DeliveryStatus.PENDING, DeliveryStatus.ASSIGNED]),
            Delivery.created_at.between(start_of_day, end_of_day)
        ).all()
        
        if not deliveries:
            return {'message': 'No deliveries to optimize', 'routes': []}
        
        # Group deliveries by zone and create optimized routes
        routes = self._create_optimized_routes(deliveries)
        
        return {
            'date': date.isoformat(),
            'total_deliveries': len(deliveries),
            'routes': routes,
            'optimization_summary': {
                'total_routes': len(routes),
                'total_distance_km': sum(route['total_distance_km'] for route in routes),
                'estimated_total_time_hours': sum(route['estimated_time_hours'] for route in routes)
            }
        }
    
    def complete_delivery(self, delivery_id: int, driver_id: int = None, 
                         proof_photo: str = None, customer_signature: str = None) -> Delivery:
        """Mark delivery as completed"""
        delivery = self.update_delivery_status(
            delivery_id, 
            DeliveryStatus.DELIVERED, 
            driver_id, 
            "Delivery completed successfully"
        )
        
        # Add completion details
        delivery.delivered_at = datetime.now(timezone.utc)
        delivery.proof_of_delivery_photo = proof_photo
        delivery.customer_signature = customer_signature
        
        db.session.commit()
        
        return delivery
    
    def cancel_delivery(self, delivery_id: int, reason: str = None) -> Delivery:
        """Cancel delivery"""
        delivery = Delivery.query.get(delivery_id)
        if not delivery:
            raise NotFoundError("Delivery not found")
        
        if delivery.status in [DeliveryStatus.DELIVERED, DeliveryStatus.FAILED]:
            raise ValidationError("Cannot cancel completed or failed delivery")
        
        delivery.status = DeliveryStatus.FAILED
        delivery.cancelled_at = datetime.now(timezone.utc)
        delivery.cancellation_reason = reason
        
        db.session.commit()
        
        # Notify customer and driver
        self._notify_delivery_cancellation(delivery)
        
        return delivery
    
    # Private helper methods
    def _get_delivery_zone(self, distance_km: float) -> str:
        """Determine delivery zone based on distance"""
        for zone, info in DELIVERY_ZONES.items():
            if distance_km <= info['radius']:
                return zone
        return 'OUTER'
    
    def _calculate_estimated_delivery_time(self, distance_km: float, delivery_type: DeliveryType) -> datetime:
        """Calculate estimated delivery time"""
        base_time = datetime.now(timezone.utc)
        
        if delivery_type == DeliveryType.EXPRESS:
            # Express: 1-2 hours
            estimated_minutes = 60 + (distance_km * 2)
        elif delivery_type == DeliveryType.EMERGENCY:
            # Emergency: 30-60 minutes
            estimated_minutes = 30 + distance_km
        else:
            # Standard: 2-4 hours
            estimated_minutes = 120 + (distance_km * 3)
        
        return base_time + timedelta(minutes=estimated_minutes)
    
    def _is_driver_available(self, driver_id: int) -> bool:
        """Check if driver is available for assignment"""
        # Check active deliveries
        active_deliveries = Delivery.query.filter_by(
            driver_id=driver_id,
            status=DeliveryStatus.IN_TRANSIT
        ).count()
        
        # Allow up to 5 concurrent deliveries per driver
        return active_deliveries < 5
    
    def _is_valid_delivery_status_transition(self, current: DeliveryStatus, new: DeliveryStatus) -> bool:
        """Check if delivery status transition is valid"""
        valid_transitions = {
            DeliveryStatus.PENDING: [DeliveryStatus.ASSIGNED, DeliveryStatus.FAILED],
            DeliveryStatus.ASSIGNED: [DeliveryStatus.PICKED_UP, DeliveryStatus.FAILED],
            DeliveryStatus.PICKED_UP: [DeliveryStatus.IN_TRANSIT, DeliveryStatus.FAILED],
            DeliveryStatus.IN_TRANSIT: [DeliveryStatus.ARRIVED, DeliveryStatus.FAILED],
            DeliveryStatus.ARRIVED: [DeliveryStatus.DELIVERED, DeliveryStatus.FAILED],
            DeliveryStatus.DELIVERED: [],
            DeliveryStatus.FAILED: [],
            DeliveryStatus.RETURNED: []
        }
        
        return new in valid_transitions.get(current, [])
    
    def _update_delivery_status_fields(self, delivery: Delivery, new_status: DeliveryStatus,
                                     current_location: Tuple[float, float] = None):
        """Update status-specific fields"""
        now = datetime.now(timezone.utc)
        
        if new_status == DeliveryStatus.ASSIGNED:
            delivery.assigned_at = now
        elif new_status == DeliveryStatus.PICKED_UP:
            delivery.picked_up_at = now
        elif new_status == DeliveryStatus.IN_TRANSIT:
            delivery.in_transit_at = now
        elif new_status == DeliveryStatus.ARRIVED:
            delivery.arrived_at = now
        elif new_status == DeliveryStatus.DELIVERED:
            delivery.delivered_at = now
        
        # Update current location if provided
        if current_location:
            delivery.current_latitude, delivery.current_longitude = current_location
            delivery.last_location_update = now
    
    def _create_delivery_status_history(self, delivery_id: int, old_status: DeliveryStatus,
                                       new_status: DeliveryStatus, changed_by: int = None, notes: str = None):
        """Create delivery status history record"""
        history = DeliveryStatusHistory(
            delivery_id=delivery_id,
            old_status=old_status,
            new_status=new_status,
            changed_by=changed_by,
            notes=notes,
            changed_at=datetime.now(timezone.utc)
        )
        
        db.session.add(history)
    
    def _handle_delivery_status_change(self, delivery: Delivery, new_status: DeliveryStatus):
        """Handle actions when delivery status changes"""
        # Send notifications
        from ..tasks.notification_tasks import send_delivery_update_task
        send_delivery_update_task.delay(delivery.id, new_status.value)
        
        # Update order status if delivery is completed
        if new_status == DeliveryStatus.DELIVERED:
            from .order_service import OrderService
            order_service = OrderService()
            order_service.update_order_status(delivery.order_id, 'delivered')
    
    def _schedule_delivery_assignment(self, delivery_id: int):
        """Schedule automatic delivery assignment"""
        from ..tasks.delivery_tasks import auto_assign_delivery_task
        # Assign delivery automatically after 5 minutes
        auto_assign_delivery_task.apply_async(args=[delivery_id], countdown=300)
    
    def _notify_driver(self, delivery: Delivery):
        """Notify driver of new delivery assignment"""
        from ..tasks.notification_tasks import notify_driver_assignment_task
        notify_driver_assignment_task.delay(delivery.id)
    
    def _optimize_driver_route(self, driver_id: int):
        """Optimize route for a specific driver"""
        from ..tasks.delivery_tasks import optimize_driver_route_task
        optimize_driver_route_task.delay(driver_id)
    
    def _parse_time_slot(self, time_slot: str) -> Tuple[datetime.time, datetime.time]:
        """Parse time slot string into start and end times"""
        start_str, end_str = time_slot.split('-')
        start_time = datetime.strptime(start_str, '%H:%M').time()
        end_time = datetime.strptime(end_str, '%H:%M').time()
        return start_time, end_time
    
    def _check_slot_capacity(self, date: datetime.date, time_slot: str) -> bool:
        """Check if time slot has available capacity"""
        # Get deliveries scheduled for this slot
        start_of_day = datetime.combine(date, datetime.min.time())
        end_of_day = datetime.combine(date, datetime.max.time())

        slot_deliveries = Delivery.query.filter(
            Delivery.scheduled_time_slot == time_slot,
            Delivery.created_at.between(start_of_day, end_of_day),
            Delivery.status != DeliveryStatus.FAILED
        ).count()

        # Allow up to 20 deliveries per time slot
        return slot_deliveries < 20
    
    def _get_zone_breakdown(self, deliveries: List[Delivery]) -> Dict[str, int]:
        """Get delivery count breakdown by zone"""
        breakdown = {}
        for delivery in deliveries:
            zone = delivery.delivery_zone
            breakdown[zone] = breakdown.get(zone, 0) + 1
        return breakdown
    
    def _create_optimized_routes(self, deliveries: List[Delivery]) -> List[Dict[str, Any]]:
        """Create optimized delivery routes"""
        # Group by zone first
        zone_groups = {}
        for delivery in deliveries:
            zone = delivery.delivery_zone
            if zone not in zone_groups:
                zone_groups[zone] = []
            zone_groups[zone].append(delivery)
        
        routes = []
        for zone, zone_deliveries in zone_groups.items():
            # Simple optimization: sort by proximity
            optimized_order = self._optimize_delivery_order(zone_deliveries)
            
            route = {
                'zone': zone,
                'deliveries': [
                    {
                        'id': d.id,
                        'tracking_code': d.tracking_code,
                        'order_number': d.order.order_number,
                        'address': d.delivery_address_street,
                        'latitude': d.delivery_address_latitude,
                        'longitude': d.delivery_address_longitude,
                        'estimated_time': d.estimated_delivery_time.isoformat()
                    }
                    for d in optimized_order
                ],
                'total_distance_km': self._calculate_route_distance(optimized_order),
                'estimated_time_hours': len(optimized_order) * 0.5  # 30 minutes per delivery
            }
            routes.append(route)
        
        return routes
    
    def _optimize_delivery_order(self, deliveries: List[Delivery]) -> List[Delivery]:
        """Optimize order of deliveries using simple nearest neighbor algorithm"""
        if not deliveries:
            return []
        
        # Start from store location
        current_lat, current_lon = self.store_latitude, self.store_longitude
        remaining = deliveries.copy()
        optimized = []
        
        while remaining:
            # Find nearest delivery
            nearest = min(remaining, key=lambda d: calculate_distance(
                current_lat, current_lon,
                d.delivery_address_latitude, d.delivery_address_longitude
            ))
            
            optimized.append(nearest)
            remaining.remove(nearest)
            current_lat, current_lon = nearest.delivery_address_latitude, nearest.delivery_address_longitude
        
        return optimized
    
    def _calculate_route_distance(self, deliveries: List[Delivery]) -> float:
        """Calculate total distance for a delivery route"""
        if not deliveries:
            return 0
        
        total_distance = 0
        current_lat, current_lon = self.store_latitude, self.store_longitude
        
        for delivery in deliveries:
            distance = calculate_distance(
                current_lat, current_lon,
                delivery.delivery_address_latitude, delivery.delivery_address_longitude
            )
            total_distance += distance
            current_lat, current_lon = delivery.delivery_address_latitude, delivery.delivery_address_longitude
        
        # Add return distance to store
        total_distance += calculate_distance(
            current_lat, current_lon,
            self.store_latitude, self.store_longitude
        )
        
        return round(total_distance, 2)
    
    def _notify_delivery_cancellation(self, delivery: Delivery):
        """Notify about delivery cancellation"""
        from ..tasks.notification_tasks import notify_delivery_cancellation_task
        notify_delivery_cancellation_task.delay(delivery.id)