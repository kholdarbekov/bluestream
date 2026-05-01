from datetime import datetime, timedelta, UTC
from decimal import Decimal
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, Text, ForeignKey, Enum, JSON, Index, Numeric
from sqlalchemy.orm import relationship
import uuid
from business_app import db
from shared.enums import DeliveryStatus
from business_app.models import TimestampMixin
from business_app.models.order import Order


class Delivery(db.Model, TimestampMixin):
    __tablename__ = "deliveries"
    __table_args__ = (
        Index("idx_deliveries_person_status", "delivery_person_id", "status"),
        Index("idx_deliveries_status_scheduled", "status", "scheduled_date"),
    )

    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False, unique=True, index=True)
    delivery_person_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    status = Column(
        Enum(DeliveryStatus, name="delivery_status", values_callable=lambda x: [e.value for e in x]),
        default=DeliveryStatus.SCHEDULED,
        index=True,
    )

    # Scheduling
    scheduled_date = Column(DateTime(timezone=True), nullable=False)
    scheduled_time_slot = Column(String(20), nullable=False)
    estimated_delivery_time = Column(DateTime(timezone=True), nullable=True)
    actual_delivery_time = Column(DateTime(timezone=True), nullable=True)

    # Route and tracking
    route_data = Column(JSON, default={})  # Optimized route information
    tracking_number = Column(String(50), unique=True, nullable=True, index=True)
    distance_km = Column(Float, nullable=True)
    estimated_duration_minutes = Column(Integer, nullable=True)

    # Real-time tracking
    current_location_lat = Column(Float, nullable=True)
    current_location_lng = Column(Float, nullable=True)
    last_location_update = Column(DateTime(timezone=True), nullable=True)

    # Completion details
    delivered_at = Column(DateTime(timezone=True), nullable=True)
    delivery_confirmation_photos = Column(JSON, default=[])
    recipient_signature = Column(String(500), nullable=True)
    delivery_notes = Column(Text, nullable=True)
    customer_rating = Column(Integer, nullable=True)  # 1-5 stars
    customer_feedback = Column(Text, nullable=True)

    # Delivery attempts
    delivery_attempts = Column(Integer, default=0)
    failed_delivery_reason = Column(String(255), nullable=True)

    # Cash on delivery
    cash_collected = Column(Numeric(precision=12, scale=2), nullable=True)

    order = relationship("Order", back_populates="delivery")
    delivery_person = relationship("User", foreign_keys=[delivery_person_id], back_populates="deliveries")

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if not self.tracking_number:
            self.generate_tracking_number()

    def generate_tracking_number(self):
        """Generate unique tracking number"""
        timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M")
        random_suffix = str(uuid.uuid4().hex[:6]).upper()
        self.tracking_number = f"TRK{timestamp}{random_suffix}"

    def update_location(self, lat, lng):
        """Update current delivery location"""
        self.current_location_lat = lat
        self.current_location_lng = lng
        self.last_location_update = datetime.now(UTC)

    def mark_as_delivered(self, photos=None, signature=None, notes=None):
        """Mark delivery as completed"""
        self.status = DeliveryStatus.DELIVERED
        self.delivered_at = datetime.now(UTC)
        self.actual_delivery_time = self.delivered_at

        if photos:
            self.delivery_confirmation_photos = photos
        if signature:
            self.recipient_signature = signature
        if notes:
            self.delivery_notes = notes

    def to_dict(self):
        return {
            "id": self.id,
            "tracking_number": self.tracking_number,
            "status": self.status.value,
            "scheduled_date": self.scheduled_date.isoformat() if self.scheduled_date else None,
            "scheduled_time_slot": self.scheduled_time_slot,
            "estimated_delivery_time": (
                self.estimated_delivery_time.isoformat() if self.estimated_delivery_time else None
            ),
            "actual_delivery_time": self.actual_delivery_time.isoformat() if self.actual_delivery_time else None,
            "current_location_lat": self.current_location_lat,
            "current_location_lng": self.current_location_lng,
            "last_location_update": self.last_location_update.isoformat() if self.last_location_update else None,
            "distance_km": self.distance_km,
            "estimated_duration_minutes": self.estimated_duration_minutes,
            "delivery_attempts": self.delivery_attempts,
            "delivery_person": (
                {
                    "id": self.delivery_person.id,
                    "name": self.delivery_person.full_name,
                    "phone": self.delivery_person.phone,
                }
                if self.delivery_person
                else None
            ),
        }


class DeliveryTimeSlot(db.Model):
    __tablename__ = "delivery_time_slots"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)  # Morning, Afternoon, Evening
    start_time = Column(String(5), nullable=False)  # "09:00"
    end_time = Column(String(5), nullable=False)  # "12:00"
    is_active = Column(Boolean, default=True)
    max_orders = Column(Integer, default=50)  # Maximum orders per slot
    delivery_fee = Column(Numeric(precision=10, scale=2), default=Decimal("0.00"))

    # Availability by day of week (0=Monday, 6=Sunday)
    available_days = Column(JSON, default=[0, 1, 2, 3, 4, 5, 6])

    # Special pricing
    is_premium = Column(Boolean, default=False)  # Premium time slots cost extra
    premium_fee = Column(Numeric(precision=10, scale=2), default=Decimal("0.00"))

    def is_available_on_date(self, target_date):
        """Check if slot is available on given date"""
        if not self.is_active:
            return False

        day_of_week = target_date.weekday()
        return day_of_week in self.available_days

    def get_current_orders_count(self, target_date):
        """Get number of orders already scheduled for this slot on target date"""
        return Order.query.filter(
            Order.delivery_date >= target_date,
            Order.delivery_date < target_date + timedelta(days=1),
            Order.delivery_time_slot == f"{self.start_time}-{self.end_time}",
        ).count()

    def is_available(self, target_date):
        """Check if slot has capacity on target date"""
        if not self.is_available_on_date(target_date):
            return False

        current_orders = self.get_current_orders_count(target_date)
        return current_orders < self.max_orders

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "time_range": f"{self.start_time}-{self.end_time}",
            "is_active": self.is_active,
            "max_orders": self.max_orders,
            "delivery_fee": self.delivery_fee,
            "is_premium": self.is_premium,
            "premium_fee": self.premium_fee,
            "available_days": self.available_days,
        }


class DeliveryRoute(db.Model, TimestampMixin):
    """Optimized delivery routes"""

    __tablename__ = "delivery_routes"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    delivery_person_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    # Route details
    start_location_lat = Column(Float, nullable=False)  # Depot/warehouse location
    start_location_lng = Column(Float, nullable=False)
    route_date = Column(DateTime(timezone=True), nullable=False, index=True)

    # Route optimization data
    optimized_order = Column(JSON, default=[])  # List of order IDs in optimized sequence
    total_distance_km = Column(Float, nullable=True)
    estimated_duration_minutes = Column(Integer, nullable=True)

    # Route status
    status = Column(String(20), default="planned")  # planned, in_progress, completed, cancelled
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    # Performance metrics
    actual_distance_km = Column(Float, nullable=True)
    actual_duration_minutes = Column(Integer, nullable=True)
    deliveries_completed = Column(Integer, default=0)
    deliveries_failed = Column(Integer, default=0)

    # Additional data
    extra_data = Column(JSON, default={})
    notes = Column(Text, nullable=True)

    delivery_person = relationship("User", backref="delivery_routes")

    def get_completion_rate(self):
        """Calculate route completion rate"""
        total_deliveries = self.deliveries_completed + self.deliveries_failed
        if total_deliveries == 0:
            return 0
        return (self.deliveries_completed / total_deliveries) * 100

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "delivery_person_id": self.delivery_person_id,
            "route_date": self.route_date.isoformat() if self.route_date else None,
            "status": self.status,
            "total_distance_km": self.total_distance_km,
            "estimated_duration_minutes": self.estimated_duration_minutes,
            "actual_distance_km": self.actual_distance_km,
            "actual_duration_minutes": self.actual_duration_minutes,
            "deliveries_completed": self.deliveries_completed,
            "deliveries_failed": self.deliveries_failed,
            "completion_rate": self.get_completion_rate(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "delivery_person": (
                {
                    "id": self.delivery_person.id,
                    "name": f"{self.delivery_person.first_name} {self.delivery_person.last_name}",
                    "phone": self.delivery_person.phone,
                }
                if self.delivery_person
                else None
            ),
        }


class DeliveryStatusHistory(db.Model, TimestampMixin):
    """Track delivery status changes"""

    __tablename__ = "delivery_status_history"

    id = Column(Integer, primary_key=True)
    delivery_id = Column(Integer, ForeignKey("deliveries.id"), nullable=False, index=True)
    old_status = Column(
        Enum(DeliveryStatus, name="delivery_status", values_callable=lambda x: [e.value for e in x]), nullable=False
    )
    new_status = Column(
        Enum(DeliveryStatus, name="delivery_status", values_callable=lambda x: [e.value for e in x]), nullable=False
    )
    changed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    changed_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))

    # Location when status changed
    location_lat = Column(Float, nullable=True)
    location_lng = Column(Float, nullable=True)
    location_accuracy = Column(Float, nullable=True)

    # Context
    reason = Column(String(100), nullable=True)
    notes = Column(Text, nullable=True)
    automatic = Column(Boolean, default=False)  # True if changed by system, False if manual

    # Additional metadata
    extra_data = Column(JSON, default={})
    device_info = Column(String(255), nullable=True)
    ip_address = Column(String(45), nullable=True)

    delivery = relationship("Delivery", backref="status_history")
    changed_by_user = relationship("User", foreign_keys=[changed_by])

    def to_dict(self):
        return {
            "id": self.id,
            "delivery_id": self.delivery_id,
            "old_status": self.old_status.value if self.old_status else None,
            "new_status": self.new_status.value if self.new_status else None,
            "changed_by": self.changed_by,
            "changed_at": self.changed_at.isoformat() if self.changed_at else None,
            "location_lat": self.location_lat,
            "location_lng": self.location_lng,
            "reason": self.reason,
            "notes": self.notes,
            "automatic": self.automatic,
            "changed_by_user": (
                {
                    "id": self.changed_by_user.id,
                    "name": f"{self.changed_by_user.first_name} {self.changed_by_user.last_name}",
                    "role": self.changed_by_user.role.value,
                }
                if self.changed_by_user
                else None
            ),
        }


class DeliveryPerson(db.Model, TimestampMixin):
    """Delivery personnel model"""

    __tablename__ = "delivery_persons"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, unique=True, index=True)

    # Personal information
    full_name = Column(String(100), nullable=False)
    phone = Column(String(20), nullable=False)
    email = Column(String(120), nullable=True)

    # Work details
    employee_id = Column(String(50), nullable=True, unique=True)
    hire_date = Column(DateTime(timezone=True), nullable=True)

    # Vehicle information
    vehicle_type = Column(String(50), nullable=True)  # motorcycle, car, truck, bicycle
    vehicle_number = Column(String(20), nullable=True)
    vehicle_capacity_kg = Column(Float, default=0.0)

    # Work schedule
    working_hours_start = Column(String(5), default="09:00")  # HH:MM format
    working_hours_end = Column(String(5), default="18:00")
    working_days = Column(JSON, default=["monday", "tuesday", "wednesday", "thursday", "friday", "saturday"])

    # Location tracking
    current_location_lat = Column(Float, nullable=True)
    current_location_lng = Column(Float, nullable=True)
    last_location_update = Column(DateTime(timezone=True), nullable=True)

    # Status and metrics
    is_active = Column(Boolean, default=True, index=True)
    is_available = Column(Boolean, default=True, index=True)  # Available for new deliveries

    # Capacity and workload
    max_concurrent_deliveries = Column(Integer, default=3)
    current_active_deliveries = Column(Integer, default=0)

    # Performance metrics
    total_deliveries = Column(Integer, default=0)
    successful_deliveries = Column(Integer, default=0)
    average_rating = Column(Float, default=0.0)
    total_distance_km = Column(Float, default=0.0)
    total_cash_collected = Column(Numeric(precision=12, scale=2), default=Decimal("0.00"))

    # Staff bot notification settings (managed by admins only)
    notifications_muted = Column(Boolean, default=False)

    # Emergency contact
    emergency_contact_name = Column(String(100), nullable=True)
    emergency_contact_phone = Column(String(20), nullable=True)

    # Relationships
    user = relationship("User", backref="delivery_person_profile")
    # Note: Deliveries are linked to User directly via delivery_person_id, not through DeliveryPerson

    def __repr__(self):
        return f"<DeliveryPerson {self.full_name}>"

    @property
    def success_rate(self):
        """Calculate delivery success rate"""
        if self.total_deliveries == 0:
            return 0.0
        return round((self.successful_deliveries / self.total_deliveries) * 100, 2)

    @property
    def is_working_now(self):
        """Check if delivery person is currently working"""
        if not self.is_active or not self.is_available:
            return False

        from datetime import datetime, time, UTC

        now = datetime.now(UTC).time()
        start_time = time.fromisoformat(self.working_hours_start)
        end_time = time.fromisoformat(self.working_hours_end)

        # Check if current time is within working hours
        if start_time <= end_time:
            return start_time <= now <= end_time
        else:  # Night shift (crosses midnight)
            return now >= start_time or now <= end_time

    def update_location(self, lat: float, lng: float):
        """Update current location"""
        self.current_location_lat = lat
        self.current_location_lng = lng
        self.last_location_update = datetime.now(UTC)

    def calculate_distance_to(self, dest_lat: float, dest_lng: float) -> float:
        """Calculate distance to destination in kilometers"""
        if not self.current_location_lat or not self.current_location_lng:
            return 0.0

        from math import radians, cos, sin, asin, sqrt

        # Convert to radians
        lat1, lng1, lat2, lng2 = map(
            radians, [self.current_location_lat, self.current_location_lng, dest_lat, dest_lng]
        )

        # Haversine formula
        dlng = lng2 - lng1
        dlat = lat2 - lat1
        a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlng / 2) ** 2
        return 2 * asin(sqrt(a)) * 6371  # Earth's radius in km

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "full_name": self.full_name,
            "phone": self.phone,
            "email": self.email,
            "employee_id": self.employee_id,
            "hire_date": self.hire_date.isoformat() if self.hire_date else None,
            "vehicle_type": self.vehicle_type,
            "vehicle_number": self.vehicle_number,
            "vehicle_capacity_kg": self.vehicle_capacity_kg,
            "working_hours_start": self.working_hours_start,
            "working_hours_end": self.working_hours_end,
            "working_days": self.working_days,
            "current_location": (
                {
                    "lat": self.current_location_lat,
                    "lng": self.current_location_lng,
                    "last_update": self.last_location_update.isoformat() if self.last_location_update else None,
                }
                if self.current_location_lat and self.current_location_lng
                else None
            ),
            "is_active": self.is_active,
            "is_available": self.is_available,
            "is_working_now": self.is_working_now,
            "total_deliveries": self.total_deliveries,
            "successful_deliveries": self.successful_deliveries,
            "success_rate": self.success_rate,
            "average_rating": self.average_rating,
            "total_distance_km": self.total_distance_km,
            "total_cash_collected": float(self.total_cash_collected) if self.total_cash_collected else 0,
            "max_concurrent_deliveries": self.max_concurrent_deliveries,
            "current_active_deliveries": self.current_active_deliveries,
            "notifications_muted": self.notifications_muted,
            "emergency_contact": (
                {"name": self.emergency_contact_name, "phone": self.emergency_contact_phone}
                if self.emergency_contact_name
                else None
            ),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
