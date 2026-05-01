"""
Payment Serializers for the Water Business Platform using Pydantic v2
This file contains Pydantic models for payment-related data serialization
"""

from datetime import datetime
from typing import Dict, Any, Optional, List
from enum import Enum
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator, ConfigDict
from pydantic.alias_generators import to_camel

from shared.enums import PaymentStatus, PaymentMethod


class RefundStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class OrderInfoSchema(BaseModel):
    """Order information for payment"""

    model_config = ConfigDict(from_attributes=True)

    order_number: str
    total_amount: Decimal

    @field_validator("total_amount")
    @classmethod
    def validate_total_amount(cls, v):
        return float(v)


class SubscriptionInfoSchema(BaseModel):
    """Subscription information for payment"""

    model_config = ConfigDict(from_attributes=True)

    name: str
    billing_cycle: str


class PaymentRefundSchema(BaseModel):
    """Payment refund schema"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    amount: Decimal
    status: RefundStatus
    reason: Optional[str] = None
    created_at: datetime

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, v):
        return float(v)


class PaymentSchema(BaseModel):
    """Payment schema for API responses"""

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)

    id: int
    payment_id: str
    order_id: Optional[int] = None
    subscription_id: Optional[int] = None
    user_id: int
    amount: Decimal = Field(..., description="Payment amount")
    currency: str = Field(default="UZS")
    status: PaymentStatus
    payment_method: PaymentMethod
    description: Optional[str] = None
    is_recurring: bool = Field(default=False)
    provider_payment_id: Optional[str] = None
    provider_response: Optional[Dict[str, Any]] = None
    provider_transaction_id: Optional[str] = None
    payment_provider: Optional[str] = None
    payment_link: Optional[str] = None
    fiscalization_status: Optional[str] = None
    fiscalization: Optional[Dict[str, Any]] = None
    failure_reason: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None
    processed_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    amount_collected: Decimal = Field(default=0)
    outstanding_amount: Decimal = Field(default=0)
    last_collected_at: Optional[datetime] = None
    collection_events_count: int = Field(default=0)

    # Related objects
    order: Optional[OrderInfoSchema] = None
    subscription: Optional[SubscriptionInfoSchema] = None
    refunds: Optional[List[PaymentRefundSchema]] = None

    @field_validator("amount", "amount_collected", "outstanding_amount")
    @classmethod
    def validate_amount(cls, v):
        return float(v)


class PaymentListSchema(BaseModel):
    """Schema for payment list responses"""

    payments: List[PaymentSchema]
    total: int
    page: int
    per_page: int
    pages: int


class CreditCardSchema(BaseModel):
    """Credit card schema"""

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)

    id: int
    user_id: int
    last_four_digits: str
    card_type: Optional[str] = None
    card_brand: Optional[str] = None
    expiry_month: int
    expiry_year: int
    cardholder_name: str
    is_default: bool = Field(default=False)
    is_active: bool = Field(default=True)
    is_expired: bool = Field(default=False)
    created_at: datetime
    updated_at: Optional[datetime] = None

    @field_validator("last_four_digits")
    @classmethod
    def mask_card_number(cls, v):
        """Ensure only last 4 digits are shown"""
        if isinstance(v, str) and len(v) > 4:
            return v[-4:]
        return v


class PaymentRefundFullSchema(BaseModel):
    """Full payment refund schema"""

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)

    id: int
    payment_id: int
    amount: Decimal
    currency: str = Field(default="UZS")
    status: RefundStatus
    reason: Optional[str] = None
    provider_refund_id: Optional[str] = None
    provider_response: Optional[Dict[str, Any]] = None
    processed_by: Optional[int] = None
    created_at: datetime
    processed_at: Optional[datetime] = None

    # Payment information
    payment: Optional[Dict[str, Any]] = None

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, v):
        return float(v)


class PaymentMethodInfoSchema(BaseModel):
    """Payment method information"""

    method: PaymentMethod
    name: str
    display_name: str
    icon_url: str
    description: str
    is_active: bool
    supported_currencies: List[str]
    min_amount: Decimal
    max_amount: Decimal
    processing_fee: Decimal
    supports_recurring: bool
    supports_refunds: bool

    @field_validator("min_amount", "max_amount", "processing_fee")
    @classmethod
    def validate_amounts(cls, v):
        return float(v)


class PaymentStatisticsSchema(BaseModel):
    """Payment statistics schema"""

    period: str
    total_payments: int
    successful_payments: int
    failed_payments: int
    pending_payments: int
    success_rate: float
    total_amount: Decimal
    average_payment: Decimal
    refund_rate: float
    payment_methods: Dict[str, int]
    monthly_trend: Dict[str, Any]
    currency_breakdown: Dict[str, Decimal]
    top_countries: List[Dict[str, Any]]
    processing_times: Dict[str, float]

    @field_validator("total_amount", "average_payment")
    @classmethod
    def validate_amounts(cls, v):
        return float(v)

    @field_validator("currency_breakdown")
    @classmethod
    def validate_currency_breakdown(cls, v):
        if isinstance(v, dict):
            return {k: float(val) for k, val in v.items()}
        return v


class PaymentWebhookSchema(BaseModel):
    """Payment webhook schema"""

    provider: str
    event_type: str
    payment_id: str
    status: str
    amount: Optional[Decimal] = None
    currency: Optional[str] = None
    provider_transaction_id: Optional[str] = None
    timestamp: datetime
    signature: Optional[str] = None
    raw_data: Optional[Dict[str, Any]] = None
    processed: bool = Field(default=False)
    error_message: Optional[str] = None

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, v):
        if v is not None:
            return float(v)
        return v


class PaymentLinkSchema(BaseModel):
    """Payment link schema"""

    payment_link: str
    payment_id: str
    expires_at: datetime
    qr_code_url: Optional[str] = None
    deep_link: Optional[str] = None
    instructions: List[str] = Field(default_factory=list)
    supported_methods: List[PaymentMethod] = Field(default_factory=list)


class CreatePaymentRequest(BaseModel):
    """Create payment request schema"""

    order_id: Optional[int] = None
    subscription_id: Optional[int] = None
    amount: Decimal = Field(..., gt=0, description="Payment amount must be positive")
    currency: str = Field(default="UZS")
    payment_method: PaymentMethod
    description: Optional[str] = None
    return_url: Optional[str] = None
    cancel_url: Optional[str] = None

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, v):
        return float(v)


class ProcessPaymentRequest(BaseModel):
    """Process payment request schema"""

    payment_id: str
    provider_data: Dict[str, Any] = Field(..., description="Provider-specific payment data")


class RefundPaymentRequest(BaseModel):
    """Refund payment request schema"""

    payment_id: int
    amount: Optional[Decimal] = None  # If None, refund full amount
    reason: str = Field(..., min_length=5, max_length=255)

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, v):
        if v is not None and v <= 0:
            raise ValueError("Refund amount must be positive")
        return float(v) if v is not None else v


class PaymentResponseSchema(BaseModel):
    """Standard payment response schema"""

    success: bool
    message: str
    payment: Optional[PaymentSchema] = None
    payment_link: Optional[str] = None
    errors: Optional[List[str]] = None


# Export all schemas for easy importing
__all__ = [
    "PaymentSchema",
    "PaymentListSchema",
    "CreditCardSchema",
    "PaymentRefundFullSchema",
    "PaymentMethodInfoSchema",
    "PaymentStatisticsSchema",
    "PaymentWebhookSchema",
    "PaymentLinkSchema",
    "CreatePaymentRequest",
    "ProcessPaymentRequest",
    "RefundPaymentRequest",
    "PaymentResponseSchema",
    "PaymentStatus",
    "PaymentMethod",
    "RefundStatus",
]


def serialize_payment(payment, include_sensitive: bool = False) -> Dict[str, Any]:
    """
    Serialize a payment object to dictionary using Pydantic

    Args:
        payment: Payment model instance
        include_sensitive: Whether to include sensitive information

    Returns:
        Serialized payment data
    """
    try:
        schema = PaymentSchema.model_validate(payment)
        result = schema.model_dump(by_alias=True, exclude_none=True)
        if getattr(payment, "provider_transaction_id", None):
            result["providerTransactionId"] = payment.provider_transaction_id
        if getattr(payment, "payment_provider", None):
            result["paymentProvider"] = payment.payment_provider
        if getattr(payment, "payment_link", None):
            result["paymentLink"] = payment.payment_link
        if getattr(payment, "fiscalization", None):
            result["fiscalization"] = payment.fiscalization.to_dict()
            result["fiscalizationStatus"] = (
                payment.fiscalization.status.value
                if hasattr(payment.fiscalization.status, "value")
                else str(payment.fiscalization.status)
            )

        # Filter sensitive data if needed
        if not include_sensitive and "provider_response" in result:
            result.pop("provider_response", None)

        return result
    except Exception:
        # Fallback to manual serialization if Pydantic validation fails
        return {
            "id": payment.id,
            "payment_id": payment.payment_id,
            "order_id": payment.order_id,
            "user_id": payment.user_id,
            "amount": float(payment.amount),
            "currency": payment.currency,
            "status": payment.status.value if payment.status else None,
            "payment_method": payment.payment_method.value if payment.payment_method else None,
            "payment_provider": getattr(payment, "payment_provider", None),
            "provider_transaction_id": getattr(payment, "provider_transaction_id", None),
            "payment_link": getattr(payment, "payment_link", None),
            "fiscalization_status": (
                payment.fiscalization.status.value
                if getattr(payment, "fiscalization", None) and hasattr(payment.fiscalization.status, "value")
                else None
            ),
            "amount_collected": float(getattr(payment, "amount_collected", 0) or 0),
            "outstanding_amount": float(getattr(payment, "outstanding_amount", 0) or 0),
            "last_collected_at": (
                payment.last_collected_at.isoformat() if getattr(payment, "last_collected_at", None) else None
            ),
            "collection_events_count": len(getattr(payment, "cash_collection_allocations", []) or []),
            "created_at": payment.created_at.isoformat() if payment.created_at else None,
        }


def serialize_payment_list(payments: List, include_sensitive: bool = False) -> List[Dict[str, Any]]:
    """
    Serialize a list of payments

    Args:
        payments: List of payment model instances
        include_sensitive: Whether to include sensitive information

    Returns:
        List of serialized payment data
    """
    return [serialize_payment(payment, include_sensitive) for payment in payments]


def serialize_credit_card(card) -> Dict[str, Any]:
    """
    Serialize a credit card object to dictionary using Pydantic

    Args:
        card: CreditCard model instance

    Returns:
        Serialized credit card data
    """
    try:
        schema = CreditCardSchema.model_validate(card)
        return schema.model_dump(by_alias=True, exclude_none=True)
    except Exception:
        # Fallback to manual serialization
        return {
            "id": card.id,
            "user_id": card.user_id,
            "last_four_digits": card.last_four_digits if card.last_four_digits else None,
            "card_type": card.card_brand,
            "expiry_month": card.expiry_month,
            "expiry_year": card.expiry_year,
            "cardholder_name": card.cardholder_name,
            "is_default": card.is_default,
            "is_active": card.is_active,
            "created_at": card.created_at.isoformat() if card.created_at else None,
        }


def get_available_payment_methods() -> List[Dict[str, Any]]:
    """Get available payment methods"""
    methods_config = [
        {
            "method": "click",
            "name": "click",
            "display_name": "Click",
            "icon_url": "/static/images/payment/click.png",
            "description": "Pay with Click wallet or linked card",
            "is_active": True,
            "supported_currencies": ["UZS"],
            "min_amount": 1000,
            "max_amount": 50000000,
            "processing_fee": 0.0,
            "supports_recurring": True,
            "supports_refunds": True,
        },
        {
            "method": "payme",
            "name": "payme",
            "display_name": "Payme",
            "icon_url": "/static/images/payment/payme.png",
            "description": "Pay with Payme wallet or linked card",
            "is_active": True,
            "supported_currencies": ["UZS"],
            "min_amount": 1000,
            "max_amount": 50000000,
            "processing_fee": 0.0,
            "supports_recurring": True,
            "supports_refunds": True,
        },
        {
            "method": "cash",
            "name": "cash",
            "display_name": "Cash on Delivery",
            "icon_url": "/static/images/payment/cash.png",
            "description": "Pay with cash when order is delivered",
            "is_active": True,
            "supported_currencies": ["UZS"],
            "min_amount": 0,
            "max_amount": 5000000,
            "processing_fee": 0.0,
            "supports_recurring": False,
            "supports_refunds": False,
        },
    ]

    return [PaymentMethodInfoSchema(**method).model_dump(mode="json") for method in methods_config]
