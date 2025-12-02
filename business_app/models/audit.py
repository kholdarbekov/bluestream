"""
Audit log model for tracking all system activities
"""
from enum import Enum
from business_app import db
from business_app.models import TimestampMixin


class AuditEventType(Enum):
    """Types of audit events for categorization."""

    # Authentication events
    LOGIN_SUCCESS = "login_success"
    LOGIN_FAILURE = "login_failure"
    LOGOUT = "logout"
    PASSWORD_CHANGE = "password_change"
    PASSWORD_RESET = "password_reset"

    # User management events
    USER_CREATED = "user_created"
    USER_UPDATED = "user_updated"
    USER_DELETED = "user_deleted"
    USER_ROLE_CHANGED = "user_role_changed"
    USER_STATUS_CHANGED = "user_status_changed"

    # Order management events
    ORDER_CREATED = "order_created"
    ORDER_UPDATED = "order_updated"
    ORDER_CANCELLED = "order_cancelled"
    ORDER_PROCESSED = "order_processed"
    ORDER_DELIVERED = "order_delivered"

    # Payment events
    PAYMENT_PROCESSED = "payment_processed"
    PAYMENT_REFUNDED = "payment_refunded"
    PAYMENT_FAILED = "payment_failed"

    # Product management events
    PRODUCT_CREATED = "product_created"
    PRODUCT_UPDATED = "product_updated"
    PRODUCT_DELETED = "product_deleted"
    INVENTORY_UPDATED = "inventory_updated"

    # System administration events
    SETTINGS_CHANGED = "settings_changed"
    SYSTEM_MAINTENANCE = "system_maintenance"
    DATA_EXPORT = "data_export"
    BULK_OPERATION = "bulk_operation"

    # Security events
    PERMISSION_DENIED = "permission_denied"
    SUSPICIOUS_ACTIVITY = "suspicious_activity"
    EMERGENCY_OPERATION = "emergency_operation"
    SENSITIVE_DATA_ACCESS = "sensitive_data_access"

    # API events
    API_KEY_CREATED = "api_key_created"
    API_KEY_REVOKED = "api_key_revoked"
    WEBHOOK_RECEIVED = "webhook_received"


class AuditSeverity(Enum):
    """Severity levels for audit events."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class AuditLog(db.Model, TimestampMixin):
    """Database model for audit logs."""

    __tablename__ = 'audit_logs'

    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.String(36), unique=True, nullable=False, index=True)
    event_type = db.Column(db.Enum(AuditEventType, values_callable=lambda x: [e.value for e in x]), nullable=False, index=True)
    severity = db.Column(db.Enum(AuditSeverity, values_callable=lambda x: [e.value for e in x]), nullable=False, index=True)

    # User context
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    user_role = db.Column(db.String(50), nullable=True)
    session_id = db.Column(db.String(255), nullable=True)

    # Request context
    ip_address = db.Column(db.String(45), nullable=True, index=True)
    user_agent = db.Column(db.Text, nullable=True)
    endpoint = db.Column(db.String(255), nullable=True, index=True)
    method = db.Column(db.String(10), nullable=True)

    # Event details
    resource_type = db.Column(db.String(100), nullable=True, index=True)
    resource_id = db.Column(db.String(100), nullable=True, index=True)
    action = db.Column(db.String(100), nullable=False, index=True)
    description = db.Column(db.Text, nullable=True)

    # Data changes
    old_values = db.Column(db.JSON, nullable=True)
    new_values = db.Column(db.JSON, nullable=True)

    # Metadata
    duration_ms = db.Column(db.Integer, nullable=True)
    success = db.Column(db.Boolean, nullable=False, default=True, index=True)
    error_message = db.Column(db.Text, nullable=True)
    additional_data = db.Column(db.JSON, nullable=True)

    def __repr__(self):
        return f'<AuditLog {self.event_id}: {self.event_type.value} by {self.user_id}>'

    def to_dict(self):
        return {
            'id': self.id,
            'event_id': self.event_id,
            'event_type': self.event_type.value,
            'severity': self.severity.value,
            'user_id': self.user_id,
            'user_role': self.user_role,
            'ip_address': self.ip_address,
            'endpoint': self.endpoint,
            'method': self.method,
            'resource_type': self.resource_type,
            'resource_id': self.resource_id,
            'action': self.action,
            'description': self.description,
            'old_values': self.old_values,
            'new_values': self.new_values,
            'duration_ms': self.duration_ms,
            'success': self.success,
            'error_message': self.error_message,
            'additional_data': self.additional_data,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }
