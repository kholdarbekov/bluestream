"""
Subscription Serializers for the Water Business Platform using Pydantic v2
This file contains Pydantic models for subscription-related data serialization
"""

from datetime import datetime, date
from typing import Dict, Any, Optional, List
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator, ConfigDict
from pydantic.alias_generators import to_camel

from business_app.models.subscription import SubscriptionItem


class SubscriptionItemSchema(BaseModel):
    """Subscription item schema"""

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)

    id: int
    subscription_id: int
    product_id: int
    product_name: str
    product_sku: Optional[str] = None
    quantity: int
    unit_price: Decimal
    total_price: Decimal

    # Product details
    product_image_url: Optional[str] = None
    product_description: Optional[str] = None
    product_weight: Optional[float] = None
    product_volume: Optional[float] = None

    # Item configuration
    delivery_schedule: Optional[str] = None
    special_instructions: Optional[str] = None

    @field_validator("unit_price", "total_price")
    @classmethod
    def validate_prices(cls, v):
        return float(v)


class SubscriptionAddressSchema(BaseModel):
    """Subscription delivery address schema"""

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)

    id: int
    title: str
    full_address: str
    city: str
    district: Optional[str] = None
    postal_code: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    delivery_instructions: Optional[str] = None


class SubscriptionSchema(BaseModel):
    """Main subscription schema"""

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)

    id: int
    user_id: int
    name: str
    description: Optional[str] = None
    status: str

    # Billing configuration
    billing_cycle: str
    billing_amount: Decimal
    discount_percentage: float = Field(default=0.0)

    # Delivery configuration
    delivery_frequency: str
    delivery_day_of_week: Optional[int] = None  # 0=Monday, 6=Sunday
    delivery_day_of_month: Optional[int] = None  # 1-31
    delivery_time_slot_id: Optional[int] = None
    delivery_time_slot: Optional[Dict[str, Any]] = None  # Full time slot details

    # Payment configuration
    payment_method: str
    auto_payment: bool = Field(default=True)
    auto_renew: bool = Field(default=True)

    # Billing tracking
    total_amount_billed: Decimal = Field(default=0)
    failed_billing_attempts: int = Field(default=0)
    last_billing_date: Optional[datetime] = None
    next_billing_date: Optional[datetime] = None

    # Schedule tracking
    last_delivery_date: Optional[date] = None
    next_delivery_date: Optional[date] = None

    # Lifecycle dates
    start_date: datetime
    end_date: Optional[datetime] = None
    pause_start_date: Optional[datetime] = None
    pause_end_date: Optional[datetime] = None

    # Timestamps
    created_at: datetime
    updated_at: Optional[datetime] = None

    # Relationships (optional, loaded when needed)
    subscription_items: List[SubscriptionItemSchema] = Field(default_factory=list)
    delivery_address: Optional[SubscriptionAddressSchema] = None

    @field_validator("billing_amount", "total_amount_billed")
    @classmethod
    def validate_amounts(cls, v):
        return float(v)


class CreateSubscriptionRequest(BaseModel):
    """Create subscription request schema"""

    name: str = Field(..., min_length=3, max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    billing_cycle: str  # Will be validated as SubscriptionFrequency value
    delivery_frequency: str  # Will be validated as SubscriptionFrequency value
    delivery_day_of_week: Optional[int] = Field(None, ge=0, le=6)
    delivery_day_of_month: Optional[int] = Field(None, ge=1, le=31)
    delivery_time_slot_id: Optional[int] = Field(None, gt=0)
    delivery_address_id: int = Field(..., gt=0)
    payment_method: str
    auto_payment: bool = Field(default=True)
    auto_renew: bool = Field(default=True)
    discount_percentage: float = Field(default=0.0, ge=0.0, le=100.0)
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    items: List[Dict[str, Any]] = Field(..., min_length=1)


class UpdateSubscriptionRequest(BaseModel):
    """Update subscription request schema"""

    name: Optional[str] = Field(None, min_length=3, max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    delivery_day_of_week: Optional[int] = Field(None, ge=0, le=6)
    delivery_day_of_month: Optional[int] = Field(None, ge=1, le=31)
    delivery_time_slot_id: Optional[int] = Field(None, gt=0)
    delivery_address_id: Optional[int] = Field(None, gt=0)
    payment_method: Optional[str] = None
    auto_payment: Optional[bool] = None
    auto_renew: Optional[bool] = None


class PauseSubscriptionRequest(BaseModel):
    """Pause subscription request schema"""

    reason: Optional[str] = Field(None, max_length=500)
    resume_date: Optional[datetime] = None


class CancelSubscriptionRequest(BaseModel):
    """Cancel subscription request schema"""

    reason: Optional[str] = Field(None, max_length=500)
    immediate: bool = Field(default=False)


class AddSubscriptionItemRequest(BaseModel):
    """Add subscription item request schema"""

    product_id: int = Field(..., gt=0)
    quantity: int = Field(..., gt=0)
    special_instructions: Optional[str] = Field(None, max_length=200)


class UpdateSubscriptionItemRequest(BaseModel):
    """Update subscription item request schema"""

    quantity: int = Field(..., gt=0)
    special_instructions: Optional[str] = Field(None, max_length=200)


class SubscriptionPreviewRequest(BaseModel):
    """Subscription preview request schema"""

    billing_cycle: str  # SubscriptionFrequency value
    delivery_frequency: str  # SubscriptionFrequency value
    items: List[Dict[str, Any]] = Field(..., min_length=1)
    discount_percentage: float = Field(default=0.0, ge=0.0, le=100.0)


class SubscriptionPreviewResponse(BaseModel):
    """Subscription preview response schema"""

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)

    items_total: Decimal
    discount_amount: Decimal
    billing_amount: Decimal

    # Delivery estimates
    deliveries_per_month: int
    monthly_cost_estimate: Decimal

    # Savings calculation
    regular_price_total: Decimal
    total_savings: Decimal
    savings_percentage: float

    # Item breakdown
    items_breakdown: List[Dict[str, Any]]

    @field_validator(
        "items_total",
        "discount_amount",
        "billing_amount",
        "monthly_cost_estimate",
        "regular_price_total",
        "total_savings",
    )
    @classmethod
    def validate_amounts(cls, v):
        return float(v)


class SubscriptionBillingInfo(BaseModel):
    """Subscription billing information schema"""

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)

    next_billing_date: Optional[datetime] = None
    billing_amount: Decimal
    payment_method: str
    auto_payment_enabled: bool
    failed_attempts: int = Field(default=0)
    last_successful_payment: Optional[datetime] = None
    total_amount_billed: Decimal = Field(default=0)

    # Payment history summary
    successful_payments: int = Field(default=0)
    failed_payments: int = Field(default=0)

    @field_validator("billing_amount", "total_amount_billed")
    @classmethod
    def validate_amounts(cls, v):
        return float(v)


class SubscriptionStatistics(BaseModel):
    """Subscription statistics schema"""

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)

    total_subscriptions: int
    active_subscriptions: int
    paused_subscriptions: int
    cancelled_subscriptions: int

    # Financial metrics
    total_spent: Decimal
    total_savings: Decimal
    average_monthly_spending: Decimal

    # Delivery metrics
    upcoming_deliveries: int
    total_deliveries_completed: int

    # Trend data
    monthly_spending_trend: Dict[str, Decimal]

    @field_validator("total_spent", "total_savings", "average_monthly_spending")
    @classmethod
    def validate_amounts(cls, v):
        return float(v)

    @field_validator("monthly_spending_trend")
    @classmethod
    def validate_monthly_spending(cls, v):
        return {k: float(amount) for k, amount in v.items()}


class SubscriptionLogSchema(BaseModel):
    """Subscription log schema"""

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)

    id: int
    subscription_id: int
    action: str
    details: str
    created_at: datetime
    user_name: Optional[str] = None
    extra_data: Optional[Dict[str, Any]] = None


class SubscriptionTemplate(BaseModel):
    """Subscription template schema"""

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)

    id: str
    name: str
    description: str
    billing_cycle: str
    delivery_frequency: str
    discount_percentage: float
    suggested_items: List[Dict[str, Any]]
    estimated_monthly_cost: Decimal

    @field_validator("estimated_monthly_cost")
    @classmethod
    def validate_cost(cls, v):
        return float(v)


class ChangePaymentMethodRequest(BaseModel):
    """Change payment method request schema"""

    payment_method: str


class SkipDeliveryRequest(BaseModel):
    """Skip delivery request schema"""

    reason: Optional[str] = Field(None, max_length=500)


class SubscriptionResponseSchema(BaseModel):
    """Standard subscription response schema"""

    success: bool
    message: str
    subscription: Optional[SubscriptionSchema] = None
    errors: Optional[List[str]] = None


# Export all schemas for easy importing
__all__ = [
    "SubscriptionSchema",
    "SubscriptionItemSchema",
    "SubscriptionAddressSchema",
    "CreateSubscriptionRequest",
    "UpdateSubscriptionRequest",
    "PauseSubscriptionRequest",
    "CancelSubscriptionRequest",
    "AddSubscriptionItemRequest",
    "UpdateSubscriptionItemRequest",
    "SubscriptionPreviewRequest",
    "SubscriptionPreviewResponse",
    "SubscriptionBillingInfo",
    "SubscriptionStatistics",
    "SubscriptionLogSchema",
    "SubscriptionTemplate",
    "ChangePaymentMethodRequest",
    "SkipDeliveryRequest",
    "SubscriptionResponseSchema",
]


def serialize_subscription(subscription, include_items=False, include_address=False) -> Dict[str, Any]:
    """
    Serialize subscription object to dictionary

    Args:
        subscription: Subscription model instance
        include_items: Include subscription items in serialization
        include_address: Include delivery address information

    Returns:
        Serialized subscription data
    """
    try:
        subscription_data = {
            "id": subscription.id,
            "user_id": subscription.user_id,
            "name": subscription.name,
            "description": getattr(subscription, "description", None),
            "status": subscription.status.value if hasattr(subscription.status, "value") else str(subscription.status),
            "billing_cycle": (
                subscription.billing_cycle.value
                if hasattr(subscription.billing_cycle, "value")
                else str(subscription.billing_cycle)
            ),
            "billing_amount": float(subscription.billing_amount),
            "discount_percentage": getattr(subscription, "discount_percentage", 0.0),
            "delivery_frequency": subscription.delivery_frequency.value,
            "delivery_day_of_week": getattr(subscription, "delivery_day_of_week", None),
            "delivery_day_of_month": getattr(subscription, "delivery_day_of_month", None),
            "delivery_time_slot_id": getattr(subscription, "delivery_time_slot_id", None),
            "delivery_time_slot": (
                subscription.delivery_time_slot.to_dict() if getattr(subscription, "delivery_time_slot", None) else None
            ),
            "payment_method": (
                subscription.payment_method.value
                if hasattr(subscription.payment_method, "value")
                else str(subscription.payment_method)
            ),
            "auto_payment": getattr(subscription, "auto_payment", True),
            "auto_renew": getattr(subscription, "auto_renew", True),
            "total_amount_billed": float(getattr(subscription, "total_amount_billed", 0)),
            "failed_billing_attempts": getattr(subscription, "failed_billing_attempts", 0),
            "last_billing_date": (
                subscription.last_billing_date.isoformat() if getattr(subscription, "last_billing_date", None) else None
            ),
            "next_billing_date": (
                subscription.next_billing_date.isoformat() if getattr(subscription, "next_billing_date", None) else None
            ),
            "last_delivery_date": (
                subscription.last_delivery_date.isoformat()
                if getattr(subscription, "last_delivery_date", None)
                else None
            ),
            "next_delivery_date": (
                subscription.next_delivery_date.isoformat()
                if getattr(subscription, "next_delivery_date", None)
                else None
            ),
            "start_date": subscription.start_date.isoformat() if subscription.start_date else None,
            "end_date": subscription.end_date.isoformat() if getattr(subscription, "end_date", None) else None,
            "pause_start_date": (
                subscription.pause_start_date.isoformat() if getattr(subscription, "pause_start_date", None) else None
            ),
            "pause_end_date": (
                subscription.pause_end_date.isoformat() if getattr(subscription, "pause_end_date", None) else None
            ),
            "created_at": subscription.created_at.isoformat(),
            "updated_at": subscription.updated_at.isoformat() if getattr(subscription, "updated_at", None) else None,
        }

        if include_items and hasattr(subscription, "subscription_items"):
            subscription_data["subscription_items"] = [
                serialize_subscription_item(item) for item in subscription.subscription_items
            ]

        if include_address and hasattr(subscription, "delivery_address") and subscription.delivery_address:
            subscription_data["delivery_address"] = serialize_subscription_address(subscription.delivery_address)

        return subscription_data

    except Exception as e:
        # Fallback serialization
        return {
            "id": subscription.id,
            "user_id": subscription.user_id,
            "name": subscription.name,
            "status": str(subscription.status),
            "billing_amount": float(subscription.billing_amount),
            "created_at": subscription.created_at.isoformat(),
            "error": f"Partial serialization due to: {str(e)}",
        }


def serialize_subscription_item(item: SubscriptionItem) -> Dict[str, Any]:
    """Serialize subscription item object"""
    try:
        return {
            "id": item.id,
            "subscription_id": item.subscription_id,
            "product_id": item.product_id,
            "quantity": item.quantity,
            "unit_price": float(item.unit_price),
            "total_price": float(item.total_price),
            "product": item.product.to_dict(),
        }
    except Exception:
        return {
            "id": item.id,
            "subscription_id": item.subscription_id,
            "product_id": item.product_id,
            "quantity": item.quantity,
            "unit_price": float(item.unit_price),
            "total_price": float(item.total_price),
        }


def serialize_subscription_address(address) -> Dict[str, Any]:
    """Serialize subscription delivery address"""
    try:
        return {
            "id": address.id,
            "title": address.title,
            "full_address": address.full_address,
            "city": address.city,
            "district": getattr(address, "district", None),
            "postal_code": getattr(address, "postal_code", None),
            "latitude": getattr(address, "latitude", None),
            "longitude": getattr(address, "longitude", None),
            "delivery_instructions": getattr(address, "delivery_instructions", None),
        }
    except Exception:
        return {"id": address.id, "title": address.title, "full_address": address.full_address, "city": address.city}


def serialize_subscription_billing_info(billing_info: Dict[str, Any]) -> Dict[str, Any]:
    """Serialize subscription billing information"""
    return {
        "next_billing_date": billing_info.get("next_billing_date"),
        "billing_amount": float(billing_info.get("billing_amount", 0)),
        "payment_method": billing_info.get("payment_method", ""),
        "auto_payment_enabled": billing_info.get("auto_payment_enabled", True),
        "failed_attempts": billing_info.get("failed_attempts", 0),
        "last_successful_payment": billing_info.get("last_successful_payment"),
        "total_amount_billed": float(billing_info.get("total_amount_billed", 0)),
        "successful_payments": billing_info.get("successful_payments", 0),
        "failed_payments": billing_info.get("failed_payments", 0),
    }


def serialize_subscription_statistics(stats_data: Dict[str, Any]) -> Dict[str, Any]:
    """Serialize subscription statistics"""
    return {
        "total_subscriptions": stats_data.get("total_subscriptions", 0),
        "active_subscriptions": stats_data.get("active_subscriptions", 0),
        "paused_subscriptions": stats_data.get("paused_subscriptions", 0),
        "cancelled_subscriptions": stats_data.get("cancelled_subscriptions", 0),
        "total_spent": float(stats_data.get("total_spent", 0)),
        "total_savings": float(stats_data.get("total_savings", 0)),
        "average_monthly_spending": float(stats_data.get("average_monthly_spending", 0)),
        "upcoming_deliveries": stats_data.get("upcoming_deliveries", 0),
        "total_deliveries_completed": stats_data.get("total_deliveries_completed", 0),
        "monthly_spending_trend": {k: float(v) for k, v in stats_data.get("monthly_spending_trend", {}).items()},
    }


def serialize_subscription_preview(preview_data: Dict[str, Any]) -> Dict[str, Any]:
    """Serialize subscription preview data"""
    return {
        "items_total": float(preview_data.get("items_total", 0)),
        "discount_amount": float(preview_data.get("discount_amount", 0)),
        "billing_amount": float(preview_data.get("billing_amount", 0)),
        "deliveries_per_month": preview_data.get("deliveries_per_month", 0),
        "monthly_cost_estimate": float(preview_data.get("monthly_cost_estimate", 0)),
        "regular_price_total": float(preview_data.get("regular_price_total", 0)),
        "total_savings": float(preview_data.get("total_savings", 0)),
        "savings_percentage": preview_data.get("savings_percentage", 0.0),
        "items_breakdown": preview_data.get("items_breakdown", []),
    }


def serialize_subscription_log(log) -> Dict[str, Any]:
    """Serialize subscription log entry"""
    try:
        return {
            "id": log.id,
            "subscription_id": log.subscription_id,
            "action": log.action,
            "details": log.details,
            "created_at": log.created_at.isoformat(),
            "user_name": getattr(log.user, "full_name", None) if hasattr(log, "user") and log.user else None,
            "extra_data": getattr(log, "extra_data", None),
        }
    except Exception:
        return {"id": log.id, "action": log.action, "details": log.details, "created_at": log.created_at.isoformat()}
