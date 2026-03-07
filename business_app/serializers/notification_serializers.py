"""
Notification Serializers for the Water Business Platform using Pydantic v2
This file contains Pydantic models for notification-related data serialization
"""
from datetime import datetime, UTC
from typing import Dict, Any, Optional, List
from enum import Enum

from pydantic import BaseModel, Field, field_validator, ConfigDict
from pydantic.alias_generators import to_camel


class NotificationType(str, Enum):
    EMAIL = "email"
    SMS = "sms"
    PUSH = "push"
    IN_APP = "in_app"
    TELEGRAM = "telegram"


class NotificationStatus(str, Enum):
    PENDING = "pending"
    SENT = "sent"
    DELIVERED = "delivered"
    FAILED = "failed"
    READ = "read"


class Priority(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class NotificationCategory(str, Enum):
    ORDER = "order"
    DELIVERY = "delivery"
    PAYMENT = "payment"
    PROMOTION = "promotion"
    SYSTEM = "system"
    LOYALTY = "loyalty"
    SECURITY = "security"
    REMINDER = "reminder"


class NotificationTemplateSchema(BaseModel):
    """Notification template schema"""
    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)
    
    id: int
    name: str
    description: Optional[str] = None
    category: NotificationCategory
    notification_type: NotificationType
    subject_template: Optional[str] = None
    body_template: str
    variables: List[str] = Field(default_factory=list)  # Template variables like {user_name}, {order_number}
    is_active: bool = Field(default=True)
    language: str = Field(default="uz")
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class NotificationPreferencesSchema(BaseModel):
    """User notification preferences schema"""
    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)
    
    user_id: int
    email_enabled: bool = Field(default=True)
    sms_enabled: bool = Field(default=True)
    push_enabled: bool = Field(default=True)
    in_app_enabled: bool = Field(default=True)
    telegram_enabled: bool = Field(default=False)
    delivery_telegram_status_updates_enabled: bool = Field(default=True)
    
    # Category-specific preferences
    order_notifications: bool = Field(default=True)
    delivery_notifications: bool = Field(default=True)
    payment_notifications: bool = Field(default=True)
    promotion_notifications: bool = Field(default=True)
    system_notifications: bool = Field(default=True)
    loyalty_notifications: bool = Field(default=True)
    security_notifications: bool = Field(default=True)
    reminder_notifications: bool = Field(default=True)
    
    # Quiet hours
    quiet_hours_enabled: bool = Field(default=False)
    quiet_hours_start: Optional[str] = None  # Format: "22:00"
    quiet_hours_end: Optional[str] = None    # Format: "08:00"
    
    # Frequency settings
    digest_enabled: bool = Field(default=False)
    digest_frequency: str = Field(default="weekly")  # daily, weekly, monthly
    
    updated_at: Optional[datetime] = None


class NotificationSchema(BaseModel):
    """Main notification schema"""
    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)
    
    id: int
    user_id: int
    notification_type: NotificationType
    category: NotificationCategory
    priority: Priority = Field(default=Priority.NORMAL)
    subject: Optional[str] = None
    message: str
    data: Optional[Dict[str, Any]] = None  # Additional data payload
    status: NotificationStatus = Field(default=NotificationStatus.PENDING)
    is_read: bool = Field(default=False)
    read_at: Optional[datetime] = None
    sent_at: Optional[datetime] = None
    delivered_at: Optional[datetime] = None
    failed_at: Optional[datetime] = None
    failure_reason: Optional[str] = None
    created_at: datetime
    expires_at: Optional[datetime] = None
    
    # Delivery information
    recipient_email: Optional[str] = None
    recipient_phone: Optional[str] = None
    recipient_push_token: Optional[str] = None
    recipient_telegram_id: Optional[str] = None
    
    # Template information
    template_id: Optional[int] = None
    template_variables: Optional[Dict[str, Any]] = None
    
    # Tracking
    tracking_id: Optional[str] = None
    provider_message_id: Optional[str] = None
    provider_response: Optional[Dict[str, Any]] = None


class NotificationListSchema(BaseModel):
    """Schema for notification list responses"""
    notifications: List[NotificationSchema]
    total: int
    unread_count: int
    page: int
    per_page: int
    pages: int


class BulkNotificationSchema(BaseModel):
    """Bulk notification schema"""
    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)
    
    id: int
    name: str
    description: Optional[str] = None
    notification_type: NotificationType
    category: NotificationCategory
    template_id: int
    target_users: str = Field(default="all")  # all, active, segment, specific
    user_segment_id: Optional[int] = None
    specific_user_ids: Optional[List[int]] = None
    subject: Optional[str] = None
    message: str
    template_variables: Optional[Dict[str, Any]] = None
    scheduled_at: Optional[datetime] = None
    status: str = Field(default="draft")  # draft, scheduled, sending, sent, failed
    total_recipients: int = Field(default=0)
    sent_count: int = Field(default=0)
    delivered_count: int = Field(default=0)
    failed_count: int = Field(default=0)
    created_at: datetime
    sent_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class NotificationAnalyticsSchema(BaseModel):
    """Notification analytics schema"""
    period: str
    total_sent: int = Field(default=0)
    total_delivered: int = Field(default=0)
    total_failed: int = Field(default=0)
    total_read: int = Field(default=0)
    delivery_rate: float = Field(default=0.0)
    open_rate: float = Field(default=0.0)
    failure_rate: float = Field(default=0.0)
    
    # By type
    email_stats: Dict[str, Any] = Field(default_factory=dict)
    sms_stats: Dict[str, Any] = Field(default_factory=dict)
    push_stats: Dict[str, Any] = Field(default_factory=dict)
    in_app_stats: Dict[str, Any] = Field(default_factory=dict)
    telegram_stats: Dict[str, Any] = Field(default_factory=dict)
    
    # By category
    category_stats: Dict[str, Any] = Field(default_factory=dict)
    
    # Trends
    daily_trend: List[Dict[str, Any]] = Field(default_factory=list)
    hourly_distribution: List[Dict[str, Any]] = Field(default_factory=list)


class SendNotificationRequest(BaseModel):
    """Send notification request schema"""
    user_id: Optional[int] = None
    user_ids: Optional[List[int]] = None
    notification_type: NotificationType
    category: NotificationCategory = Field(default=NotificationCategory.SYSTEM)
    priority: Priority = Field(default=Priority.NORMAL)
    subject: Optional[str] = None
    message: str = Field(..., min_length=1, max_length=5000)
    data: Optional[Dict[str, Any]] = None
    template_id: Optional[int] = None
    template_variables: Optional[Dict[str, Any]] = None
    scheduled_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    
    @field_validator('user_ids')
    @classmethod
    def validate_user_ids(cls, v):
        if v and len(v) > 1000:  # Limit bulk notifications
            raise ValueError('Maximum 1000 users per notification')
        return v


class CreateTemplateRequest(BaseModel):
    """Create notification template request"""
    name: str = Field(..., min_length=3, max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    category: NotificationCategory
    notification_type: NotificationType
    subject_template: Optional[str] = Field(None, max_length=200)
    body_template: str = Field(..., min_length=10, max_length=10000)
    variables: List[str] = Field(default_factory=list)
    language: str = Field(default="uz")


class UpdatePreferencesRequest(BaseModel):
    """Update notification preferences request"""
    email_enabled: Optional[bool] = None
    sms_enabled: Optional[bool] = None
    push_enabled: Optional[bool] = None
    in_app_enabled: Optional[bool] = None
    telegram_enabled: Optional[bool] = None
    
    # Category preferences
    order_notifications: Optional[bool] = None
    delivery_notifications: Optional[bool] = None
    payment_notifications: Optional[bool] = None
    promotion_notifications: Optional[bool] = None
    system_notifications: Optional[bool] = None
    loyalty_notifications: Optional[bool] = None
    security_notifications: Optional[bool] = None
    reminder_notifications: Optional[bool] = None
    
    # Quiet hours
    quiet_hours_enabled: Optional[bool] = None
    quiet_hours_start: Optional[str] = Field(None, pattern=r'^([01]?[0-9]|2[0-3]):[0-5][0-9]$')
    quiet_hours_end: Optional[str] = Field(None, pattern=r'^([01]?[0-9]|2[0-3]):[0-5][0-9]$')
    
    # Digest settings
    digest_enabled: Optional[bool] = None
    digest_frequency: Optional[str] = Field(None, pattern=r'^(daily|weekly|monthly)$')


class MarkAsReadRequest(BaseModel):
    """Mark notifications as read request"""
    notification_ids: List[int] = Field(..., min_items=1, max_items=100)


class BulkNotificationRequest(BaseModel):
    """Bulk notification request schema"""
    name: str = Field(..., min_length=3, max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    notification_type: NotificationType
    category: NotificationCategory = Field(default=NotificationCategory.PROMOTION)
    template_id: int
    target_users: str = Field(..., pattern=r'^(all|active|segment|specific)$')
    user_segment_id: Optional[int] = None
    specific_user_ids: Optional[List[int]] = None
    template_variables: Optional[Dict[str, Any]] = None
    scheduled_at: Optional[datetime] = None
    
    @field_validator('specific_user_ids')
    @classmethod
    def validate_specific_user_ids(cls, v, info):
        if info.data.get('target_users') == 'specific' and (not v or len(v) == 0):
            raise ValueError('specific_user_ids is required when target_users is "specific"')
        if v and len(v) > 10000:
            raise ValueError('Maximum 10000 users for bulk notifications')
        return v


class NotificationResponseSchema(BaseModel):
    """Standard notification response schema"""
    success: bool
    message: str
    notification: Optional[NotificationSchema] = None
    tracking_id: Optional[str] = None
    errors: Optional[List[str]] = None


# Export all schemas for easy importing
__all__ = [
    'NotificationSchema',
    'NotificationListSchema',
    'NotificationTemplateSchema',
    'NotificationPreferencesSchema',
    'BulkNotificationSchema',
    'NotificationAnalyticsSchema',
    'SendNotificationRequest',
    'CreateTemplateRequest',
    'UpdatePreferencesRequest',
    'MarkAsReadRequest',
    'BulkNotificationRequest',
    'NotificationResponseSchema',
    'NotificationType',
    'NotificationStatus',
    'Priority',
    'NotificationCategory'
]


def serialize_notification(notification, include_sensitive: bool = False) -> Dict[str, Any]:
    """
    Serialize notification to dictionary using Pydantic
    
    Args:
        notification: Notification model instance
        include_sensitive: Whether to include sensitive information
        
    Returns:
        Serialized notification data
    """
    try:
        data = {
            'id': notification.id,
            'user_id': notification.user_id,
            'notification_type': notification.notification_type.value if notification.notification_type else None,
            'category': notification.category.value if notification.category else None,
            'priority': notification.priority.value if notification.priority else 'normal',
            'subject': notification.subject,
            'message': notification.message,
            'data': notification.data,
            'status': notification.status.value if notification.status else 'pending',
            'is_read': notification.is_read,
            'read_at': notification.read_at.isoformat() if notification.read_at else None,
            'sent_at': notification.sent_at.isoformat() if notification.sent_at else None,
            'delivered_at': notification.delivered_at.isoformat() if notification.delivered_at else None,
            'created_at': notification.created_at.isoformat() if notification.created_at else None,
            'expires_at': notification.expires_at.isoformat() if notification.expires_at else None
        }
        
        # Add failure information if failed
        if notification.status and notification.status.value == 'failed':
            data['failed_at'] = notification.failed_at.isoformat() if notification.failed_at else None
            data['failure_reason'] = notification.failure_reason
        
        # Add tracking information
        if notification.tracking_id:
            data['tracking_id'] = notification.tracking_id
        
        # Add template information
        if notification.template_id:
            data['template_id'] = notification.template_id
            if include_sensitive:
                data['template_variables'] = notification.template_variables
        
        # Add sensitive provider information for admin/debug
        if include_sensitive:
            data['provider_message_id'] = notification.provider_message_id
            data['provider_response'] = notification.provider_response
            data['recipient_email'] = notification.recipient_email
            data['recipient_phone'] = notification.recipient_phone
        
        return data
        
    except Exception:
        # Fallback to basic serialization
        return {
            'id': notification.id,
            'user_id': notification.user_id,
            'notification_type': getattr(notification, 'notification_type', 'in_app'),
            'category': getattr(notification, 'category', 'system'),
            'message': notification.message,
            'status': getattr(notification, 'status', 'pending'),
            'is_read': getattr(notification, 'is_read', False),
            'created_at': notification.created_at.isoformat() if notification.created_at else None
        }


def serialize_notification_list(notifications: List, include_sensitive: bool = False) -> List[Dict[str, Any]]:
    """Serialize a list of notifications"""
    return [serialize_notification(notification, include_sensitive) for notification in notifications]


def serialize_notification_template(template) -> Dict[str, Any]:
    """
    Serialize notification template to dictionary
    
    Args:
        template: NotificationTemplate model instance
        
    Returns:
        Serialized template data
    """
    try:
        return {
            'id': template.id,
            'name': template.name,
            'description': template.description,
            'category': template.category.value if template.category else None,
            'notification_type': template.notification_type.value if template.notification_type else None,
            'subject_template': template.subject_template,
            'body_template': template.body_template,
            'variables': template.variables or [],
            'is_active': template.is_active,
            'language': template.language,
            'created_at': template.created_at.isoformat() if template.created_at else None,
            'updated_at': template.updated_at.isoformat() if template.updated_at else None
        }
        
    except Exception:
        # Fallback to basic serialization
        return {
            'id': template.id,
            'name': template.name,
            'body_template': template.body_template,
            'is_active': getattr(template, 'is_active', True),
            'language': getattr(template, 'language', 'uz')
        }


def serialize_notification_preferences(preferences) -> Dict[str, Any]:
    """
    Serialize notification preferences to dictionary
    
    Args:
        preferences: NotificationPreferences model instance
        
    Returns:
        Serialized preferences data
    """
    try:
        return {
            'user_id': preferences.user_id,
            'email_enabled': preferences.email_enabled,
            'sms_enabled': preferences.sms_enabled,
            'push_enabled': preferences.push_enabled,
            'in_app_enabled': preferences.in_app_enabled,
            'telegram_enabled': preferences.telegram_enabled,
            'delivery_telegram_status_updates_enabled': getattr(
                preferences,
                'delivery_telegram_status_updates_enabled',
                True,
            ),
            
            # Category preferences
            'order_notifications': preferences.order_notifications,
            'delivery_notifications': preferences.delivery_notifications,
            'payment_notifications': preferences.payment_notifications,
            'promotion_notifications': preferences.promotion_notifications,
            'system_notifications': preferences.system_notifications,
            'loyalty_notifications': preferences.loyalty_notifications,
            'security_notifications': preferences.security_notifications,
            'reminder_notifications': preferences.reminder_notifications,
            
            # Quiet hours
            'quiet_hours_enabled': preferences.quiet_hours_enabled,
            'quiet_hours_start': preferences.quiet_hours_start,
            'quiet_hours_end': preferences.quiet_hours_end,
            
            # Digest settings
            'digest_enabled': preferences.digest_enabled,
            'digest_frequency': preferences.digest_frequency,
            
            'updated_at': preferences.updated_at.isoformat() if preferences.updated_at else None
        }
        
    except Exception:
        # Fallback to basic serialization
        return {
            'user_id': preferences.user_id,
            'email_enabled': getattr(preferences, 'email_enabled', True),
            'sms_enabled': getattr(preferences, 'sms_enabled', True),
            'push_enabled': getattr(preferences, 'push_enabled', True),
            'in_app_enabled': getattr(preferences, 'in_app_enabled', True),
            'telegram_enabled': getattr(preferences, 'telegram_enabled', False),
            'delivery_telegram_status_updates_enabled': getattr(
                preferences,
                'delivery_telegram_status_updates_enabled',
                True,
            ),
        }


def serialize_bulk_notification(bulk_notification) -> Dict[str, Any]:
    """
    Serialize bulk notification to dictionary
    
    Args:
        bulk_notification: BulkNotification model instance
        
    Returns:
        Serialized bulk notification data
    """
    try:
        data = {
            'id': bulk_notification.id,
            'name': bulk_notification.name,
            'description': bulk_notification.description,
            'notification_type': bulk_notification.notification_type.value if bulk_notification.notification_type else None,
            'category': bulk_notification.category.value if bulk_notification.category else None,
            'template_id': bulk_notification.template_id,
            'target_users': bulk_notification.target_users,
            'user_segment_id': bulk_notification.user_segment_id,
            'subject': bulk_notification.subject,
            'message': bulk_notification.message,
            'scheduled_at': bulk_notification.scheduled_at.isoformat() if bulk_notification.scheduled_at else None,
            'status': bulk_notification.status,
            'total_recipients': bulk_notification.total_recipients or 0,
            'sent_count': bulk_notification.sent_count or 0,
            'delivered_count': bulk_notification.delivered_count or 0,
            'failed_count': bulk_notification.failed_count or 0,
            'created_at': bulk_notification.created_at.isoformat() if bulk_notification.created_at else None,
            'sent_at': bulk_notification.sent_at.isoformat() if bulk_notification.sent_at else None,
            'completed_at': bulk_notification.completed_at.isoformat() if bulk_notification.completed_at else None
        }
        
        # Calculate completion percentage
        if bulk_notification.total_recipients and bulk_notification.total_recipients > 0:
            processed = (bulk_notification.sent_count or 0) + (bulk_notification.failed_count or 0)
            data['completion_percentage'] = min(100, round((processed / bulk_notification.total_recipients) * 100, 1))
        else:
            data['completion_percentage'] = 0
        
        # Calculate delivery rate
        if bulk_notification.sent_count and bulk_notification.sent_count > 0:
            data['delivery_rate'] = round(((bulk_notification.delivered_count or 0) / bulk_notification.sent_count) * 100, 1)
        else:
            data['delivery_rate'] = 0
        
        return data
        
    except Exception:
        # Fallback to basic serialization
        return {
            'id': bulk_notification.id,
            'name': bulk_notification.name,
            'status': getattr(bulk_notification, 'status', 'draft'),
            'total_recipients': getattr(bulk_notification, 'total_recipients', 0),
            'sent_count': getattr(bulk_notification, 'sent_count', 0)
        }


def get_notification_analytics(period: str = 'last_30_days') -> Dict[str, Any]:
    """
    Get notification analytics data
    
    Args:
        period: Analytics period
        
    Returns:
        Analytics data
    """
    # This would typically aggregate data from the database
    # For now, return placeholder analytics
    return {
        'period': period,
        'total_sent': 0,
        'total_delivered': 0,
        'total_failed': 0,
        'total_read': 0,
        'delivery_rate': 0.0,
        'open_rate': 0.0,
        'failure_rate': 0.0,
        'email_stats': {'sent': 0, 'delivered': 0, 'failed': 0, 'opened': 0},
        'sms_stats': {'sent': 0, 'delivered': 0, 'failed': 0},
        'push_stats': {'sent': 0, 'delivered': 0, 'failed': 0, 'opened': 0},
        'in_app_stats': {'sent': 0, 'read': 0},
        'telegram_stats': {'sent': 0, 'delivered': 0, 'failed': 0},
        'category_stats': {},
        'daily_trend': [],
        'hourly_distribution': []
    }


def validate_notification_template(template_body: str, variables: List[str]) -> Dict[str, Any]:
    """
    Validate notification template syntax and variables
    
    Args:
        template_body: Template body content
        variables: List of available variables
        
    Returns:
        Validation result
    """
    import re
    
    # Find all template variables in the body
    found_variables = re.findall(r'\{(\w+)\}', template_body)
    
    # Check for undefined variables
    undefined_vars = [var for var in found_variables if var not in variables]
    
    # Check for unused variables
    unused_vars = [var for var in variables if var not in found_variables]
    
    is_valid = len(undefined_vars) == 0
    
    return {
        'is_valid': is_valid,
        'found_variables': found_variables,
        'undefined_variables': undefined_vars,
        'unused_variables': unused_vars,
        'errors': [f"Undefined variable: {var}" for var in undefined_vars],
        'warnings': [f"Unused variable: {var}" for var in unused_vars]
    }


def is_in_quiet_hours(preferences, current_time: datetime = None) -> bool:
    """
    Check if current time is within user's quiet hours
    
    Args:
        preferences: User notification preferences
        current_time: Current time (defaults to now)
        
    Returns:
        True if in quiet hours, False otherwise
    """
    if not preferences.quiet_hours_enabled:
        return False
    
    if not preferences.quiet_hours_start or not preferences.quiet_hours_end:
        return False
    
    if current_time is None:
        current_time = datetime.now(UTC)
    
    try:
        start_hour, start_minute = map(int, preferences.quiet_hours_start.split(':'))
        end_hour, end_minute = map(int, preferences.quiet_hours_end.split(':'))
        
        current_minute = current_time.hour * 60 + current_time.minute
        start_minute_total = start_hour * 60 + start_minute
        end_minute_total = end_hour * 60 + end_minute
        
        if start_minute_total <= end_minute_total:
            # Same day (e.g., 09:00 to 17:00)
            return start_minute_total <= current_minute <= end_minute_total
        else:
            # Spans midnight (e.g., 22:00 to 08:00)
            return current_minute >= start_minute_total or current_minute <= end_minute_total
            
    except (ValueError, AttributeError):
        return False
