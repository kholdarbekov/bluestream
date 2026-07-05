"""
Staff Serializers for the Water Business Platform using Pydantic v2
Request/response schemas for staff API endpoints.
"""

from datetime import datetime
from typing import Dict, Any, Optional, List

from pydantic import BaseModel, Field, field_validator, ConfigDict
from pydantic.alias_generators import to_camel


# --- Request Schemas ---


class StaffLoginRequest(BaseModel):
    """Staff login request"""

    telegram_id: str = Field(..., min_length=1)
    invite_token: Optional[str] = None


class UpdateDeliveryStatusRequest(BaseModel):
    """Update delivery status request"""

    status: str
    metadata: Optional[Dict[str, Any]] = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        allowed = ["picked_up", "in_transit", "arrived", "delivered", "failed"]
        if v not in allowed:
            raise ValueError(f"Status must be one of: {allowed}")
        return v


class UpdateLocationRequest(BaseModel):
    """Update delivery location request"""

    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)


class CreateClientRequest(BaseModel):
    """Create client user request (operator)"""

    phone: str = Field(..., min_length=9, max_length=20)
    first_name: str = Field(..., min_length=1, max_length=100)
    last_name: Optional[str] = Field(None, max_length=100)
    preferred_language: str = Field(default="uz")


class CreatePhoneOrderRequest(BaseModel):
    """Create phone order request (operator)"""

    client_id: int
    items: List[Dict[str, Any]] = Field(..., min_length=1)
    delivery_address_id: int
    payment_method: Optional[str] = None
    delivery_notes: Optional[str] = None
    delivery_fee: Optional[float] = 0


class AddClientAddressRequest(BaseModel):
    """Add address for client (operator)"""

    title: str = Field(..., min_length=1, max_length=100)
    full_address: str = Field(..., min_length=1, max_length=255)
    city: str = Field(default="Tashkent")
    district: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    delivery_notes: Optional[str] = None


# --- Response Schemas ---


class StaffUserResponse(BaseModel):
    """Staff user info in responses"""

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)

    id: int
    first_name: str
    last_name: Optional[str] = None
    phone: Optional[str] = None
    role: str
    staff_roles: List[str] = []
    preferred_language: str = "en"
    delivery_person_id: Optional[int] = None


class StaffLoginResponse(BaseModel):
    """Staff login response"""

    user: StaffUserResponse
    access_token: str
    refresh_token: str
    expires_in: int


class DeliveryPoolItemResponse(BaseModel):
    """Single item in the delivery order pool"""

    model_config = ConfigDict(from_attributes=True)

    delivery_id: int
    order_id: Optional[int] = None
    order_number: str
    status: Optional[str] = None
    delivery_status: Optional[str] = None
    customer_name: str = ""
    customer_phone: str = ""
    district: str = ""
    address: str = ""
    time_slot: str = ""
    total_amount: float = 0
    payment_method: str = ""
    item_count: int = 0
    items: List[Dict[str, Any]] = []
    delivery_notes: str = ""
    delivery_person_id: Optional[int] = None
    delivery_person_name: str = ""
    created_at: Optional[datetime] = None


class ActiveDeliveryResponse(BaseModel):
    """Active delivery response"""

    model_config = ConfigDict(from_attributes=True)

    delivery_id: int
    order_number: str
    status: str
    customer_name: str = ""
    customer_phone: str = ""
    address: str = ""
    district: str = ""
    total_amount: float = 0
    payment_method: str = ""
    item_count: int = 0
    items: List[Dict[str, Any]] = []
    delivery_notes: str = ""
    current_location_lat: Optional[float] = None
    current_location_lng: Optional[float] = None


class DeliveryStatsResponse(BaseModel):
    """Delivery statistics response"""

    period: str
    total_deliveries: int = 0
    delivered: int = 0
    failed: int = 0
    success_rate: float = 0
    total_cash_collected: float = 0
    avg_delivery_time_minutes: Optional[float] = None
    avg_rating: Optional[float] = None


class ClientUserResponse(BaseModel):
    """Client user response (for operators)"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    first_name: str
    last_name: Optional[str] = None
    phone: Optional[str] = None
    address_count: int = 0
    order_count: int = 0


class StaffOverviewResponse(BaseModel):
    """Staff dashboard overview"""

    orders_today: int = 0
    pending_orders: int = 0
    preparing_orders: int = 0
    active_deliveries: int = 0
    unassigned_deliveries: int = 0
    deliveries_completed_today: int = 0
    active_drivers: int = 0
