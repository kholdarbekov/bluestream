from datetime import datetime, UTC
from sqlalchemy import (
    Column,
    Integer,
    String,
    Boolean,
    DateTime,
    Text,
    ForeignKey,
    Enum,
    JSON,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship, backref
from business_app import db
from business_app.models import TimestampMixin
from business_app.models.translatable import TranslatableMixin, translatable
from business_app.utils.constants import NotificationChannel, NotificationStatus, Priority


class Notification(db.Model, TimestampMixin):
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    notification_type = Column(String(50), nullable=False, index=True)  # Note: schema uses VARCHAR, not the enum type
    channel = Column(
        Enum(NotificationChannel, name="notification_channel", values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )

    # Content
    title = Column(String(255), nullable=False)
    message = Column(Text, nullable=False)

    # Delivery status
    is_sent = Column(Boolean, default=False, index=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    delivery_status = Column(
        Enum(NotificationStatus, name="notification_status", values_callable=lambda x: [e.value for e in x]),
        default=NotificationStatus.PENDING,
    )
    failure_reason = Column(String(255), nullable=True)

    # Recipient details
    recipient_phone = Column(String(20), nullable=True)
    recipient_email = Column(String(120), nullable=True)
    recipient_telegram_id = Column(String(50), nullable=True)

    # Related entities
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=True)
    delivery_id = Column(Integer, ForeignKey("deliveries.id"), nullable=True)
    campaign_id = Column(Integer, ForeignKey("notification_campaigns.id"), nullable=True, index=True)

    # Scheduling
    scheduled_for = Column(DateTime(timezone=True), nullable=True)
    priority = Column(
        Enum(Priority, name="priority", values_callable=lambda x: [e.value for e in x]), default=Priority.NORMAL
    )

    # Additional data
    extra_data = Column(JSON, default={})

    user = relationship("User", back_populates="notifications")
    order = relationship("Order")
    delivery = relationship("Delivery")
    campaign = relationship("NotificationCampaign", back_populates="notifications")

    def mark_as_sent(self, status=NotificationStatus.SENT):
        """Mark notification as sent"""
        self.is_sent = True
        self.sent_at = datetime.now(UTC)
        self.delivery_status = status

    def mark_as_failed(self, reason):
        """Mark notification as failed"""
        self.delivery_status = NotificationStatus.FAILED
        self.failure_reason = reason

    def to_dict(self):
        return {
            "id": self.id,
            "notification_type": self.notification_type,
            "channel": self.channel.value if hasattr(self.channel, "value") else self.channel,
            "title": self.title,
            "message": self.message,
            "is_sent": self.is_sent,
            "sent_at": self.sent_at.isoformat() if self.sent_at else None,
            "delivery_status": (
                self.delivery_status.value if hasattr(self.delivery_status, "value") else self.delivery_status
            ),
            "scheduled_for": self.scheduled_for.isoformat() if self.scheduled_for else None,
            "priority": self.priority.value if hasattr(self.priority, "value") else self.priority,
            "campaign_id": self.campaign_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class NotificationCampaign(db.Model, TimestampMixin):
    """Admin-managed bulk notification campaign."""

    __tablename__ = "notification_campaigns"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    template_id = Column(Integer, ForeignKey("notification_templates.id"), nullable=True, index=True)
    notification_type = Column(String(50), nullable=False, index=True)
    channel = Column(String(20), nullable=False, index=True)
    subject_override = Column(String(255), nullable=True)
    content_override = Column(Text, nullable=True)
    target_audience = Column(String(50), nullable=False, default="all_customers", index=True)
    target_segment_id = Column(Integer, ForeignKey("user_segments.id"), nullable=True, index=True)
    specific_user_ids = Column(JSON, default=list)
    status = Column(String(20), nullable=False, default="draft", index=True)
    priority = Column(String(20), nullable=False, default=Priority.NORMAL.value)
    scheduled_at = Column(DateTime(timezone=True), nullable=True, index=True)
    queued_at = Column(DateTime(timezone=True), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    updated_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    celery_task_id = Column(String(255), nullable=True, index=True)
    recipient_count = Column(Integer, nullable=False, default=0)
    recipient_ids_snapshot = Column(JSON, default=list)
    last_error = Column(Text, nullable=True)

    template = relationship("NotificationTemplate", backref=backref("campaigns", lazy="dynamic"))
    target_segment = relationship("UserSegment")
    created_by = relationship("User", foreign_keys=[created_by_user_id])
    updated_by = relationship("User", foreign_keys=[updated_by_user_id])
    notifications = relationship("Notification", back_populates="campaign")

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "template_id": self.template_id,
            "notification_type": self.notification_type,
            "channel": self.channel,
            "subject_override": self.subject_override,
            "content_override": self.content_override,
            "target_audience": self.target_audience,
            "target_segment_id": self.target_segment_id,
            "specific_user_ids": list(self.specific_user_ids or []),
            "status": self.status,
            "priority": self.priority,
            "scheduled_at": self.scheduled_at.isoformat() if self.scheduled_at else None,
            "queued_at": self.queued_at.isoformat() if self.queued_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "cancelled_at": self.cancelled_at.isoformat() if self.cancelled_at else None,
            "created_by_user_id": self.created_by_user_id,
            "updated_by_user_id": self.updated_by_user_id,
            "celery_task_id": self.celery_task_id,
            "recipient_count": self.recipient_count,
            "recipient_ids_snapshot": list(self.recipient_ids_snapshot or []),
            "last_error": self.last_error,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


@translatable("name", "subject", "content")
class NotificationTemplate(db.Model, TimestampMixin, TranslatableMixin):
    """Notification template for different types and channels"""

    __tablename__ = "notification_templates"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)  # Default/fallback name (Uzbek)
    notification_type = Column(String(50), nullable=False)
    channel = Column(String(20), nullable=False)  # email, sms, push, in_app
    subject = Column(String(255), nullable=True)  # Default/fallback subject (Uzbek)
    content = Column(Text, nullable=False)  # Default/fallback content (Uzbek)
    is_active = Column(Boolean, default=True)

    def __repr__(self):
        return f"<NotificationTemplate {self.name}:{self.channel}>"

    def to_dict(self, language=None, include_all_translations=False):
        """Convert to dictionary with multilingual support"""
        return self.to_dict_multilingual(language, include_all_translations)


class NotificationPreference(db.Model, TimestampMixin):
    """User notification preferences"""

    __tablename__ = "notification_preferences"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "notification_type",
            "channel",
            name="uq_notification_preferences_user_type_channel",
        ),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    notification_type = Column(String(50), nullable=False)
    channel = Column(
        Enum(NotificationChannel, name="notification_channel", values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    is_enabled = Column(Boolean, default=True, nullable=False)

    def __repr__(self):
        return f"<NotificationPreference {self.user_id}:{self.notification_type}:{self.channel}>"

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "notification_type": self.notification_type,
            "channel": self.channel.value if hasattr(self.channel, "value") else self.channel,
            "is_enabled": self.is_enabled,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class PushNotificationToken(db.Model, TimestampMixin):
    """Push notification tokens for mobile devices"""

    __tablename__ = "push_notification_tokens"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    token = Column(String(255), nullable=False, unique=True)

    # Device information
    platform = Column(String(10), nullable=False)  # ios, android, web
    device_id = Column(String(255), nullable=True)
    device_name = Column(String(100), nullable=True)
    app_version = Column(String(20), nullable=True)

    # Status
    is_active = Column(Boolean, default=True, index=True)
    last_used = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    user = relationship("User")

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "token": self.token,
            "platform": self.platform,
            "device_id": self.device_id,
            "device_name": self.device_name,
            "app_version": self.app_version,
            "is_active": self.is_active,
            "last_used": self.last_used.isoformat() if self.last_used else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


@translatable("display_name", "description")
class NotificationChannel(db.Model, TimestampMixin, TranslatableMixin):
    """Notification delivery channels configuration"""

    __tablename__ = "notification_channels"

    id = Column(Integer, primary_key=True)
    name = Column(String(50), nullable=False, unique=True)
    display_name = Column(String(100), nullable=False)  # Default/fallback display name (Uzbek)
    description = Column(Text, nullable=True)  # Default/fallback description (Uzbek)

    # Channel configuration
    is_active = Column(Boolean, default=True)
    requires_confirmation = Column(Boolean, default=False)
    rate_limit_per_hour = Column(Integer, default=100)
    priority = Column(Integer, default=1)  # Higher number = higher priority

    # Provider settings (JSON)
    provider_settings = Column(JSON, default={})

    def to_dict(self, language=None, include_all_translations=False):
        """Convert to dictionary with multilingual support"""
        result = self.to_dict_multilingual(language, include_all_translations)

        # Add channel-specific fields
        result.update(
            {
                "name": self.name,
                "is_active": self.is_active,
                "requires_confirmation": self.requires_confirmation,
                "rate_limit_per_hour": self.rate_limit_per_hour,
                "priority": self.priority,
                "provider_settings": self.provider_settings,
            }
        )

        return result
