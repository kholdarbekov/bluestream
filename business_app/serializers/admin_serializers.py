"""
Admin Serializers for the Water Business Platform using Pydantic v2
This file contains Pydantic models for admin-related data serialization
"""

import logging
from datetime import datetime, date, UTC
from typing import Dict, Any, Optional, List, Union, Literal
from enum import Enum
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator, ConfigDict
from pydantic.alias_generators import to_camel
from business_app.models.product import Product, ProductCategory
from business_app.models.order import Order
from business_app.utils.user_types import normalize_user_type
from business_app.utils.constants import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from business_app.utils.delivery_window import format_delivery_window
from business_app.serializers.types import MoneyFloat


class UserRole(str, Enum):
    CUSTOMER = "customer"
    ADMIN = "admin"
    SUPER_ADMIN = "super_admin"
    MANAGER = "manager"
    SUPPORT = "support"
    DELIVERY_PERSON = "delivery_person"


class UserStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    SUSPENDED = "suspended"
    BANNED = "banned"
    PENDING = "pending"


class OrderStatus(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    PROCESSING = "processing"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"


class AdminActionType(str, Enum):
    USER_STATUS_CHANGE = "user_status_change"
    ORDER_STATUS_CHANGE = "order_status_change"
    PRODUCT_UPDATE = "product_update"
    STOCK_ADJUSTMENT = "stock_adjustment"
    BULK_ACTION = "bulk_action"
    SYSTEM_CONFIG = "system_config"
    REPORT_GENERATION = "report_generation"
    PROMOTION_MANAGEMENT = "promotion_management"


class ReportType(str, Enum):
    SALES_SUMMARY = "sales_summary"
    CUSTOMER_REPORT = "customer_report"
    PRODUCT_PERFORMANCE = "product_performance"
    DELIVERY_REPORT = "delivery_report"
    FINANCIAL_SUMMARY = "financial_summary"
    USER_ACTIVITY = "user_activity"
    INVENTORY_REPORT = "inventory_report"
    ANALYTICS_REPORT = "analytics_report"


class UserAdminSchema(BaseModel):
    """User admin schema"""

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)

    id: int
    email: str
    phone: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    role: UserRole
    status: UserStatus
    email_verified: bool = Field(default=False)
    phone_verified: bool = Field(default=False)
    created_at: Optional[datetime] = None
    last_login: Optional[datetime] = None
    last_activity_at: Optional[datetime] = None

    # Admin-specific fields
    login_attempts: int = Field(default=0)
    is_locked: bool = Field(default=False)
    locked_until: Optional[datetime] = None
    two_factor_enabled: bool = Field(default=False)
    cod_debt_check_exempt: bool = Field(default=False)

    # Statistics
    total_orders: int = Field(default=0)
    total_spent: MoneyFloat = Field(default=0)
    average_order_value: MoneyFloat = Field(default=0)
    lifetime_value: MoneyFloat = Field(default=0)
    loyalty_points: int = Field(default=0)
    referral_count: int = Field(default=0)


class UserListAdminSchema(BaseModel):
    """User list admin schema"""

    users: List[UserAdminSchema]
    total: int
    page: int
    per_page: int
    pages: int
    filters_applied: Dict[str, Any] = Field(default_factory=dict)


class OrderAdminSchema(BaseModel):
    """Order admin schema"""

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)

    id: int
    order_number: str
    user_id: int
    customer_name: Optional[str] = None
    customer_email: Optional[str] = None
    customer_phone: Optional[str] = None
    status: OrderStatus
    total_amount: MoneyFloat
    tax_amount: MoneyFloat = Field(default=0)
    discount_amount: MoneyFloat = Field(default=0)
    delivery_fee: MoneyFloat = Field(default=0)
    payment_method: Optional[str] = None
    payment_status: Optional[str] = None
    is_subscription_order: bool = Field(default=False)
    subscription_id: Optional[int] = None
    delivery_date: Optional[date] = None
    delivery_window: Optional[Dict[str, Any]] = None
    awaiting_release: bool = Field(default=False)
    release_at: Optional[datetime] = None
    delivery_address: Optional[str] = None
    special_instructions: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    # Order items summary
    item_count: int = Field(default=0)
    items_summary: List[Dict[str, Any]] = Field(default_factory=list)

    # Delivery information
    delivery_person_name: Optional[str] = None
    tracking_number: Optional[str] = None

    # Admin notes
    admin_notes: Optional[str] = None
    priority_level: str = Field(default="normal")


class OrderListAdminSchema(BaseModel):
    """Order list admin schema"""

    orders: List[OrderAdminSchema]
    total: int
    page: int
    per_page: int
    pages: int
    total_revenue: MoneyFloat = Field(default=0)
    filters_applied: Dict[str, Any] = Field(default_factory=dict)


class ProductAdminSchema(BaseModel):
    """Product admin schema"""

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)

    id: int
    name: str
    sku: str
    barcode: Optional[str] = None
    category_name: Optional[str] = None
    base_price: MoneyFloat
    current_price: MoneyFloat
    stock_quantity: Optional[int] = None
    min_stock_level: Optional[int] = None
    is_active: bool = Field(default=True)
    is_featured: bool = Field(default=False)
    track_inventory: bool = Field(default=True)
    is_tryout_eligible: bool = Field(default=True)
    tracks_returnable_bottles: bool = Field(default=False)
    returnable_bottles_per_unit: float = Field(default=0.0)

    # Performance metrics
    total_sold: int = Field(default=0)
    total_revenue: MoneyFloat = Field(default=0)
    view_count: int = Field(default=0)
    average_rating: float = Field(default=0.0)
    review_count: int = Field(default=0)

    # Stock status
    stock_status: str = Field(default="in_stock")  # in_stock, low_stock, out_of_stock
    days_of_stock: Optional[int] = None

    # Dates
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    last_sold_at: Optional[datetime] = None


class ProductListAdminSchema(BaseModel):
    """Product list admin schema"""

    products: List[ProductAdminSchema]
    total: int
    page: int
    per_page: int
    pages: int
    low_stock_count: int = Field(default=0)
    out_of_stock_count: int = Field(default=0)
    filters_applied: Dict[str, Any] = Field(default_factory=dict)


class DeliveryPersonAdminSchema(BaseModel):
    """Delivery person admin schema"""

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)

    id: int
    user_id: int
    full_name: str
    phone: str
    email: Optional[str] = None
    vehicle_type: str
    vehicle_number: Optional[str] = None
    license_number: Optional[str] = None
    is_active: bool = Field(default=True)
    is_available: bool = Field(default=True)
    current_location_lat: Optional[float] = None
    current_location_lng: Optional[float] = None
    last_location_update: Optional[datetime] = None

    # Performance metrics
    total_deliveries: int = Field(default=0)
    successful_deliveries: int = Field(default=0)
    failed_deliveries: int = Field(default=0)
    success_rate: float = Field(default=0.0)
    average_rating: float = Field(default=0.0)
    rating_count: int = Field(default=0)
    average_delivery_time: Optional[float] = None  # in minutes
    on_time_percentage: float = Field(default=0.0)

    # Current workload
    active_deliveries: int = Field(default=0)
    pending_deliveries: int = Field(default=0)

    # Admin fields
    hire_date: Optional[date] = None
    employee_id: Optional[str] = None
    emergency_contact: Optional[str] = None
    monthly_earnings: MoneyFloat = Field(default=0)

    # Status
    verification_status: str = Field(default="verified")
    background_check_status: str = Field(default="passed")


class SystemSettingSchema(BaseModel):
    """System setting schema"""

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)

    key: str
    value: Union[str, int, float, bool, Dict[str, Any]]
    category: str = Field(default="general")
    description: Optional[str] = None
    data_type: str = Field(default="string")  # string, integer, float, boolean, json
    is_sensitive: bool = Field(default=False)
    is_editable: bool = Field(default=True)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    updated_by: Optional[int] = None


class AuditLogSchema(BaseModel):
    """Audit log schema"""

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)

    id: int
    admin_id: int
    admin_name: Optional[str] = None
    action: AdminActionType
    target_type: Optional[str] = None  # user, order, product, etc.
    target_id: Optional[int] = None
    details: Optional[str] = None
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    created_at: datetime

    # Additional context
    before_data: Optional[Dict[str, Any]] = None
    after_data: Optional[Dict[str, Any]] = None
    risk_level: str = Field(default="low")  # low, medium, high


class AuditLogListSchema(BaseModel):
    """Audit log list schema"""

    logs: List[AuditLogSchema]
    total: int
    page: int
    per_page: int
    pages: int
    filters_applied: Dict[str, Any] = Field(default_factory=dict)


class AdminDashboardSchema(BaseModel):
    """Admin dashboard schema"""

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)

    # User metrics
    users: Dict[str, int] = Field(default_factory=dict)

    # Order metrics
    orders: Dict[str, Union[int, Decimal]] = Field(default_factory=dict)

    # Product metrics
    products: Dict[str, int] = Field(default_factory=dict)

    # Delivery metrics
    delivery: Dict[str, int] = Field(default_factory=dict)

    # Subscription metrics
    subscriptions: Dict[str, Union[int, Decimal]] = Field(default_factory=dict)

    # System health
    system_health: Dict[str, Any] = Field(default_factory=dict)

    # Recent activity
    recent_orders: List[Dict[str, Any]] = Field(default_factory=list)
    recent_users: List[Dict[str, Any]] = Field(default_factory=list)
    low_stock_alerts: List[Dict[str, Any]] = Field(default_factory=list)
    pending_deliveries: List[Dict[str, Any]] = Field(default_factory=list)

    # Timestamp
    generated_at: datetime


class BulkActionRequestSchema(BaseModel):
    """Bulk action request schema"""

    action: str = Field(..., pattern=r"^(activate|deactivate|suspend|delete|update_stock|send_email|export)$")
    target_type: str = Field(..., pattern=r"^(user|order|product|delivery)$")
    target_ids: List[int] = Field(..., min_items=1, max_items=1000)
    parameters: Dict[str, Any] = Field(default_factory=dict)
    reason: Optional[str] = Field(None, max_length=500)

    @field_validator("target_ids")
    @classmethod
    def validate_target_ids(cls, v):
        if len(v) != len(set(v)):
            raise ValueError("Target IDs must be unique")
        return v


class BulkActionResultSchema(BaseModel):
    """Bulk action result schema"""

    action: str
    target_type: str
    total_items: int
    successful_items: int
    failed_items: int
    success_rate: float
    execution_time: float  # in seconds
    results: List[Dict[str, Any]] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)
    started_at: datetime
    completed_at: datetime


class ReportConfigSchema(BaseModel):
    """Report configuration schema"""

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)

    id: Optional[int] = None
    name: str = Field(..., min_length=3, max_length=100)
    report_type: ReportType
    description: Optional[str] = Field(None, max_length=500)
    parameters: Dict[str, Any] = Field(default_factory=dict)
    filters: Dict[str, Any] = Field(default_factory=dict)
    format_type: str = Field(default="pdf", pattern=r"^(pdf|excel|csv|json)$")
    is_scheduled: bool = Field(default=False)
    schedule_frequency: Optional[str] = Field(None, pattern=r"^(daily|weekly|monthly|quarterly)$")
    schedule_time: Optional[str] = None  # HH:MM format
    recipients: List[str] = Field(default_factory=list)
    is_active: bool = Field(default=True)
    created_by: Optional[int] = None
    created_at: Optional[datetime] = None
    last_generated_at: Optional[datetime] = None


class GeneratedReportSchema(BaseModel):
    """Generated report schema"""

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)

    id: int
    report_config_id: Optional[int] = None
    name: str
    report_type: ReportType
    file_path: Optional[str] = None
    file_url: Optional[str] = None
    file_size: Optional[int] = None  # in bytes
    format_type: str
    status: str = Field(default="generating")  # generating, completed, failed
    parameters: Dict[str, Any] = Field(default_factory=dict)
    generated_by: int
    generated_at: datetime
    expires_at: Optional[datetime] = None
    download_count: int = Field(default=0)

    # Error information
    error_message: Optional[str] = None
    execution_time: Optional[float] = None  # in seconds


class BackupConfigSchema(BaseModel):
    """Backup configuration schema"""

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)

    id: int
    name: str
    backup_type: str = Field(default="full", pattern=r"^(full|incremental|differential)$")
    frequency: str = Field(default="daily", pattern=r"^(hourly|daily|weekly|monthly)$")
    retention_days: int = Field(default=30, ge=1, le=365)
    include_files: bool = Field(default=True)
    include_database: bool = Field(default=True)
    compression_enabled: bool = Field(default=True)
    encryption_enabled: bool = Field(default=True)
    storage_location: str = Field(default="local")
    is_active: bool = Field(default=True)
    last_backup_at: Optional[datetime] = None
    next_backup_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


class NotificationTemplateAdminSchema(BaseModel):
    """Notification template admin schema"""

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)

    id: int
    name: str
    category: str
    notification_type: str
    subject_template: Optional[str] = None
    body_template: str
    variables: List[str] = Field(default_factory=list)
    is_active: bool = Field(default=True)
    language: str = Field(default="uz")
    usage_count: int = Field(default=0)
    last_used_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    created_by: Optional[int] = None


# Request schemas for admin actions
class UpdateUserStatusRequest(BaseModel):
    """Update user status request"""

    user_id: int
    status: UserStatus
    reason: Optional[str] = Field(None, max_length=500)
    notify_user: bool = Field(default=True)


class UpdateOrderStatusRequest(BaseModel):
    """Update order status request"""

    order_id: int
    status: OrderStatus
    notes: Optional[str] = Field(None, max_length=1000)
    notify_customer: bool = Field(default=True)


class StockAdjustmentRequest(BaseModel):
    """Stock adjustment request"""

    product_id: int
    new_stock_quantity: int = Field(..., ge=0)
    adjustment_type: str = Field(default="manual", pattern=r"^(manual|received|sold|damaged|returned)$")
    reason: Optional[str] = Field(None, max_length=500)


class SystemMaintenanceRequest(BaseModel):
    """System maintenance request"""

    maintenance_type: str = Field(..., pattern=r"^(scheduled|emergency|update)$")
    start_time: datetime
    estimated_duration: int = Field(..., ge=1, le=1440)  # minutes
    description: str = Field(..., min_length=10, max_length=1000)
    affected_services: List[str] = Field(default_factory=list)
    notify_users: bool = Field(default=True)


class AdminResponseSchema(BaseModel):
    """Standard admin response schema"""

    success: bool
    message: str
    data: Optional[Dict[str, Any]] = None
    action_id: Optional[str] = None
    errors: Optional[List[str]] = None


class InactiveCustomersQuerySchema(BaseModel):
    """Query params for GET /admin/analytics/inactive-customers"""

    model_config = ConfigDict(extra="ignore")

    days_since: int = Field(default=30, ge=0, le=3650)
    customer_type: Literal["all", "individual", "workplace", "grocery"] = "all"
    include_never_ordered: bool = True
    page: int = Field(default=1, ge=1)
    per_page: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)

    @field_validator("include_never_ordered", mode="before")
    @classmethod
    def _parse_bool(cls, v):
        if isinstance(v, str):
            return v.lower() in ("1", "true", "yes", "on")
        return v


# Export all schemas for easy importing
__all__ = [
    "UserAdminSchema",
    "UserListAdminSchema",
    "OrderAdminSchema",
    "OrderListAdminSchema",
    "ProductAdminSchema",
    "ProductListAdminSchema",
    "DeliveryPersonAdminSchema",
    "SystemSettingSchema",
    "AuditLogSchema",
    "AuditLogListSchema",
    "AdminDashboardSchema",
    "BulkActionRequestSchema",
    "BulkActionResultSchema",
    "ReportConfigSchema",
    "GeneratedReportSchema",
    "BackupConfigSchema",
    "NotificationTemplateAdminSchema",
    "UpdateUserStatusRequest",
    "UpdateOrderStatusRequest",
    "StockAdjustmentRequest",
    "SystemMaintenanceRequest",
    "AdminResponseSchema",
    "InactiveCustomersQuerySchema",
    "CustomerMapPinSchema",
    "UserRole",
    "UserStatus",
    "OrderStatus",
    "AdminActionType",
    "ReportType",
]


def serialize_user_admin(user, include_statistics: bool = False) -> Dict[str, Any]:
    """
    Serialize user for admin view

    Args:
        user: User model instance
        include_statistics: Whether to include detailed statistics

    Returns:
        Serialized user data for admin
    """
    try:
        data = {
            "id": user.id,
            "email": user.email,
            "phone": user.phone,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "role": user.role.value if hasattr(user.role, "value") else (user.role if user.role else "customer"),
            "status": (
                user.status.value if hasattr(user.status, "value") else (user.status if user.status else "active")
            ),
            "email_verified": user.email_verified_at is not None,
            "phone_verified": user.phone_verified_at is not None,
            "created_at": user.created_at.isoformat() if user.created_at else None,
            "updated_at": user.updated_at.isoformat() if user.updated_at else None,
            "last_login": user.last_login.isoformat() if user.last_login else None,
            "last_activity_at": getattr(user, "last_activity_at", None),
            "login_attempts": getattr(user, "failed_login_attempts", 0),
            "account_locked_until": user.account_locked_until.isoformat() if user.account_locked_until else None,
            "two_factor_enabled": getattr(user, "two_factor_enabled", False),
            "cod_debt_check_exempt": bool(getattr(user, "cod_debt_check_exempt", False)),
            "registration_source": user.registration_source,
            "registration_method": getattr(user, "registration_method", None),
            "preferred_language": getattr(user, "preferred_language", None),
            "is_verified": getattr(user, "is_verified", False),
            "user_type": normalize_user_type(
                getattr(user, "user_type", None),
                role=getattr(user, "role", None),
                staff_roles=getattr(user, "staff_roles", None),
            ),
            "company_name": getattr(user, "company_name", None),
            "tax_id": getattr(user, "tax_id", None),
            "date_of_birth": user.date_of_birth.isoformat() if getattr(user, "date_of_birth", None) else None,
            "entity_subtype": (
                getattr(user, "entity_subtype", None).value
                if getattr(user, "entity_subtype", None) is not None
                and hasattr(getattr(user, "entity_subtype", None), "value")
                else getattr(user, "entity_subtype", None)
            ),
            # Telegram fields
            "telegram_id": user.telegram_id,
            "telegram_username": getattr(user, "telegram_username", None),
            "is_bot_active": getattr(user, "is_bot_active", False),
            "last_bot_interaction": (
                user.last_bot_interaction.isoformat() if getattr(user, "last_bot_interaction", None) else None
            ),
        }

        if include_statistics:
            # Add user statistics (would typically come from database queries)
            data.update(
                {
                    "total_orders": get_user_order_count(user.id),
                    "total_spent": get_user_total_spent(user.id),
                    "average_order_value": get_user_average_order_value(user.id),
                    "lifetime_value": get_user_lifetime_value(user.id),
                    "loyalty_points": get_user_loyalty_points(user.id),
                    "referral_count": get_user_referral_count(user.id),
                }
            )

        return data

    except Exception:
        # Fallback to basic serialization
        return {
            "id": user.id,
            "email": user.email,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "role": getattr(user, "role", "customer"),
            "status": getattr(user, "status", "active"),
            "created_at": user.created_at.isoformat() if user.created_at else None,
        }


def serialize_order_admin(order: Order) -> Dict[str, Any]:
    """
    Serialize order for admin view

    Args:
        order: Order model instance

    Returns:
        Serialized order data for admin
    """
    try:
        # Local import: order_schedule_service imports business_app.models.*
        # only, so this is not a real cycle risk, but every other
        # cross-service import in this function (see order_serializers below)
        # is already lazy -- matching that keeps this module importable before
        # the service layer is fully wired up.
        from business_app.serializers.order_serializers import payability_fields
        from business_app.services.order_schedule_service import OrderScheduleService

        awaiting_release = OrderScheduleService.is_awaiting_release(order)
        release_at = OrderScheduleService.release_at(order) if awaiting_release else None

        data = {
            "id": order.id,
            "order_number": order.order_number,
            "user_id": order.user_id,
            "status": order.status.value if order.status else None,
            "total_amount": float(order.total_amount),
            "tax_amount": float(getattr(order, "tax_amount", 0)),
            "discount_amount": float(getattr(order, "discount_amount", 0)),
            "delivery_fee": float(getattr(order, "delivery_fee", 0)),
            "payment_method": order.payment_method.value if order.payment_method else None,
            "is_subscription_order": bool(order.is_subscription_order),
            "subscription_id": order.subscription_id,
            "delivery_date": order.delivery_date.isoformat() if order.delivery_date else None,
            "delivery_window": format_delivery_window(order.delivery_window_start, order.delivery_window_end),
            "awaiting_release": awaiting_release,
            "release_at": release_at.isoformat() if release_at else None,
            "delivery_address": order.delivery_address.to_dict() if getattr(order, "delivery_address", None) else None,
            "special_instructions": getattr(order, "special_instructions", None),
            "created_at": order.created_at.isoformat() if order.created_at else None,
            "updated_at": order.updated_at.isoformat() if order.updated_at else None,
        }

        # Add customer information
        if order.user:
            data["customer_name"] = f"{order.user.first_name} {order.user.last_name}".strip()
            data["customer_email"] = order.user.email
            data["customer_phone"] = order.user.phone

        # Add order items summary.
        #
        # Truncation is NOT decided here: `summarize_order_items` owns how many
        # lines a compact summary shows, so this screen and the dispatch/
        # delivery rows can't disagree about the same order (they used to —
        # 5 here, 3 there). `with_prices` widens each row for the money columns
        # the Orders page renders; it does not change which rows appear.
        # `item_count` stays the TRUE line count so "+N more" is honest.
        if hasattr(order, "order_items") and order.order_items:
            from business_app.serializers.order_serializers import is_free_reward_item, summarize_order_items

            summary = summarize_order_items(order, with_prices=True)
            data["item_count"] = summary["total_count"]
            data["items_summary"] = summary["items"]
            data["has_loyalty_reward"] = float(getattr(order, "loyalty_discount", 0) or 0) > 0 or any(
                is_free_reward_item(it) for it in order.order_items
            )
        else:
            data["item_count"] = 0
            data["items_summary"] = []
            data["has_loyalty_reward"] = float(getattr(order, "loyalty_discount", 0) or 0) > 0

        # Add delivery information
        if hasattr(order, "delivery") and order.delivery:
            delivery = order.delivery
            data["tracking_number"] = delivery.tracking_number
            if delivery.delivery_person:
                data["delivery_person_name"] = delivery.delivery_person.full_name

        # Add admin-specific fields
        data["admin_notes"] = getattr(order, "admin_notes", None)
        data["priority_level"] = getattr(order, "priority_level", "normal")

        # Add payment status from related Payment model
        if hasattr(order, "payment") and order.payment:
            data["payment_id"] = order.payment.id
            data["payment_status"] = (
                order.payment.status.value if hasattr(order.payment.status, "value") else str(order.payment.status)
            )
            data["payment_provider"] = getattr(order.payment, "payment_provider", None)
            data["payment_link"] = getattr(order.payment, "payment_link", None)
            data["provider_transaction_id"] = getattr(order.payment, "provider_transaction_id", None)
            data["consume_marking_codes"] = bool(getattr(order.payment, "consume_marking_codes", False))
            fiscalization = getattr(order.payment, "fiscalization", None)
            data["fiscalization_status"] = (
                fiscalization.status.value if fiscalization and hasattr(fiscalization.status, "value") else None
            )
            data["fiscalization_retries_exhausted"] = bool(
                fiscalization and getattr(fiscalization, "retries_exhausted_at", None)
            )
            data["fiscalization"] = fiscalization.to_dict() if fiscalization else None
            # B3 -- the backend answers payability instead of letting the two
            # "Open Payment Link" buttons in Orders.js infer it from "we stored
            # a link once". See `payability_fields`.
            data.update(payability_fields(order, order.payment))
        else:
            data["payment_id"] = None
            data["payment_status"] = "pending"
            data["payment_provider"] = None
            data["payment_link"] = None
            data["provider_transaction_id"] = None
            data["consume_marking_codes"] = False
            data["fiscalization_status"] = None
            data["fiscalization_retries_exhausted"] = False
            # DEFAULTED, never omitted: an absent key reaches JS as `undefined`
            # and silently flips every truthiness test that reads it.
            data["is_payable"] = False
            data["payable_payment_link"] = None

        if getattr(order, "payment", None):
            from business_app.services.payment_fiscalization_service import PaymentFiscalizationService

            data["marking_code_summary"] = PaymentFiscalizationService().marking_code_allocation_summary(order)
        else:
            data["marking_code_summary"] = {"events": {}, "codes_by_order_item": {}}

        return data

    except Exception as e:
        logging.error(f"Exception in serialize_order_admin: {e}")
        # Fallback to basic serialization
        status = getattr(order, "status", None)
        return {
            "id": order.id,
            "order_number": order.order_number,
            "user_id": order.user_id,
            "status": status.value if status else "pending",
            "total_amount": float(order.total_amount),
            "created_at": order.created_at.isoformat() if order.created_at else None,
        }


def _admin_stock_quantity(product: Product, marking_code_counts: Dict[str, int]) -> Optional[int]:
    """The stock value to publish for the admin listing/edit views.

    ``marking_code_counts`` is the SAME dict serialize_product_admin already
    computed via ProductFiscalService.build_product_fiscal_snapshot -- reused
    here rather than re-querying, so the AVAILABLE count is computed once per
    product, not once per consumer.
    """
    from business_app.services.product_fiscal_service import ProductFiscalService

    if ProductFiscalService.is_stock_derived(product):
        # SSOT: the same call the admin write guard makes, so what we publish
        # here is exactly what an echoed payload is measured against.
        return ProductFiscalService.published_stock_quantity(product, marking_code_counts=marking_code_counts)
    return product.stock_quantity if product.track_inventory else None


def serialize_product_admin(product: Product) -> Dict[str, Any]:
    """
    Serialize product for admin view

    Args:
        product: Product model instance

    Returns:
        Serialized product data for admin
    """
    try:
        # Get images array and extract first image URL for convenience
        images = product.images or []
        image_url = images[0] if images else None

        # Computed once, up front: both the derived stock_quantity below and
        # the marking_code_counts merged in later read from this SAME dict,
        # so the AVAILABLE count is one query, not two.
        from business_app.services.product_fiscal_service import ProductFiscalService

        fiscal_snapshot = ProductFiscalService().build_product_fiscal_snapshot(product)

        data = {
            "id": product.id,
            "name": product.name,
            "description": product.description,
            "short_description": product.short_description,
            "sku": product.sku,
            "barcode": product.barcode,
            "base_price": float(product.base_price),
            "price": float(product.base_price),  # Frontend expects 'price'
            "discount_price": float(product.discount_price) if product.discount_price else None,
            "current_price": float(getattr(product, "current_price", product.base_price)),
            "category_id": product.category_id,
            "volume": product.volume,
            "volume_unit": product.volume_unit,
            # For a marking-code product the pool IS the stock; the column is a
            # projection that goes stale between marking-code events and that no
            # admin can correct by hand. Publish the fact instead.
            "stock_quantity": _admin_stock_quantity(product, fiscal_snapshot["marking_code_counts"]),
            "min_stock_level": product.min_stock_level,
            "min_order_quantity": int(getattr(product, "min_order_quantity", 1) or 1),
            "is_active": product.is_active,
            "status": "active" if product.is_active else "inactive",  # Frontend expects 'status'
            "is_featured": product.is_featured,
            "track_inventory": product.track_inventory,
            "is_tryout_eligible": bool(getattr(product, "is_tryout_eligible", True)),
            "tracks_returnable_bottles": bool(getattr(product, "tracks_returnable_bottles", False)),
            "returnable_bottles_per_unit": float(getattr(product, "returnable_bottles_per_unit", 0) or 0),
            "images": images,
            "image_url": image_url,  # First image for display convenience
            "created_at": product.created_at.isoformat() if product.created_at else None,
            "updated_at": product.updated_at.isoformat() if product.updated_at else None,
            # Translations
            "name_translations": product.get_all_translations("name"),
            "description_translations": product.get_all_translations("description"),
            "short_description_translations": product.get_all_translations("short_description"),
            "ingredients_translations": product.get_all_translations("ingredients"),
            "meta_title_translations": product.get_all_translations("meta_title"),
            "meta_description_translations": product.get_all_translations("meta_description"),
            "expire_days": product.expire_days,
        }

        # Add category information
        if product.category:
            data["category_name"] = product.category.name

        data.update(fiscal_snapshot)

        # Add performance metrics
        data["total_sold"] = getattr(product, "total_sold", 0)
        data["total_revenue"] = float(getattr(product, "total_revenue", 0))
        data["view_count"] = getattr(product, "view_count", 0)
        data["average_rating"] = float(getattr(product, "average_rating", 0))
        data["review_count"] = getattr(product, "review_count", 0)

        # Determine stock status
        if not product.track_inventory:
            data["stock_status"] = "not_tracked"
        elif product.stock_quantity == 0:
            data["stock_status"] = "out_of_stock"
        elif product.stock_quantity <= (product.min_stock_level or 0):
            data["stock_status"] = "low_stock"
        else:
            data["stock_status"] = "in_stock"

        # Calculate days of stock (placeholder calculation)
        if product.track_inventory and product.stock_quantity:
            # This would typically use sales velocity data
            average_daily_sales = getattr(product, "average_daily_sales", 1) or 1
            data["days_of_stock"] = int(product.stock_quantity / average_daily_sales)

        data["last_sold_at"] = getattr(product, "last_sold_at", None)

        return data

    except Exception:
        # Fallback to basic serialization
        return {
            "id": product.id,
            "name": product.name,
            "sku": product.sku,
            "base_price": float(product.base_price),
            "stock_quantity": getattr(product, "stock_quantity", None),
            "is_active": product.is_active,
            "created_at": product.created_at.isoformat() if product.created_at else None,
        }


def serialize_delivery_person_admin(person, current_active_deliveries: Optional[int] = None) -> Dict[str, Any]:
    """
    Serialize delivery person for admin view

    Args:
        person: DeliveryPerson model instance

    Returns:
        Serialized delivery person data for admin
    """
    try:
        vehicle_type = person.vehicle_type.value if hasattr(person.vehicle_type, "value") else person.vehicle_type
        data = {
            "id": person.id,
            # `user_id` is what every *_user_id column FKs to. Clients must assign by
            # this, never by `id` (the delivery_persons PK) — the two id spaces overlap
            # numerically, so confusing them silently targets an unrelated account.
            "user_id": person.user_id,
            "full_name": person.full_name,
            "phone": person.phone,
            "email": getattr(person, "email", None),
            "vehicle_type": vehicle_type,
            "vehicle_number": person.vehicle_number,
            "license_number": getattr(person, "license_number", None),
            "is_active": person.is_active,
            "is_available": getattr(person, "is_available", True),
            "current_location_lat": person.current_location_lat,
            "current_location_lng": person.current_location_lng,
            "last_location_update": person.last_location_update.isoformat() if person.last_location_update else None,
        }

        # Add performance metrics
        data["total_deliveries"] = getattr(person, "total_deliveries", 0)
        data["successful_deliveries"] = getattr(person, "successful_deliveries", 0)
        data["success_rate"] = float(getattr(person, "success_rate", 0))
        data["average_rating"] = float(getattr(person, "average_rating", 0))
        data["current_active_deliveries"] = (
            current_active_deliveries
            if current_active_deliveries is not None
            else getattr(person, "current_active_deliveries", 0)
        )

        return data

    except Exception:
        # Fallback to basic serialization
        vehicle_type = person.vehicle_type.value if hasattr(person.vehicle_type, "value") else person.vehicle_type
        return {
            "id": person.id,
            "user_id": person.user_id,
            "full_name": person.full_name,
            "phone": person.phone,
            "vehicle_type": vehicle_type,
            "is_active": person.is_active,
            "current_active_deliveries": (
                current_active_deliveries
                if current_active_deliveries is not None
                else getattr(person, "current_active_deliveries", 0)
            ),
        }


def serialize_category_admin(category: ProductCategory) -> Dict[str, Any]:
    """
    Serialize product category for admin view

    Args:
        category: ProductCategory model instance

    Returns:
        Serialized category data for admin
    """
    try:
        data = {
            "id": category.id,
            "name": category.name,  # Raw name (default language)
            "description": category.description,  # Raw description
            "is_active": category.is_active,
            "sort_order": category.sort_order,
            "icon_url": category.icon_url,
            "created_at": category.created_at.isoformat() if category.created_at else None,
            "updated_at": category.updated_at.isoformat() if category.updated_at else None,
            # Translations
            "name_translations": category.get_all_translations("name"),
            "description_translations": category.get_all_translations("description"),
        }

        # Add computed fields if available (added by query)
        if hasattr(category, "product_count"):
            data["product_count"] = category.product_count

        return data

    except Exception as e:
        logging.error(f"Exception in serialize_category_admin: {e}")
        # Fallback to basic serialization
        return {"id": category.id, "name": category.name, "is_active": category.is_active}


def generate_admin_dashboard_data() -> Dict[str, Any]:
    """
    Generate admin dashboard data

    Returns:
        Admin dashboard data
    """
    # This would typically fetch real data from the database
    # For now, return placeholder data structure
    dashboard = AdminDashboardSchema(
        users={"total": 0, "new_today": 0, "new_this_week": 0, "active": 0},
        orders={"total": 0, "today": 0, "pending": 0, "revenue_today": 0, "revenue_month": 0},
        products={"total": 0, "low_stock": 0},
        delivery={"active_deliveries": 0, "failed_today": 0},
        subscriptions={"active": 0, "monthly_revenue": 0},
        system_health={"status": "healthy", "uptime": "99.9%", "response_time": "120ms", "error_rate": "0.01%"},
        recent_orders=[],
        recent_users=[],
        low_stock_alerts=[],
        pending_deliveries=[],
        generated_at=datetime.now(UTC),
    )

    return dashboard.model_dump()


class CustomerMapPinSchema(BaseModel):
    """One customer address pin for the admin map."""

    # populate_by_name=True lets model_validate accept the snake_case dicts the
    # service produces (without it, alias_generator=to_camel forces camelCase input).
    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel, populate_by_name=True)

    address_id: int
    user_id: int
    full_name: str
    phone: Optional[str] = None
    user_type: str
    entity_subtype: Optional[str] = None
    lat: float
    lng: float
    is_default: bool = False
    address_label: str = ""
    address_index: int = 1
    address_count: int = 1
    last_order_date: Optional[datetime] = None
    order_count: int = 0
    bottle_balance: MoneyFloat = Field(default=0)
    outstanding_debt: MoneyFloat = Field(default=0)
    active_cod_debt_count: int = 0
    cod_restricted: bool = False
    is_shared_place: bool = False
    place_member_count: int = 1


# Helper functions (would typically query the database)
def get_user_order_count(user_id: int) -> int:
    """Get total order count for user"""
    return 0


def get_user_total_spent(user_id: int) -> float:
    """Get total amount spent by user"""
    return 0.0


def get_user_average_order_value(user_id: int) -> float:
    """Get average order value for user"""
    return 0.0


def get_user_lifetime_value(user_id: int) -> float:
    """Get lifetime value of user"""
    return 0.0


def get_user_loyalty_points(user_id: int) -> int:
    """Get user's current loyalty points"""
    return 0


def get_user_referral_count(user_id: int) -> int:
    """Get count of successful referrals by user"""
    return 0


def get_active_deliveries_count(delivery_person_id: int) -> int:
    """Get count of active deliveries for delivery person"""
    return 0


def get_pending_deliveries_count(delivery_person_id: int) -> int:
    """Get count of pending deliveries for delivery person"""
    return 0
