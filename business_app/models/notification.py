from datetime import datetime, timedelta, UTC
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, Text, ForeignKey, Enum, JSON, Index
from sqlalchemy.orm import relationship, backref
from sqlalchemy.ext.hybrid import hybrid_property
from werkzeug.security import generate_password_hash, check_password_hash
from flask_sqlalchemy import SQLAlchemy
import uuid
from business_app import db
from business_app.models import TimestampMixin
from business_app.models.translatable import TranslatableMixin, translatable


class Notification(db.Model, TimestampMixin):
    __tablename__ = 'notifications'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    notification_type = Column(String(50), nullable=False, index=True)  # order_update, delivery_reminder, etc.
    channel = Column(String(20), nullable=False)  # sms, email, telegram, push
    
    # Content
    title = Column(String(255), nullable=False)
    message = Column(Text, nullable=False)
    
    # Delivery status
    is_sent = Column(Boolean, default=False, index=True)
    sent_at = Column(DateTime, nullable=True)
    delivery_status = Column(String(20), default='pending')  # pending, sent, delivered, failed
    failure_reason = Column(String(255), nullable=True)
    
    # Recipient details
    recipient_phone = Column(String(20), nullable=True)
    recipient_email = Column(String(120), nullable=True)
    recipient_telegram_id = Column(String(50), nullable=True)
    
    # Related entities
    order_id = Column(Integer, ForeignKey('orders.id'), nullable=True)
    delivery_id = Column(Integer, ForeignKey('deliveries.id'), nullable=True)
    
    # Scheduling
    scheduled_for = Column(DateTime, nullable=True)
    priority = Column(String(10), default='normal')  # low, normal, high, urgent
    
    # Additional data
    extra_data = Column(JSON, default={})
    
    user = relationship('User', back_populates='notifications')
    order = relationship('Order')
    delivery = relationship('Delivery')
    
    def mark_as_sent(self, status='sent'):
        """Mark notification as sent"""
        self.is_sent = True
        self.sent_at = datetime.now(UTC)
        self.delivery_status = status
    
    def mark_as_failed(self, reason):
        """Mark notification as failed"""
        self.delivery_status = 'failed'
        self.failure_reason = reason
    
    def to_dict(self):
        return {
            'id': self.id,
            'notification_type': self.notification_type,
            'channel': self.channel,
            'title': self.title,
            'message': self.message,
            'is_sent': self.is_sent,
            'sent_at': self.sent_at.isoformat() if self.sent_at else None,
            'delivery_status': self.delivery_status,
            'scheduled_for': self.scheduled_for.isoformat() if self.scheduled_for else None,
            'priority': self.priority,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }


@translatable('name', 'subject', 'content')
class NotificationTemplate(db.Model, TimestampMixin, TranslatableMixin):
    """Notification template for different types and channels"""
    __tablename__ = 'notification_templates'
    
    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)          # Default/fallback name (Uzbek)
    notification_type = Column(String(50), nullable=False)
    channel = Column(String(20), nullable=False)        # email, sms, push, in_app
    subject = Column(String(255), nullable=True)        # Default/fallback subject (Uzbek)
    content = Column(Text, nullable=False)              # Default/fallback content (Uzbek)
    is_active = Column(Boolean, default=True)
    
    def __repr__(self):
        return f'<NotificationTemplate {self.name}:{self.channel}>'
    
    def to_dict(self, language=None, include_all_translations=False):
        """Convert to dictionary with multilingual support"""
        return self.to_dict_multilingual(language, include_all_translations)


class NotificationPreference(db.Model, TimestampMixin):
    """User notification preferences"""
    __tablename__ = 'notification_preferences'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    notification_type = Column(String(50), nullable=False)
    channel = Column(String(20), nullable=False)  # email, sms, push, telegram
    is_enabled = Column(Boolean, default=True, nullable=False)
    
    def __repr__(self):
        return f'<NotificationPreference {self.user_id}:{self.notification_type}:{self.channel}>'
    
    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'notification_type': self.notification_type,
            'channel': self.channel,
            'is_enabled': self.is_enabled,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }


class PushNotificationToken(db.Model, TimestampMixin):
    """Push notification tokens for mobile devices"""
    __tablename__ = 'push_notification_tokens'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    token = Column(String(255), nullable=False, unique=True)
    
    # Device information
    platform = Column(String(10), nullable=False)  # ios, android, web
    device_id = Column(String(255), nullable=True)
    device_name = Column(String(100), nullable=True)
    app_version = Column(String(20), nullable=True)
    
    # Status
    is_active = Column(Boolean, default=True, index=True)
    last_used = Column(DateTime, nullable=True)
    
    # Relationships
    user = relationship('User')
    
    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'token': self.token,
            'platform': self.platform,
            'device_id': self.device_id,
            'device_name': self.device_name,
            'app_version': self.app_version,
            'is_active': self.is_active,
            'last_used': self.last_used.isoformat() if self.last_used else None,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }


@translatable('display_name', 'description')
class NotificationChannel(db.Model, TimestampMixin, TranslatableMixin):
    """Notification delivery channels configuration"""
    __tablename__ = 'notification_channels'
    
    id = Column(Integer, primary_key=True)
    name = Column(String(50), nullable=False, unique=True)
    display_name = Column(String(100), nullable=False)  # Default/fallback display name (Uzbek)
    description = Column(Text, nullable=True)           # Default/fallback description (Uzbek)
    
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
        result.update({
            'name': self.name,
            'is_active': self.is_active,
            'requires_confirmation': self.requires_confirmation,
            'rate_limit_per_hour': self.rate_limit_per_hour,
            'priority': self.priority,
            'provider_settings': self.provider_settings
        })
        
        return result
