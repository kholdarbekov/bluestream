"""
Delivery Serializers for the Water Business Platform using Pydantic v2
This file contains Pydantic models for delivery-related data serialization
"""

from datetime import datetime, timedelta, UTC
from typing import Dict, Any, Optional, List
from enum import Enum

from pydantic import BaseModel, Field, ConfigDict
from pydantic.alias_generators import to_camel

from business_app.models.user import UserAddress
from business_app.serializers.types import MoneyFloat


class DeliveryStatus(str, Enum):
    CREATED = "created"
    ASSIGNED = "assigned"
    PICKED_UP = "picked_up"
    IN_TRANSIT = "in_transit"
    DELIVERED = "delivered"
    FAILED = "failed"
    CANCELLED = "cancelled"


class VehicleType(str, Enum):
    BICYCLE = "bicycle"
    MOTORCYCLE = "motorcycle"
    CAR = "car"
    VAN = "van"
    TRUCK = "truck"


class DeliveryAddressSchema(BaseModel):
    """Delivery address schema"""

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)

    id: int
    title: Optional[str] = None
    full_address: str
    city: str
    district: Optional[str] = None
    postal_code: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    phone: Optional[str] = None
    contact_name: Optional[str] = None


class DeliveryPersonInfoSchema(BaseModel):
    """Delivery person information schema"""

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)

    id: int
    name: str
    phone: str
    vehicle_type: VehicleType
    vehicle_number: Optional[str] = None
    photo_url: Optional[str] = None
    rating: Optional[float] = None


class OrderInfoSchema(BaseModel):
    """Order information for delivery"""

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)

    order_number: str
    total_amount: MoneyFloat
    item_count: int


class CurrentLocationSchema(BaseModel):
    """Current location schema"""

    latitude: float
    longitude: float
    last_update: Optional[datetime] = None
    accuracy: Optional[float] = None
    is_real_time: bool = Field(default=False)


class StatusHistorySchema(BaseModel):
    """Status history entry schema"""

    status: str
    timestamp: Optional[datetime] = None
    description: str


class CustomerFeedbackSchema(BaseModel):
    """Customer feedback schema"""

    rating: Optional[int] = Field(None, ge=1, le=5)
    comment: Optional[str] = None
    delivery_photo: Optional[str] = None


class AdminFieldsSchema(BaseModel):
    """Admin-specific delivery fields"""

    delivery_cost: MoneyFloat = Field(default=0)
    driver_commission: MoneyFloat = Field(default=0)
    route_optimization_score: Optional[float] = None
    estimated_distance: Optional[float] = None
    actual_distance: Optional[float] = None
    failed_delivery_reason: Optional[str] = None
    internal_notes: Optional[str] = None


class TimeSlotSchema(BaseModel):
    """Delivery time slot schema"""

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)

    id: int
    name: str
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    delivery_fee: MoneyFloat = Field(default=0)
    premium_fee: MoneyFloat = Field(default=0)
    is_premium: bool = Field(default=False)
    is_express: bool = Field(default=False)
    is_active: bool = Field(default=True)
    sort_order: int = Field(default=0)
    description: Optional[str] = None
    is_available: bool = Field(default=True)
    capacity: Optional[Dict[str, Any]] = None


class DeliverySchema(BaseModel):
    """Main delivery schema for API responses"""

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)

    id: int
    tracking_number: str
    order_id: Optional[int] = None
    status: DeliveryStatus
    delivery_date: Optional[datetime] = None
    time_slot: Optional[TimeSlotSchema] = None
    estimated_delivery_time: Optional[datetime] = None
    actual_delivery_time: Optional[datetime] = None
    delivery_attempts: int = Field(default=0)
    delivery_fee: MoneyFloat = Field(default=0)
    special_instructions: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    # Related objects
    delivery_address: Optional[DeliveryAddressSchema] = None
    delivery_person: Optional[DeliveryPersonInfoSchema] = None
    order: Optional[OrderInfoSchema] = None
    current_location: Optional[CurrentLocationSchema] = None
    status_history: List[StatusHistorySchema] = Field(default_factory=list)
    estimated_arrival: Optional[datetime] = None
    customer_feedback: Optional[CustomerFeedbackSchema] = None
    admin_fields: Optional[AdminFieldsSchema] = None


class DeliveryListSchema(BaseModel):
    """Schema for delivery list responses"""

    deliveries: List[DeliverySchema]
    total: int
    page: int
    per_page: int
    pages: int


class DeliveryPersonStatisticsSchema(BaseModel):
    """Delivery person statistics schema"""

    total_deliveries: int = Field(default=0)
    successful_deliveries: int = Field(default=0)
    success_rate: float = Field(default=0.0)
    average_delivery_time: Optional[int] = None  # in minutes
    on_time_percentage: float = Field(default=0.0)
    customer_satisfaction: float = Field(default=0.0)


class DeliveryPersonSchema(BaseModel):
    """Delivery person schema"""

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)

    id: int
    full_name: str
    phone: str
    vehicle_type: VehicleType
    vehicle_number: Optional[str] = None
    is_active: bool = Field(default=True)
    is_available: bool = Field(default=True)
    photo_url: Optional[str] = None
    rating: Dict[str, float] = Field(default_factory=dict)
    verification_status: str = Field(default="verified")
    created_at: Optional[datetime] = None
    current_location: Optional[CurrentLocationSchema] = None
    statistics: Optional[DeliveryPersonStatisticsSchema] = None
    admin_fields: Optional[Dict[str, Any]] = None


class DeliveryPersonListSchema(BaseModel):
    """Schema for delivery person list responses"""

    delivery_personnel: List[DeliveryPersonSchema]
    total: int
    page: int
    per_page: int
    pages: int


class DeliveryStopSchema(BaseModel):
    """Delivery route stop schema"""

    delivery_id: int
    stop_number: int
    address: str
    estimated_arrival: Optional[datetime] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    special_instructions: Optional[str] = None


class DeliveryRouteSchema(BaseModel):
    """Delivery route schema"""

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)

    route_id: Optional[str] = None
    delivery_person_id: Optional[int] = None
    total_deliveries: int = Field(default=0)
    estimated_duration: Optional[int] = None  # in minutes
    estimated_distance: Optional[float] = None  # in km
    optimization_score: Optional[float] = None
    route_polyline: Optional[str] = None
    delivery_stops: List[DeliveryStopSchema] = Field(default_factory=list)
    traffic_conditions: str = Field(default="normal")
    weather_conditions: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[datetime] = None
    status: str = Field(default="planned")


class TrackingLocationSchema(BaseModel):
    """Tracking location schema"""

    latitude: Optional[float] = None
    longitude: Optional[float] = None
    address: Optional[str] = None
    last_update: Optional[datetime] = None


class TrackingProgressSchema(BaseModel):
    """Tracking progress schema"""

    percentage: float = Field(default=0.0, ge=0.0, le=100.0)
    distance_remaining: Optional[float] = None
    time_remaining: Optional[int] = None  # in minutes


class TrackingMapDataSchema(BaseModel):
    """Tracking map data schema"""

    route_polyline: Optional[str] = None
    start_location: Optional[TrackingLocationSchema] = None
    end_location: Optional[TrackingLocationSchema] = None
    waypoints: List[TrackingLocationSchema] = Field(default_factory=list)


class DeliveryTrackingSchema(BaseModel):
    """Delivery tracking schema"""

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)

    delivery_id: int
    tracking_number: str
    current_status: DeliveryStatus
    current_location: TrackingLocationSchema
    estimated_arrival: Optional[datetime] = None
    delivery_person: Optional[DeliveryPersonInfoSchema] = None
    progress: TrackingProgressSchema
    timeline: List[StatusHistorySchema] = Field(default_factory=list)
    map_data: TrackingMapDataSchema


class DeliveryFeedbackSchema(BaseModel):
    """Delivery feedback schema"""

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)

    delivery_id: int
    customer_rating: Optional[int] = Field(None, ge=1, le=5)
    delivery_rating: Optional[int] = Field(None, ge=1, le=5)
    driver_rating: Optional[int] = Field(None, ge=1, le=5)
    feedback_comment: Optional[str] = None
    delivery_issues: List[str] = Field(default_factory=list)
    would_recommend: Optional[bool] = None
    delivery_speed_rating: Optional[int] = Field(None, ge=1, le=5)
    communication_rating: Optional[int] = Field(None, ge=1, le=5)
    professionalism_rating: Optional[int] = Field(None, ge=1, le=5)
    suggestions: Optional[str] = None
    created_at: Optional[datetime] = None


class CreateDeliveryRequest(BaseModel):
    """Create delivery request schema"""

    order_id: int
    delivery_address_id: int
    delivery_date: datetime
    time_slot_id: Optional[int] = None
    special_instructions: Optional[str] = Field(None, max_length=500)
    priority_level: str = Field(default="normal")  # normal, high, urgent


class UpdateDeliveryRequest(BaseModel):
    """Update delivery request schema"""

    status: Optional[DeliveryStatus] = None
    delivery_person_id: Optional[int] = None
    estimated_delivery_time: Optional[datetime] = None
    special_instructions: Optional[str] = Field(None, max_length=500)
    internal_notes: Optional[str] = Field(None, max_length=1000)


class AssignDeliveryRequest(BaseModel):
    """Assign delivery request schema"""

    delivery_ids: List[int] = Field(..., min_items=1, max_items=50)
    delivery_person_id: int
    estimated_completion_time: Optional[datetime] = None


class DeliveryResponseSchema(BaseModel):
    """Standard delivery response schema"""

    success: bool
    message: str
    delivery: Optional[DeliverySchema] = None
    tracking_info: Optional[DeliveryTrackingSchema] = None
    errors: Optional[List[str]] = None


# Export all schemas for easy importing
__all__ = [
    "DeliverySchema",
    "DeliveryListSchema",
    "DeliveryPersonSchema",
    "DeliveryPersonListSchema",
    "TimeSlotSchema",
    "DeliveryRouteSchema",
    "DeliveryTrackingSchema",
    "DeliveryFeedbackSchema",
    "CreateDeliveryRequest",
    "UpdateDeliveryRequest",
    "AssignDeliveryRequest",
    "DeliveryResponseSchema",
    "DeliveryStatus",
    "VehicleType",
]


def serialize_delivery(delivery, include_sensitive: bool = False, user_view: bool = True) -> Dict[str, Any]:
    """
    Serialize a delivery object to dictionary using Pydantic

    Args:
        delivery: Delivery model instance
        include_sensitive: Whether to include sensitive admin information
        user_view: Whether this is for customer view

    Returns:
        Serialized delivery data
    """
    try:
        data = {
            "id": delivery.id,
            "tracking_number": delivery.tracking_number,
            "order_id": delivery.order_id,
            "status": delivery.status.value if delivery.status else None,
            "delivery_date": delivery.delivery_date.isoformat() if delivery.delivery_date else None,
            "estimated_delivery_time": (
                delivery.estimated_delivery_time.isoformat() if delivery.estimated_delivery_time else None
            ),
            "actual_delivery_time": (
                delivery.actual_delivery_time.isoformat() if delivery.actual_delivery_time else None
            ),
            "delivery_attempts": delivery.delivery_attempts or 0,
            "delivery_fee": float(delivery.delivery_fee) if delivery.delivery_fee else 0.0,
            "special_instructions": delivery.special_instructions,
            "created_at": delivery.created_at.isoformat() if delivery.created_at else None,
            "updated_at": delivery.updated_at.isoformat() if delivery.updated_at else None,
        }

        # Add delivery address information
        if delivery.delivery_address:
            data["delivery_address"] = serialize_delivery_address(delivery.delivery_address)

        # Add delivery person information (for customer view)
        if delivery.delivery_person and user_view:
            data["delivery_person"] = serialize_delivery_person_info(delivery.delivery_person)

        # Add order information
        if delivery.order:
            data["order"] = {
                "order_number": delivery.order.order_number,
                "total_amount": float(delivery.order.total_amount),
                "item_count": len(delivery.order.order_items) if delivery.order.order_items else 0,
            }

        # Add time slot information
        if delivery.time_slot:
            data["time_slot"] = serialize_time_slot(delivery.time_slot)

        # Add real-time location data (if available and customer's order)
        if user_view and hasattr(delivery, "current_location_lat") and delivery.current_location_lat:
            data["current_location"] = {
                "latitude": delivery.current_location_lat,
                "longitude": delivery.current_location_lng,
                "last_update": delivery.last_location_update.isoformat() if delivery.last_location_update else None,
                "accuracy": getattr(delivery, "location_accuracy", None),
                "is_real_time": is_location_recent(delivery),
            }

        # Add delivery timeline/status history
        data["status_history"] = get_status_history(delivery)

        # Add estimated arrival time
        if delivery.status and delivery.status.value in ["assigned", "picked_up", "in_transit"]:
            data["estimated_arrival"] = calculate_estimated_arrival(delivery)

        # Add customer feedback if delivery is completed
        if delivery.status and delivery.status.value == "delivered":
            data["customer_feedback"] = {
                "rating": getattr(delivery, "customer_rating", None),
                "comment": getattr(delivery, "customer_feedback", None),
                "delivery_photo": getattr(delivery, "delivery_photo_url", None),
            }

        # Add admin/driver specific information
        if include_sensitive:
            data["admin_fields"] = {
                "delivery_cost": float(getattr(delivery, "delivery_cost", 0)),
                "driver_commission": float(getattr(delivery, "driver_commission", 0)),
                "route_optimization_score": getattr(delivery, "route_score", None),
                "estimated_distance": getattr(delivery, "estimated_distance", None),
                "actual_distance": getattr(delivery, "actual_distance", None),
                "failed_delivery_reason": getattr(delivery, "failed_delivery_reason", None),
                "internal_notes": getattr(delivery, "internal_notes", None),
            }

        return data

    except Exception:
        # Fallback to basic serialization
        return {
            "id": delivery.id,
            "tracking_number": delivery.tracking_number,
            "order_id": delivery.order_id,
            "status": delivery.status.value if delivery.status else None,
            "created_at": delivery.created_at.isoformat() if delivery.created_at else None,
        }


def serialize_delivery_list(
    deliveries: List, include_sensitive: bool = False, user_view: bool = True
) -> List[Dict[str, Any]]:
    """Serialize a list of deliveries"""
    return [serialize_delivery(delivery, include_sensitive, user_view) for delivery in deliveries]


def serialize_delivery_person(person, include_sensitive: bool = False, include_stats: bool = False) -> Dict[str, Any]:
    """
    Serialize a delivery person object to dictionary

    Args:
        person: DeliveryPerson model instance
        include_sensitive: Whether to include sensitive information
        include_stats: Whether to include statistics

    Returns:
        Serialized delivery person data
    """
    try:
        data = {
            "id": person.id,
            "full_name": person.full_name,
            "phone": person.phone if include_sensitive else mask_phone(person.phone),
            "vehicle_type": person.vehicle_type.value if person.vehicle_type else None,
            "vehicle_number": person.vehicle_number,
            "is_active": person.is_active,
            "is_available": getattr(person, "is_available", True),
            "photo_url": getattr(person, "photo_url", None),
            "rating": {
                "average": float(getattr(person, "average_rating", 0)),
                "count": getattr(person, "rating_count", 0),
            },
            "verification_status": getattr(person, "verification_status", "verified"),
            "created_at": person.created_at.isoformat() if person.created_at else None,
        }

        # Add current location (admin only or for active deliveries)
        if include_sensitive and hasattr(person, "current_location_lat"):
            data["current_location"] = {
                "latitude": person.current_location_lat,
                "longitude": person.current_location_lng,
                "last_update": getattr(person, "last_location_update", None),
                "accuracy_m": getattr(person, "location_accuracy_m", None),
            }

        # Add delivery statistics
        if include_stats:
            data["statistics"] = {
                "total_deliveries": getattr(person, "total_deliveries", 0),
                "successful_deliveries": getattr(person, "successful_deliveries", 0),
                "success_rate": calculate_success_rate(person),
                "average_delivery_time": getattr(person, "avg_delivery_time", None),
                "on_time_percentage": getattr(person, "on_time_percentage", 0),
                "customer_satisfaction": float(getattr(person, "avg_customer_rating", 0)),
            }

        # Add sensitive admin information
        if include_sensitive:
            data["admin_fields"] = {
                "employee_id": getattr(person, "employee_id", None),
                "hire_date": getattr(person, "hire_date", None),
                "license_number": getattr(person, "license_number", None),
                "emergency_contact": getattr(person, "emergency_contact", None),
                "monthly_earnings": getattr(person, "monthly_earnings", 0),
                "working_hours": getattr(person, "working_hours", {}),
                "assigned_zones": getattr(person, "assigned_zones", []),
            }

        return data

    except Exception:
        # Fallback to basic serialization
        return {
            "id": person.id,
            "full_name": person.full_name,
            "phone": mask_phone(person.phone),
            "vehicle_type": person.vehicle_type.value if person.vehicle_type else None,
            "is_active": person.is_active,
        }


def serialize_time_slot(time_slot, include_capacity: bool = False) -> Dict[str, Any]:
    """Serialize delivery time slot"""
    try:
        data = {
            "id": time_slot.id,
            "name": time_slot.name,
            "start_time": str(time_slot.start_time) if time_slot.start_time else None,
            "end_time": str(time_slot.end_time) if time_slot.end_time else None,
            "delivery_fee": float(time_slot.delivery_fee) if time_slot.delivery_fee else 0.0,
            "premium_fee": float(time_slot.premium_fee) if time_slot.premium_fee else 0.0,
            "is_premium": time_slot.is_premium,
            "is_express": getattr(time_slot, "is_express", False),
            "is_active": time_slot.is_active,
            "sort_order": getattr(time_slot, "sort_order", 0),
            "description": getattr(time_slot, "description", None),
            "is_available": check_time_slot_availability(time_slot),
        }

        # Add capacity information if requested (admin view)
        if include_capacity:
            data["capacity"] = {
                "max_orders": time_slot.max_orders,
                "current_orders": getattr(time_slot, "current_orders_count", 0),
                "available_capacity": time_slot.max_orders - getattr(time_slot, "current_orders_count", 0),
                "utilization_percentage": calculate_time_slot_utilization(time_slot),
            }

        return data

    except Exception:
        return {"id": time_slot.id, "name": time_slot.name, "is_active": time_slot.is_active, "is_available": True}


# Helper functions
def serialize_delivery_address(address: UserAddress) -> Dict[str, Any]:
    """Serialize delivery address"""
    return {
        "id": address.id,
        "title": address.title,
        "full_address": address.full_address,
        "city": address.city,
        "district": getattr(address, "district", None),
        "postal_code": address.postal_code,
        "latitude": address.latitude,
        "longitude": address.longitude,
    }


def serialize_delivery_person_info(person) -> Dict[str, Any]:
    """Serialize delivery person info for customer view"""
    return {
        "id": person.id,
        "name": person.full_name,
        "phone": person.phone,
        "vehicle_type": person.vehicle_type.value if person.vehicle_type else None,
        "vehicle_number": person.vehicle_number,
        "photo_url": getattr(person, "photo_url", None),
        "rating": getattr(person, "average_rating", None),
    }


def is_location_recent(delivery) -> bool:
    """Check if location data is recent (within last 5 minutes)"""
    if not hasattr(delivery, "last_location_update") or not delivery.last_location_update:
        return False

    time_diff = datetime.now(UTC) - delivery.last_location_update
    return time_diff.total_seconds() < 300  # 5 minutes


def get_status_history(delivery) -> List[Dict[str, Any]]:
    """Get delivery status history"""
    # This would typically come from a status history table
    # For now, return a basic timeline based on current status
    history = [
        {
            "status": "created",
            "timestamp": delivery.created_at.isoformat() if delivery.created_at else None,
            "description": "Delivery scheduled",
        }
    ]

    current_status = delivery.status.value if delivery.status else "created"

    if current_status in ["assigned", "picked_up", "in_transit", "delivered", "failed", "cancelled"]:
        history.append(
            {
                "status": "assigned",
                "timestamp": (
                    getattr(delivery, "assigned_at", delivery.updated_at).isoformat()
                    if hasattr(delivery, "assigned_at")
                    else None
                ),
                "description": "Assigned to delivery person",
            }
        )

    if current_status in ["picked_up", "in_transit", "delivered", "failed"]:
        history.append(
            {
                "status": "picked_up",
                "timestamp": getattr(delivery, "picked_up_at", None),
                "description": "Items picked up for delivery",
            }
        )

    if current_status in ["in_transit", "delivered", "failed"]:
        history.append(
            {
                "status": "in_transit",
                "timestamp": getattr(delivery, "in_transit_at", None),
                "description": "On the way to delivery location",
            }
        )

    if current_status == "delivered":
        history.append(
            {
                "status": "delivered",
                "timestamp": delivery.actual_delivery_time.isoformat() if delivery.actual_delivery_time else None,
                "description": "Successfully delivered",
            }
        )
    elif current_status == "failed":
        history.append(
            {
                "status": "failed",
                "timestamp": (
                    getattr(delivery, "failed_at", delivery.updated_at).isoformat()
                    if hasattr(delivery, "failed_at")
                    else None
                ),
                "description": f'Delivery failed: {getattr(delivery, "failed_delivery_reason", "Unknown reason")}',
            }
        )
    elif current_status == "cancelled":
        history.append(
            {
                "status": "cancelled",
                "timestamp": delivery.updated_at.isoformat() if delivery.updated_at else None,
                "description": getattr(delivery, "delivery_notes", None)
                or "Delivery cancelled because the order was cancelled",
            }
        )

    return history


def calculate_estimated_arrival(delivery) -> Optional[str]:
    """Calculate estimated arrival time based on current location and traffic"""
    # This would integrate with maps service for real-time calculation
    # For now, return a simple estimate
    if delivery.estimated_delivery_time:
        return delivery.estimated_delivery_time.isoformat()

    # Fallback: add 30-60 minutes from now
    estimated = datetime.now(UTC) + timedelta(minutes=45)
    return estimated.isoformat()


def mask_phone(phone: str) -> str:
    """Mask phone number for customer view"""
    if not phone or len(phone) < 4:
        return "****"

    return f"{phone[:3]}****{phone[-2:]}"


def calculate_success_rate(person) -> float:
    """Calculate delivery success rate"""
    total = getattr(person, "total_deliveries", 0)
    successful = getattr(person, "successful_deliveries", 0)

    if total == 0:
        return 0.0

    return round((successful / total) * 100, 1)


def check_time_slot_availability(time_slot) -> bool:
    """Check if time slot is available"""
    if not time_slot.is_active:
        return False

    current_orders = getattr(time_slot, "current_orders_count", 0)
    return current_orders < time_slot.max_orders


def calculate_time_slot_utilization(time_slot) -> float:
    """Calculate time slot utilization percentage"""
    if not time_slot.max_orders:
        return 0.0

    current_orders = getattr(time_slot, "current_orders_count", 0)
    return round((current_orders / time_slot.max_orders) * 100, 1)
