from datetime import datetime, timedelta, UTC
from decimal import Decimal
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, Text, ForeignKey, Enum, JSON, Index, Numeric
from sqlalchemy.orm import relationship, backref
from sqlalchemy.ext.hybrid import hybrid_property
from werkzeug.security import generate_password_hash, check_password_hash
from flask_sqlalchemy import SQLAlchemy
import uuid
from business_app import db
from business_app.utils.constants import PaymentMethod, PaymentStatus
from business_app.models import TimestampMixin


class Payment(db.Model, TimestampMixin):
    __tablename__ = 'payments'
    
    id = Column(Integer, primary_key=True)
    payment_id = Column(String(100), unique=True, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    order_id = Column(Integer, ForeignKey('orders.id'), nullable=True)
    subscription_id = Column(Integer, ForeignKey('subscriptions.id'), nullable=True)
    
    amount = Column(Numeric(precision=10, scale=2), nullable=False)
    currency = Column(String(3), default='UZS')
    payment_method = Column(Enum(PaymentMethod), nullable=False)
    status = Column(Enum(PaymentStatus), default=PaymentStatus.PENDING, index=True)
    
    # Payment provider specific data
    provider_transaction_id = Column(String(255), nullable=True, index=True)
    provider_data = Column(JSON, default={})
    
    # Payment link details (for Payme/Click)
    payment_link = Column(String(500), nullable=True)
    payment_link_expires_at = Column(DateTime, nullable=True)
    
    # Webhook processing
    webhook_processed = Column(Boolean, default=False)
    webhook_attempts = Column(Integer, default=0)
    
    # Metadata
    description = Column(String(255), nullable=True)
    callback_url = Column(String(500), nullable=True)
    failure_reason = Column(String(500), nullable=True)
    
    user = relationship('User', back_populates='payments')
    order = relationship('Order', back_populates='payments')
    subscription = relationship('Subscription', back_populates='payments')
    
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if not self.payment_id:
            self.payment_id = str(uuid.uuid4())
    
    def is_expired(self):
        """Check if payment link is expired"""
        if self.payment_link_expires_at:
            return datetime.now(UTC) > self.payment_link_expires_at
        return False
    
    def to_dict(self):
        return {
            'id': self.id,
            'payment_id': self.payment_id,
            'amount': self.amount,
            'currency': self.currency,
            'payment_method': self.payment_method.value,
            'status': self.status.value,
            'payment_link': self.payment_link,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'order_id': self.order_id,
            'subscription_id': self.subscription_id
        }


class PaymentTransaction(db.Model, TimestampMixin):
    """Track individual payment transactions and events"""
    __tablename__ = 'payment_transactions'
    
    id = Column(Integer, primary_key=True)
    payment_id = Column(Integer, ForeignKey('payments.id'), nullable=False, index=True)
    transaction_type = Column(String(50), nullable=False)  # charge, refund, capture, cancel
    
    # Transaction details
    amount = Column(Numeric(precision=10, scale=2), nullable=False)
    currency = Column(String(3), default='UZS')
    status = Column(String(20), nullable=False)  # success, failed, pending
    
    # External provider details
    provider_transaction_id = Column(String(255), nullable=True, index=True)
    provider_reference = Column(String(255), nullable=True)
    provider_response = Column(JSON, default={})
    
    # Transaction context
    initiated_by = Column(Integer, ForeignKey('users.id'), nullable=True)
    ip_address = Column(String(45), nullable=True)
    user_agent = Column(String(500), nullable=True)
    
    # Result details
    success = Column(Boolean, nullable=False, default=False)
    failure_reason = Column(String(500), nullable=True)
    
    # Processing details
    processed_at = Column(DateTime, nullable=True)
    processing_time_ms = Column(Integer, nullable=True)
    
    # Additional data
    extra_data = Column(JSON, default={})
    notes = Column(Text, nullable=True)
    
    payment = relationship('Payment', backref='transactions')
    initiated_by_user = relationship('User', foreign_keys=[initiated_by])
    
    def to_dict(self):
        return {
            'id': self.id,
            'payment_id': self.payment_id,
            'transaction_type': self.transaction_type,
            'amount': self.amount,
            'currency': self.currency,
            'status': self.status,
            'provider_transaction_id': self.provider_transaction_id,
            'success': self.success,
            'failure_reason': self.failure_reason,
            'processed_at': self.processed_at.isoformat() if self.processed_at else None,
            'processing_time_ms': self.processing_time_ms,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'initiated_by': self.initiated_by,
            'notes': self.notes
        }


class CreditCard(db.Model, TimestampMixin):
    """User credit card information"""
    __tablename__ = 'credit_cards'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    
    # Card details (encrypted/tokenized)
    card_token = Column(String(255), unique=True, nullable=False, index=True)  # Provider token
    card_brand = Column(String(20), nullable=False)  # visa, mastercard, uzcard
    last_four_digits = Column(String(4), nullable=False)
    expiry_month = Column(Integer, nullable=False)
    expiry_year = Column(Integer, nullable=False)
    
    # Card holder info
    cardholder_name = Column(String(100), nullable=False)
    
    # Status and settings
    is_default = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    is_verified = Column(Boolean, default=False)
    
    # Provider info
    provider = Column(String(50), nullable=False)  # payme, click, uzcard
    provider_card_id = Column(String(255), nullable=True)
    
    # Security
    fingerprint = Column(String(100), nullable=True)  # Unique card fingerprint
    
    # Usage tracking
    last_used_at = Column(DateTime, nullable=True)
    usage_count = Column(Integer, default=0)
    
    user = relationship('User', backref='credit_cards')
    
    def is_expired(self):
        """Check if card is expired"""
        from datetime import datetime
        current_month = datetime.now().month
        current_year = datetime.now().year
        
        return (self.expiry_year < current_year) or \
               (self.expiry_year == current_year and self.expiry_month < current_month)
    
    def mask_card_number(self):
        """Return masked card number"""
        return f"****-****-****-{self.last_four_digits}"
    
    def to_dict(self):
        return {
            'id': self.id,
            'card_brand': self.card_brand,
            'last_four_digits': self.last_four_digits,
            'masked_number': self.mask_card_number(),
            'expiry_month': self.expiry_month,
            'expiry_year': self.expiry_year,
            'cardholder_name': self.cardholder_name,
            'is_default': self.is_default,
            'is_active': self.is_active,
            'is_verified': self.is_verified,
            'is_expired': self.is_expired(),
            'provider': self.provider,
            'last_used_at': self.last_used_at.isoformat() if self.last_used_at else None,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }