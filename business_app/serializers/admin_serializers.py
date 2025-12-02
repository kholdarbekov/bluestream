"""
Admin Serializers for the Water Business Platform using Pydantic v2
This file contains Pydantic models for admin-related data serialization
"""
import logging
from datetime import datetime, date
from typing import Dict, Any, Optional, List, Union
from enum import Enum
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator, ConfigDict
from pydantic.alias_generators import to_camel
from business_app.models.product import Product
from business_app.models.order import Order


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
    
    # Statistics
    total_orders: int = Field(default=0)
    total_spent: Decimal = Field(default=0)
    average_order_value: Decimal = Field(default=0)
    lifetime_value: Decimal = Field(default=0)
    loyalty_points: int = Field(default=0)
    referral_count: int = Field(default=0)
    
    @field_validator('total_spent', 'average_order_value', 'lifetime_value')
    @classmethod
    def validate_amounts(cls, v):
        return float(v)


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
    total_amount: Decimal
    tax_amount: Decimal = Field(default=0)
    discount_amount: Decimal = Field(default=0)
    delivery_fee: Decimal = Field(default=0)
    payment_method: Optional[str] = None
    payment_status: Optional[str] = None
    delivery_date: Optional[datetime] = None
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
    
    @field_validator('total_amount', 'tax_amount', 'discount_amount', 'delivery_fee')
    @classmethod
    def validate_amounts(cls, v):
        return float(v)


class OrderListAdminSchema(BaseModel):
    """Order list admin schema"""
    orders: List[OrderAdminSchema]
    total: int
    page: int
    per_page: int
    pages: int
    total_revenue: Decimal = Field(default=0)
    filters_applied: Dict[str, Any] = Field(default_factory=dict)
    
    @field_validator('total_revenue')
    @classmethod
    def validate_total_revenue(cls, v):
        return float(v)


class ProductAdminSchema(BaseModel):
    """Product admin schema"""
    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)
    
    id: int
    name: str
    sku: str
    barcode: Optional[str] = None
    category_name: Optional[str] = None
    base_price: Decimal
    current_price: Decimal
    stock_quantity: Optional[int] = None
    min_stock_level: Optional[int] = None
    is_active: bool = Field(default=True)
    is_featured: bool = Field(default=False)
    track_inventory: bool = Field(default=True)
    
    # Performance metrics
    total_sold: int = Field(default=0)
    total_revenue: Decimal = Field(default=0)
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
    
    @field_validator('base_price', 'current_price', 'total_revenue')
    @classmethod
    def validate_amounts(cls, v):
        if v is not None:
            return float(v)
        return v


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
    monthly_earnings: Decimal = Field(default=0)
    
    # Status
    verification_status: str = Field(default="verified")
    background_check_status: str = Field(default="passed")
    
    @field_validator('monthly_earnings')
    @classmethod
    def validate_earnings(cls, v):
        return float(v)


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
    action: str = Field(..., pattern=r'^(activate|deactivate|suspend|delete|update_stock|send_email|export)$')
    target_type: str = Field(..., pattern=r'^(user|order|product|delivery)$')
    target_ids: List[int] = Field(..., min_items=1, max_items=1000)
    parameters: Dict[str, Any] = Field(default_factory=dict)
    reason: Optional[str] = Field(None, max_length=500)
    
    @field_validator('target_ids')
    @classmethod
    def validate_target_ids(cls, v):
        if len(v) != len(set(v)):
            raise ValueError('Target IDs must be unique')
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
    format_type: str = Field(default="pdf", pattern=r'^(pdf|excel|csv|json)$')
    is_scheduled: bool = Field(default=False)
    schedule_frequency: Optional[str] = Field(None, pattern=r'^(daily|weekly|monthly|quarterly)$')
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
    backup_type: str = Field(default="full", pattern=r'^(full|incremental|differential)$')
    frequency: str = Field(default="daily", pattern=r'^(hourly|daily|weekly|monthly)$')
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
    adjustment_type: str = Field(default="manual", pattern=r'^(manual|received|sold|damaged|returned)$')
    reason: Optional[str] = Field(None, max_length=500)


class SystemMaintenanceRequest(BaseModel):
    """System maintenance request"""
    maintenance_type: str = Field(..., pattern=r'^(scheduled|emergency|update)$')
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


# Export all schemas for easy importing
__all__ = [
    'UserAdminSchema',
    'UserListAdminSchema',
    'OrderAdminSchema',
    'OrderListAdminSchema',
    'ProductAdminSchema',
    'ProductListAdminSchema',
    'DeliveryPersonAdminSchema',
    'SystemSettingSchema',
    'AuditLogSchema',
    'AuditLogListSchema',
    'AdminDashboardSchema',
    'BulkActionRequestSchema',
    'BulkActionResultSchema',
    'ReportConfigSchema',
    'GeneratedReportSchema',
    'BackupConfigSchema',
    'NotificationTemplateAdminSchema',
    'UpdateUserStatusRequest',
    'UpdateOrderStatusRequest',
    'StockAdjustmentRequest',
    'SystemMaintenanceRequest',
    'AdminResponseSchema',
    'UserRole',
    'UserStatus',
    'OrderStatus',
    'AdminActionType',
    'ReportType'
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
            'id': user.id,
            'email': user.email,
            'phone': user.phone,
            'first_name': user.first_name,
            'last_name': user.last_name,
            'role': user.role.value if hasattr(user.role, 'value') else (user.role if user.role else 'customer'),
            'status': user.status.value if hasattr(user.status, 'value') else (user.status if user.status else 'active'),
            'email_verified': user.email_verified_at is not None,
            'phone_verified': user.phone_verified_at is not None,
            'created_at': user.created_at.isoformat() if user.created_at else None,
            'last_login': user.last_login.isoformat() if user.last_login else None,
            'last_activity_at': getattr(user, 'last_activity_at', None),
            'login_attempts': getattr(user, 'login_attempts', 0),
            'is_locked': getattr(user, 'is_locked', False),
            'locked_until': getattr(user, 'locked_until', None),
            'two_factor_enabled': getattr(user, 'two_factor_enabled', False),
            'registration_source': user.registration_source
        }
        
        if include_statistics:
            # Add user statistics (would typically come from database queries)
            data.update({
                'total_orders': get_user_order_count(user.id),
                'total_spent': get_user_total_spent(user.id),
                'average_order_value': get_user_average_order_value(user.id),
                'lifetime_value': get_user_lifetime_value(user.id),
                'loyalty_points': get_user_loyalty_points(user.id),
                'referral_count': get_user_referral_count(user.id)
            })
        
        return data
        
    except Exception:
        # Fallback to basic serialization
        return {
            'id': user.id,
            'email': user.email,
            'first_name': user.first_name,
            'last_name': user.last_name,
            'role': getattr(user, 'role', 'customer'),
            'status': getattr(user, 'status', 'active'),
            'created_at': user.created_at.isoformat() if user.created_at else None
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
        data = {
            'id': order.id,
            'order_number': order.order_number,
            'user_id': order.user_id,
            'status': order.status.value if order.status else None,
            'total_amount': float(order.total_amount),
            'tax_amount': float(getattr(order, 'tax_amount', 0)),
            'discount_amount': float(getattr(order, 'discount_amount', 0)),
            'delivery_fee': float(getattr(order, 'delivery_fee', 0)),
            'payment_method': getattr(order, 'payment_method', None),
            'payment_status': getattr(order, 'payment_status', None),
            'delivery_date': order.delivery_date.isoformat() if order.delivery_date else None,
            'delivery_address': order.delivery_address.to_dict() if getattr(order, 'delivery_address', None) else None,
            'special_instructions': getattr(order, 'special_instructions', None),
            'created_at': order.created_at.isoformat() if order.created_at else None,
            'updated_at': order.updated_at.isoformat() if order.updated_at else None
        }
        
        # Add customer information
        if order.user:
            data['customer_name'] = f"{order.user.first_name} {order.user.last_name}".strip()
            data['customer_email'] = order.user.email
            data['customer_phone'] = order.user.phone
        
        # Add order items summary
        if hasattr(order, 'order_items') and order.order_items:
            data['item_count'] = len(order.order_items)
            data['items_summary'] = [
                {
                    'product_name': item.product.name if item.product else 'Unknown',
                    'quantity': item.quantity,
                    'unit_price': float(item.unit_price),
                    'total_price': float(item.total_price)
                }
                for item in order.order_items[:5]  # Show first 5 items
            ]
        else:
            data['item_count'] = 0
            data['items_summary'] = []
        
        # Add delivery information
        if hasattr(order, 'delivery') and order.delivery:
            delivery = order.delivery
            data['tracking_number'] = delivery.tracking_number
            if delivery.delivery_person:
                data['delivery_person_name'] = delivery.delivery_person.full_name
        
        # Add admin-specific fields
        data['admin_notes'] = getattr(order, 'admin_notes', None)
        data['priority_level'] = getattr(order, 'priority_level', 'normal')
        
        return data
        
    except Exception as e:
        logging.error(f"Exception in serialize_order_admin: {e}")
        # Fallback to basic serialization
        return {
            'id': order.id,
            'order_number': order.order_number,
            'user_id': order.user_id,
            'status': getattr(order, 'status', 'pending'),
            'total_amount': float(order.total_amount),
            'created_at': order.created_at.isoformat() if order.created_at else None
        }


def serialize_product_admin(product: Product) -> Dict[str, Any]:
    """
    Serialize product for admin view
    
    Args:
        product: Product model instance
        
    Returns:
        Serialized product data for admin
    """
    try:
        data = {
            'id': product.id,
            'name': product.name,
            'sku': product.sku,
            'barcode': product.barcode,
            'base_price': float(product.base_price),
            'current_price': float(getattr(product, 'current_price', product.base_price)),
            'stock_quantity': product.stock_quantity if product.track_inventory else None,
            'min_stock_level': product.min_stock_level,
            'is_active': product.is_active,
            'is_featured': product.is_featured,
            'track_inventory': product.track_inventory,
            'created_at': product.created_at.isoformat() if product.created_at else None,
            'updated_at': product.updated_at.isoformat() if product.updated_at else None
        }
        
        # Add category information
        if product.category:
            data['category_name'] = product.category.name
        
        # Add performance metrics
        data['total_sold'] = getattr(product, 'total_sold', 0)
        data['total_revenue'] = float(getattr(product, 'total_revenue', 0))
        data['view_count'] = getattr(product, 'view_count', 0)
        data['average_rating'] = float(getattr(product, 'average_rating', 0))
        data['review_count'] = getattr(product, 'review_count', 0)
        
        # Determine stock status
        if not product.track_inventory:
            data['stock_status'] = 'not_tracked'
        elif product.stock_quantity == 0:
            data['stock_status'] = 'out_of_stock'
        elif product.stock_quantity <= (product.min_stock_level or 0):
            data['stock_status'] = 'low_stock'
        else:
            data['stock_status'] = 'in_stock'
        
        # Calculate days of stock (placeholder calculation)
        if product.track_inventory and product.stock_quantity:
            # This would typically use sales velocity data
            average_daily_sales = getattr(product, 'average_daily_sales', 1) or 1
            data['days_of_stock'] = int(product.stock_quantity / average_daily_sales)
        
        data['last_sold_at'] = getattr(product, 'last_sold_at', None)
        
        return data
        
    except Exception:
        # Fallback to basic serialization
        return {
            'id': product.id,
            'name': product.name,
            'sku': product.sku,
            'base_price': float(product.base_price),
            'stock_quantity': getattr(product, 'stock_quantity', None),
            'is_active': product.is_active,
            'created_at': product.created_at.isoformat() if product.created_at else None
        }


def serialize_delivery_person_admin(person) -> Dict[str, Any]:
    """
    Serialize delivery person for admin view
    
    Args:
        person: DeliveryPerson model instance
        
    Returns:
        Serialized delivery person data for admin
    """
    try:
        data = {
            'id': person.id,
            'full_name': person.full_name,
            'phone': person.phone,
            'email': getattr(person, 'email', None),
            'vehicle_type': person.vehicle_type.value if person.vehicle_type else None,
            'vehicle_number': person.vehicle_number,
            'license_number': getattr(person, 'license_number', None),
            'is_active': person.is_active,
            'is_available': getattr(person, 'is_available', True),
            'current_location_lat': person.current_location_lat,
            'current_location_lng': person.current_location_lng,
            'last_location_update': person.last_location_update.isoformat() if person.last_location_update else None
        }
        
        # Add performance metrics
        data['total_deliveries'] = getattr(person, 'total_deliveries', 0)
        data['successful_deliveries'] = getattr(person, 'successful_deliveries', 0)
        data['failed_deliveries'] = getattr(person, 'failed_deliveries', 0)
        
        # Calculate success rate
        total = data['total_deliveries']
        if total > 0:
            data['success_rate'] = round((data['successful_deliveries'] / total) * 100, 2)
        else:
            data['success_rate'] = 0.0
        
        data['average_rating'] = float(getattr(person, 'average_rating', 0))
        data['rating_count'] = getattr(person, 'rating_count', 0)
        data['average_delivery_time'] = getattr(person, 'average_delivery_time', None)
        data['on_time_percentage'] = getattr(person, 'on_time_percentage', 0)
        
        # Add current workload
        data['active_deliveries'] = get_active_deliveries_count(person.id)
        data['pending_deliveries'] = get_pending_deliveries_count(person.id)
        
        # Add admin fields
        data['hire_date'] = getattr(person, 'hire_date', None)
        data['employee_id'] = getattr(person, 'employee_id', None)
        data['emergency_contact'] = getattr(person, 'emergency_contact', None)
        data['monthly_earnings'] = float(getattr(person, 'monthly_earnings', 0))
        data['verification_status'] = getattr(person, 'verification_status', 'verified')
        data['background_check_status'] = getattr(person, 'background_check_status', 'passed')
        
        return data
        
    except Exception:
        # Fallback to basic serialization
        return {
            'id': person.id,
            'full_name': person.full_name,
            'phone': person.phone,
            'vehicle_type': getattr(person, 'vehicle_type', 'car'),
            'is_active': person.is_active
        }


def generate_admin_dashboard_data() -> Dict[str, Any]:
    """
    Generate admin dashboard data
    
    Returns:
        Admin dashboard data
    """
    # This would typically fetch real data from the database
    # For now, return placeholder data structure
    dashboard = AdminDashboardSchema(
        users={
            'total': 0,
            'new_today': 0,
            'new_this_week': 0,
            'active': 0
        },
        orders={
            'total': 0,
            'today': 0,
            'pending': 0,
            'revenue_today': 0,
            'revenue_month': 0
        },
        products={
            'total': 0,
            'low_stock': 0
        },
        delivery={
            'active_deliveries': 0,
            'failed_today': 0
        },
        subscriptions={
            'active': 0,
            'monthly_revenue': 0
        },
        system_health={
            'status': 'healthy',
            'uptime': '99.9%',
            'response_time': '120ms',
            'error_rate': '0.01%'
        },
        recent_orders=[],
        recent_users=[],
        low_stock_alerts=[],
        pending_deliveries=[],
        generated_at=datetime.now()
    )
    
    return dashboard.model_dump()


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