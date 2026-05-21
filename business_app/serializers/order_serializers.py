"""
Order Serializers for the Water Business Platform using Pydantic v2
This file contains Pydantic models for order-related data serialization
"""

from datetime import datetime, date
from typing import Dict, Any, Optional, List
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator, ConfigDict
from pydantic.alias_generators import to_camel

from business_app.utils.payment_projection import get_payment_projection
from business_app.models.order import Order, OrderItem


class OrderItemSchema(BaseModel):
    """Order item schema"""

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)

    id: int
    product_id: int
    product_name: str
    product_sku: str
    quantity: int
    unit_price: Decimal
    total_price: Decimal
    special_instructions: Optional[str] = None

    # Product details
    product_image_url: Optional[str] = None
    product_weight: Optional[float] = None
    product_volume: Optional[float] = None

    @field_validator("unit_price", "total_price")
    @classmethod
    def validate_prices(cls, v):
        return float(v)


class OrderDeliverySchema(BaseModel):
    """Order delivery information schema"""

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)

    id: int
    tracking_number: str
    status: str
    estimated_delivery_time: Optional[datetime] = None
    actual_delivery_time: Optional[datetime] = None
    delivery_attempts: int = Field(default=0)
    failed_delivery_reason: Optional[str] = None
    customer_rating: Optional[int] = None
    customer_feedback: Optional[str] = None

    # Location tracking
    current_location_lat: Optional[float] = None
    current_location_lng: Optional[float] = None
    last_location_update: Optional[datetime] = None

    # Delivery person info
    delivery_person_name: Optional[str] = None
    delivery_person_phone: Optional[str] = None
    delivery_person_vehicle: Optional[str] = None


class OrderAddressSchema(BaseModel):
    """Order delivery address schema"""

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)

    id: int
    title: str
    full_address: str
    city: str
    district: Optional[str] = None
    postal_code: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    delivery_notes: Optional[str] = None


class OrderPaymentSchema(BaseModel):
    """Order payment information schema"""

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)

    id: int
    payment_method: str
    payment_status: str
    amount: Decimal
    currency: str = Field(default="UZS")
    transaction_id: Optional[str] = None
    payment_provider: Optional[str] = None
    paid_at: Optional[datetime] = None
    amount_collected: Decimal = Field(default=0)
    outstanding_amount: Decimal = Field(default=0)
    last_collected_at: Optional[datetime] = None
    collection_events_count: int = Field(default=0)

    @field_validator("amount", "amount_collected", "outstanding_amount")
    @classmethod
    def validate_amount(cls, v):
        return float(v)


class OrderSchema(BaseModel):
    """Main order schema"""

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)

    id: int
    order_number: str
    user_id: int
    status: str

    # Amounts
    subtotal_amount: Decimal
    tax_amount: Decimal = Field(default=0)
    delivery_fee: Decimal = Field(default=0)
    discount_amount: Decimal = Field(default=0)
    loyalty_discount: Decimal = Field(default=0)
    total_amount: Decimal

    # Order details
    is_urgent: bool = Field(default=False)
    order_source: str = Field(default="web")
    special_instructions: Optional[str] = None

    # Delivery information
    delivery_date: Optional[date] = None
    delivery_time_slot: Optional[str] = None
    delivery_notes: Optional[str] = None

    # Promotional info
    promo_code_used: Optional[str] = None
    loyalty_points_used: int = Field(default=0)

    # Timestamps
    created_at: datetime
    updated_at: Optional[datetime] = None
    confirmed_at: Optional[datetime] = None
    delivered_at: Optional[datetime] = None

    # Relationships (optional, loaded when needed)
    order_items: List[OrderItemSchema] = Field(default_factory=list)
    delivery_info: Optional[OrderDeliverySchema] = None
    delivery_address: Optional[OrderAddressSchema] = None
    payment_info: Optional[OrderPaymentSchema] = None

    @field_validator(
        "subtotal_amount", "tax_amount", "delivery_fee", "discount_amount", "loyalty_discount", "total_amount"
    )
    @classmethod
    def validate_amounts(cls, v):
        return float(v)


class CreateOrderRequest(BaseModel):
    """Create order request schema"""

    items: List[Dict[str, Any]] = Field(..., min_length=1)
    delivery_address_id: Optional[int] = None
    delivery_date: Optional[date] = None
    delivery_time_slot: Optional[str] = None
    delivery_notes: Optional[str] = None
    is_urgent: bool = Field(default=False)
    payment_method: Optional[str] = None
    loyalty_points_used: int = Field(default=0, ge=0)
    promo_code: Optional[str] = None
    source: str = Field(default="web")
    special_instructions: Optional[str] = None


class UpdateOrderRequest(BaseModel):
    """Update order request schema"""

    delivery_notes: Optional[str] = None
    special_instructions: Optional[str] = None
    delivery_time_slot: Optional[str] = None


class OrderFeedbackRequest(BaseModel):
    """Order feedback request schema"""

    rating: int = Field(..., ge=1, le=5)
    comment: Optional[str] = Field(None, max_length=500)
    would_recommend: Optional[bool] = None
    delivery_rating: Optional[int] = Field(None, ge=1, le=5)
    product_quality_rating: Optional[int] = Field(None, ge=1, le=5)


class OrderStatisticsSchema(BaseModel):
    """Order statistics schema"""

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)

    period: str
    total_orders: int
    total_spent: Decimal
    average_order_value: Decimal
    orders_by_status: Dict[str, int]
    top_products: List[Dict[str, Any]]
    monthly_spending_trend: Dict[str, Decimal]

    @field_validator("total_spent", "average_order_value")
    @classmethod
    def validate_amounts(cls, v):
        return float(v)

    @field_validator("monthly_spending_trend")
    @classmethod
    def validate_monthly_spending(cls, v):
        return {k: float(amount) for k, amount in v.items()}


class CartEstimateRequest(BaseModel):
    """Cart estimate request schema"""

    items: List[Dict[str, Any]] = Field(..., min_length=1)
    delivery_address_id: Optional[int] = None
    delivery_date: Optional[date] = None
    delivery_time_slot: Optional[str] = None
    loyalty_points_used: int = Field(default=0, ge=0)
    promo_code: Optional[str] = None


class CartEstimateResponse(BaseModel):
    """Cart estimate response schema"""

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)

    subtotal: Decimal
    tax_amount: Decimal = Field(default=0)
    delivery_fee: Decimal = Field(default=0)
    discount_amount: Decimal = Field(default=0)
    loyalty_discount: Decimal = Field(default=0)
    total: Decimal

    # Breakdown details
    items_total: Decimal
    promo_discount: Decimal = Field(default=0)
    loyalty_points_discount: Decimal = Field(default=0)

    # Applied promotions
    applied_promo_code: Optional[str] = None
    loyalty_points_used: int = Field(default=0)

    @field_validator(
        "subtotal",
        "tax_amount",
        "delivery_fee",
        "discount_amount",
        "loyalty_discount",
        "total",
        "items_total",
        "promo_discount",
        "loyalty_points_discount",
    )
    @classmethod
    def validate_amounts(cls, v):
        return float(v)


class DeliverySlotSchema(BaseModel):
    """Delivery slot schema"""

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)

    id: int
    name: str
    time_range: str
    delivery_fee: Decimal
    premium_fee: Decimal = Field(default=0)
    is_premium: bool = Field(default=False)
    available_capacity: int
    is_available: bool = Field(default=True)

    @field_validator("delivery_fee", "premium_fee")
    @classmethod
    def validate_fees(cls, v):
        return float(v)


class PromoCodeValidationResponse(BaseModel):
    """Promo code validation response schema"""

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)

    valid: bool
    campaign_name: Optional[str] = None
    campaign_description: Optional[str] = None
    discount_type: Optional[str] = None
    discount_value: Optional[Decimal] = None
    discount_amount: Decimal = Field(default=0)
    max_discount: Optional[Decimal] = None
    min_order_value: Optional[Decimal] = None
    error_message: Optional[str] = None

    @field_validator("discount_value", "discount_amount", "max_discount", "min_order_value")
    @classmethod
    def validate_amounts(cls, v):
        return float(v) if v is not None else None


class OrderTimelineEvent(BaseModel):
    """Order timeline event schema"""

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)

    timestamp: datetime
    event_type: str
    description: str
    details: Optional[Dict[str, Any]] = None
    actor: Optional[str] = None  # user, system, admin


class OrderResponseSchema(BaseModel):
    """Standard order response schema"""

    success: bool
    message: str
    order: Optional[OrderSchema] = None
    errors: Optional[List[str]] = None


# Export all schemas for easy importing
__all__ = [
    "OrderSchema",
    "OrderItemSchema",
    "OrderDeliverySchema",
    "OrderAddressSchema",
    "OrderPaymentSchema",
    "CreateOrderRequest",
    "UpdateOrderRequest",
    "OrderFeedbackRequest",
    "OrderStatisticsSchema",
    "CartEstimateRequest",
    "CartEstimateResponse",
    "DeliverySlotSchema",
    "PromoCodeValidationResponse",
    "OrderTimelineEvent",
    "OrderResponseSchema",
]


def serialize_order(order: Order, include_items=False, include_delivery=False, include_payment=False) -> Dict[str, Any]:
    """
    Serialize order object to dictionary

    Args:
        order: Order model instance
        include_items: Include order items in serialization
        include_delivery: Include delivery information
        include_payment: Include payment information

    Returns:
        Serialized order data
    """
    try:
        order_data = {
            "id": order.id,
            "order_number": order.order_number,
            "user_id": order.user_id,
            "status": order.status.value,
            "tax_amount": float(getattr(order, "tax_amount", 0)),
            "delivery_fee": float(getattr(order, "delivery_fee", 0)),
            "discount_amount": float(getattr(order, "discount_amount", 0)),
            "loyalty_discount": float(getattr(order, "loyalty_discount", 0)),
            "total_amount": float(order.total_amount),
            "is_urgent": getattr(order, "is_urgent", False),
            "order_source": getattr(order, "order_source", "web"),
            "special_instructions": getattr(order, "special_instructions", None),
            "delivery_date": order.delivery_date.isoformat() if order.delivery_date else None,
            "delivery_time_slot": getattr(order, "delivery_time_slot", None),
            "delivery_notes": getattr(order, "delivery_notes", None),
            "promo_code_used": getattr(order, "promo_code_used", None),
            "loyalty_points_used": getattr(order, "loyalty_points_used", 0),
            "payment_method": order.payment_method.value if getattr(order, "payment_method", None) else None,
            "is_paid": getattr(order, "is_paid", False),
            "paid_at": order.paid_at.isoformat() if getattr(order, "paid_at", None) else None,
            "created_at": order.created_at.isoformat(),
            "updated_at": order.updated_at.isoformat() if order.updated_at else None,
            "confirmed_at": order.confirmed_at.isoformat() if getattr(order, "confirmed_at", None) else None,
            "delivered_at": order.delivered_at.isoformat() if getattr(order, "delivered_at", None) else None,
        }

        if include_items and hasattr(order, "order_items"):
            order_data["order_items"] = [serialize_order_item(item) for item in order.order_items]

        if include_delivery and hasattr(order, "delivery") and order.delivery:
            order_data["delivery_info"] = serialize_order_delivery(order.delivery)

        if include_payment and hasattr(order, "payment") and order.payment:
            order_data["payment_info"] = serialize_order_payment(order.payment)

        # Always include delivery address if available
        if hasattr(order, "delivery_address") and order.delivery_address:
            addr = order.delivery_address
            order_data["delivery_address"] = addr.to_dict()

        return order_data

    except Exception as e:
        # Fallback serialization
        return {
            "id": order.id,
            "order_number": order.order_number,
            "user_id": order.user_id,
            "status": str(order.status),
            "total_amount": float(order.total_amount),
            "created_at": order.created_at.isoformat(),
            "error": f"Partial serialization due to: {str(e)}",
        }


def serialize_order_item(order_item: OrderItem) -> Dict[str, Any]:
    """Serialize order item object"""
    try:
        return {
            "id": order_item.id,
            "product_id": order_item.product_id,
            "product_name": order_item.product.name,
            "product_sku": order_item.product.sku,
            "quantity": order_item.quantity,
            "unit_price": float(order_item.unit_price),
            "total_price": float(order_item.total_price),
            # 'product_image_url': getattr(order_item, 'product_image_url', None)
        }
    except Exception:
        return {
            "id": order_item.id,
            "product_id": order_item.product_id,
            "quantity": order_item.quantity,
            "unit_price": float(order_item.unit_price),
            "total_price": float(order_item.total_price),
        }


def serialize_order_delivery(delivery) -> Dict[str, Any]:
    """Serialize delivery information"""
    try:
        return {
            "id": delivery.id,
            "tracking_number": delivery.tracking_number,
            "status": delivery.status.value if hasattr(delivery.status, "value") else str(delivery.status),
            "estimated_delivery_time": (
                delivery.estimated_delivery_time.isoformat() if delivery.estimated_delivery_time else None
            ),
            "actual_delivery_time": (
                delivery.actual_delivery_time.isoformat() if delivery.actual_delivery_time else None
            ),
            "delivery_attempts": getattr(delivery, "delivery_attempts", 0),
            "failed_delivery_reason": getattr(delivery, "failed_delivery_reason", None),
            "customer_rating": getattr(delivery, "customer_rating", None),
            "customer_feedback": getattr(delivery, "customer_feedback", None),
            "current_location_lat": getattr(delivery, "current_location_lat", None),
            "current_location_lng": getattr(delivery, "current_location_lng", None),
            "last_location_update": (
                delivery.last_location_update.isoformat() if getattr(delivery, "last_location_update", None) else None
            ),
            "delivery_person_name": (
                getattr(delivery.delivery_person, "full_name", None)
                if hasattr(delivery, "delivery_person") and delivery.delivery_person
                else None
            ),
            "delivery_person_phone": (
                getattr(delivery.delivery_person, "phone", None)
                if hasattr(delivery, "delivery_person") and delivery.delivery_person
                else None
            ),
        }
    except Exception:
        return {"id": delivery.id, "tracking_number": delivery.tracking_number, "status": str(delivery.status)}


def serialize_order_payment(payment) -> Dict[str, Any]:
    """Serialize payment information"""
    projection = get_payment_projection(payment)
    fiscalization = getattr(payment, "fiscalization", None)

    try:
        return {
            "id": payment.id,
            "payment_method": (
                payment.payment_method.value
                if hasattr(payment.payment_method, "value")
                else str(payment.payment_method)
            ),
            "payment_status": payment.status.value if hasattr(payment.status, "value") else str(payment.status),
            "amount": float(projection["amount"]),
            "amount_collected": float(projection["amount_collected"]),
            "outstanding_amount": float(projection["outstanding_amount"]),
            "currency": getattr(payment, "currency", "UZS"),
            "transaction_id": getattr(payment, "provider_transaction_id", None),
            "provider_transaction_id": getattr(payment, "provider_transaction_id", None),
            "payment_provider": getattr(payment, "payment_provider", None),
            "payment_link": getattr(payment, "payment_link", None),
            "paid_at": payment.paid_at.isoformat() if getattr(payment, "paid_at", None) else None,
            "last_collected_at": (
                payment.last_collected_at.isoformat() if getattr(payment, "last_collected_at", None) else None
            ),
            "collection_events_count": len(getattr(payment, "cash_collection_allocations", []) or []),
            "fiscalization_status": (
                fiscalization.status.value if fiscalization and hasattr(fiscalization.status, "value") else None
            ),
            "fiscalization": fiscalization.to_dict() if fiscalization else None,
        }
    except Exception:
        return {
            "id": payment.id,
            "payment_method": str(payment.payment_method),
            "payment_status": str(payment.status),
            "amount": float(projection["amount"]),
            "amount_collected": float(projection["amount_collected"]),
            "outstanding_amount": float(projection["outstanding_amount"]),
            "payment_link": getattr(payment, "payment_link", None),
        }


def serialize_order_statistics(stats_data: Dict[str, Any], period: str) -> Dict[str, Any]:
    """Serialize order statistics"""
    return {
        "period": period,
        "total_orders": stats_data.get("total_orders", 0),
        "total_spent": float(stats_data.get("total_spent", 0)),
        "average_order_value": float(stats_data.get("average_order_value", 0)),
        "orders_by_status": stats_data.get("orders_by_status", {}),
        "top_products": stats_data.get("top_products", []),
        "monthly_spending_trend": {k: float(v) for k, v in stats_data.get("monthly_spending_trend", {}).items()},
    }


def serialize_cart_estimate(estimate_data: Dict[str, Any]) -> Dict[str, Any]:
    """Serialize cart estimate data"""
    return {
        "subtotal": float(estimate_data.get("subtotal", 0)),
        "tax_amount": float(estimate_data.get("tax_amount", 0)),
        "delivery_fee": float(estimate_data.get("delivery_fee", 0)),
        "discount_amount": float(estimate_data.get("discount_amount", 0)),
        "loyalty_discount": float(estimate_data.get("loyalty_discount", 0)),
        "total": float(estimate_data.get("total", 0)),
        "items_total": float(estimate_data.get("items_total", 0)),
        "promo_discount": float(estimate_data.get("promo_discount", 0)),
        "loyalty_points_discount": float(estimate_data.get("loyalty_points_discount", 0)),
        "applied_promo_code": estimate_data.get("applied_promo_code"),
        "loyalty_points_used": estimate_data.get("loyalty_points_used", 0),
    }


# ============================================================================
# Order Edit (admin) request / response schemas
# ============================================================================


class OrderEditItemSpec(BaseModel):
    """One desired final-state line in an admin order-edit payload."""

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel, populate_by_name=True)

    # None means "insert a new line item for this product". For an existing
    # line, pass the OrderItem id explicitly so the server can detect mismatches.
    order_item_id: Optional[int] = None
    product_id: int
    # Final desired quantity. 0 ⇒ remove the existing line item. Negative is rejected.
    quantity: int = Field(ge=0)


class OrderEditRequest(BaseModel):
    """Request body for POST /admin/orders/<id>/edit and /edit-preview."""

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel, populate_by_name=True)

    items: List[OrderEditItemSpec] = Field(min_length=1)
    reason: str = Field(min_length=3, max_length=1000)


class OrderEditPreviewResponse(BaseModel):
    """Response body for the preview endpoint."""

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel, populate_by_name=True)

    blocking_reasons: List[str]
    warnings: List[str]
    items_before: List[Dict[str, Any]]
    items_after: List[Dict[str, Any]]
    totals_before: Dict[str, Any]
    totals_after: Dict[str, Any]
    cascade_summary: Dict[str, Any]
    is_post_delivery: bool


def serialize_order_edit_history(entry) -> Dict[str, Any]:
    """Serialize an OrderEditHistory row for the admin UI."""
    return entry.to_dict()


def serialize_delivery_slot(slot, target_date=None) -> Dict[str, Any]:
    """Serialize delivery slot"""
    try:
        return {
            "id": slot.id,
            "name": slot.name,
            "time_range": f"{slot.start_time}-{slot.end_time}",
            "delivery_fee": float(slot.delivery_fee),
            "premium_fee": float(getattr(slot, "premium_fee", 0)),
            "is_premium": getattr(slot, "is_premium", False),
            "available_capacity": getattr(slot, "max_orders", 0)
            - (
                slot.get_current_orders_count(target_date)
                if target_date and hasattr(slot, "get_current_orders_count")
                else 0
            ),
            "is_available": True,
        }
    except Exception:
        return {"id": slot.id, "name": slot.name, "delivery_fee": float(slot.delivery_fee), "is_available": True}
