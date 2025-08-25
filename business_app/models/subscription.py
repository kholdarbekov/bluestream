from datetime import datetime, timedelta, UTC
from decimal import Decimal
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, Text, ForeignKey, Enum, JSON, Index, Numeric
from sqlalchemy.orm import relationship, backref
from sqlalchemy.ext.hybrid import hybrid_property
from werkzeug.security import generate_password_hash, check_password_hash
from flask_sqlalchemy import SQLAlchemy
import uuid
from business_app import db
from business_app.utils.constants import SubscriptionStatus, PaymentMethod
from business_app.models import TimestampMixin
from business_app.models.translatable import TranslatableMixin, translatable


@translatable('name', 'description')
class Subscription(db.Model, TimestampMixin, TranslatableMixin):
    __tablename__ = 'subscriptions'
    
    id = Column(Integer, primary_key=True)
    subscription_number = Column(String(50), unique=True, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    status = Column(Enum(SubscriptionStatus), default=SubscriptionStatus.ACTIVE.value, index=True)
    
    # Subscription details
    name = Column(String(200), nullable=False)        # Default/fallback name (Uzbek)
    description = Column(Text, nullable=True)         # Default/fallback description (Uzbek)
    
    # Billing cycle
    billing_cycle = Column(String(20), nullable=False)  # daily, weekly, monthly
    billing_amount = Column(Numeric(precision=10, scale=2), nullable=False)
    next_billing_date = Column(DateTime, nullable=False)
    last_billing_date = Column(DateTime, nullable=True)
    
    # Delivery schedule
    delivery_frequency = Column(String(20), nullable=False)  # daily, weekly, monthly
    delivery_day_of_week = Column(Integer, nullable=True)  # 1=Monday, 7=Sunday
    delivery_day_of_month = Column(Integer, nullable=True)  # 1-31
    delivery_time_slot = Column(String(20), nullable=False)
    delivery_address_id = Column(Integer, ForeignKey('addresses.id'), nullable=False)
    
    # Subscription period
    start_date = Column(DateTime, nullable=False)
    end_date = Column(DateTime, nullable=True)  # null for indefinite
    auto_renew = Column(Boolean, default=True)
    
    # Payment settings
    payment_method = Column(Enum(PaymentMethod), nullable=False)
    auto_payment = Column(Boolean, default=True)
    
    # Pause/Resume functionality
    paused_at = Column(DateTime, nullable=True)
    pause_reason = Column(String(255), nullable=True)
    resume_date = Column(DateTime, nullable=True)
    
    # Analytics
    total_orders_generated = Column(Integer, default=0)
    total_amount_billed = Column(Numeric(precision=10, scale=2), default=Decimal('0.00'))
    failed_billing_attempts = Column(Integer, default=0)
    last_successful_billing = Column(DateTime, nullable=True)
    
    # Special features
    discount_percentage = Column(Float, default=0.0)  # Subscription discount
    loyalty_points_multiplier = Column(Float, default=1.0)  # Extra loyalty points
    
    # Relationships
    user = relationship('User', back_populates='subscriptions')
    delivery_address = relationship('UserAddress')
    subscription_items = relationship('SubscriptionItem', back_populates='subscription', cascade='all, delete-orphan')
    orders = relationship('Order', back_populates='subscription')
    payments = relationship('Payment', back_populates='subscription')
    subscription_logs = relationship('SubscriptionLog', back_populates='subscription', cascade='all, delete-orphan')
    
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if not self.subscription_number:
            self.generate_subscription_number()
    
    def generate_subscription_number(self):
        """Generate unique subscription number"""
        timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
        random_suffix = str(uuid.uuid4().hex[:4]).upper()
        self.subscription_number = f"SUB{timestamp}{random_suffix}"
    
    def calculate_next_billing_date(self):
        """Calculate next billing date based on billing cycle"""
        if self.billing_cycle == 'daily':
            return self.next_billing_date + timedelta(days=1)
        elif self.billing_cycle == 'weekly':
            return self.next_billing_date + timedelta(weeks=1)
        elif self.billing_cycle == 'monthly':
            # Handle month boundaries properly
            if self.next_billing_date.month == 12:
                return self.next_billing_date.replace(year=self.next_billing_date.year + 1, month=1)
            else:
                return self.next_billing_date.replace(month=self.next_billing_date.month + 1)
        return self.next_billing_date
    
    def calculate_next_delivery_date(self):
        """Calculate next delivery date based on delivery frequency"""
        today = datetime.now().date()
        
        if self.delivery_frequency == 'daily':
            return today + timedelta(days=1)
        elif self.delivery_frequency == 'weekly':
            days_ahead = self.delivery_day_of_week - today.weekday()
            if days_ahead <= 0:  # Target day already happened this week
                days_ahead += 7
            return today + timedelta(days=days_ahead)
        elif self.delivery_frequency == 'monthly':
            # Next month, same day
            if today.month == 12:
                next_month = today.replace(year=today.year + 1, month=1, day=self.delivery_day_of_month)
            else:
                try:
                    next_month = today.replace(month=today.month + 1, day=self.delivery_day_of_month)
                except ValueError:  # Day doesn't exist in next month
                    next_month = today.replace(month=today.month + 1, day=28)
            return next_month
        
        return today
    
    def pause(self, reason=None, resume_date=None):
        """Pause subscription"""
        self.status = SubscriptionStatus.PAUSED
        self.paused_at = datetime.now(UTC)
        self.pause_reason = reason
        self.resume_date = resume_date
        
        # Log the pause
        log = SubscriptionLog(
            subscription_id=self.id,
            action='paused',
            details=f"Reason: {reason}" if reason else "Subscription paused"
        )
        db.session.add(log)
    
    def resume(self):
        """Resume subscription"""
        self.status = SubscriptionStatus.ACTIVE
        self.paused_at = None
        self.pause_reason = None
        self.resume_date = None
        
        # Recalculate next billing date
        self.next_billing_date = self.calculate_next_billing_date()
        
        # Log the resume
        log = SubscriptionLog(
            subscription_id=self.id,
            action='resumed',
            details="Subscription resumed"
        )
        db.session.add(log)
    
    def cancel(self, reason=None):
        """Cancel subscription"""
        self.status = SubscriptionStatus.CANCELLED
        
        # Log the cancellation
        log = SubscriptionLog(
            subscription_id=self.id,
            action='cancelled',
            details=f"Reason: {reason}" if reason else "Subscription cancelled"
        )
        db.session.add(log)
    
    def get_total_value(self):
        """Calculate total subscription value per billing cycle"""
        total = sum(item.total_price for item in self.subscription_items)
        discount = total * (self.discount_percentage / 100)
        return total - discount
    
    def to_dict(self, language=None, include_all_translations=False):
        """Convert to dictionary with multilingual support"""
        result = self.to_dict_multilingual(language, include_all_translations)
        
        # Add subscription-specific fields
        result.update({
            'subscription_number': self.subscription_number,
            'status': self.status.value,
            'billing_cycle': self.billing_cycle,
            'billing_amount': float(self.billing_amount),
            'next_billing_date': self.next_billing_date.isoformat() if self.next_billing_date else None,
            'delivery_frequency': self.delivery_frequency,
            'delivery_time_slot': self.delivery_time_slot,
            'start_date': self.start_date.isoformat() if self.start_date else None,
            'end_date': self.end_date.isoformat() if self.end_date else None,
            'auto_renew': self.auto_renew,
            'auto_payment': self.auto_payment,
            'discount_percentage': self.discount_percentage,
            'total_orders_generated': self.total_orders_generated,
            'total_amount_billed': float(self.total_amount_billed),
            'subscription_items': [item.to_dict(language) for item in self.subscription_items],
            'delivery_address': self.delivery_address.to_dict() if self.delivery_address else None
        })
        
        return result

class SubscriptionItem(db.Model):
    __tablename__ = 'subscription_items'
    
    id = Column(Integer, primary_key=True)
    subscription_id = Column(Integer, ForeignKey('subscriptions.id'), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey('products.id'), nullable=False, index=True)
    quantity = Column(Integer, nullable=False)
    unit_price = Column(Numeric(precision=10, scale=2), nullable=False)
    total_price = Column(Numeric(precision=10, scale=2), nullable=False)
    
    # Product snapshot (in case product changes)
    product_name = Column(String(200), nullable=False)
    product_sku = Column(String(50), nullable=False)
    
    subscription = relationship('Subscription', back_populates='subscription_items')
    # Removed back_populates since Product model doesn't have subscription_items relationship
    # product = relationship('Product', back_populates='subscription_items')
    
    def calculate_total(self):
        """Calculate total price for this subscription item"""
        self.total_price = self.unit_price * self.quantity
        return self.total_price
    
    def to_dict(self, language=None):
        return {
            'id': self.id,
            'product_id': self.product_id,
            'product_name': self.product_name,
            'product_sku': self.product_sku,
            'quantity': self.quantity,
            'unit_price': float(self.unit_price),
            'total_price': float(self.total_price),
            'product': self.product.to_dict(language=language) if self.product else None
        }

class SubscriptionLog(db.Model, TimestampMixin):
    __tablename__ = 'subscription_logs'
    
    id = Column(Integer, primary_key=True)
    subscription_id = Column(Integer, ForeignKey('subscriptions.id'), nullable=False, index=True)
    action = Column(String(50), nullable=False)  # created, paused, resumed, cancelled, billed, etc.
    details = Column(Text, nullable=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=True)  # Who performed the action
    extra_data = Column(JSON, default={})
    
    subscription = relationship('Subscription', back_populates='subscription_logs')
    user = relationship('User')
    
    def to_dict(self):
        return {
            'id': self.id,
            'action': self.action,
            'details': self.details,
            'extra_data': self.extra_data,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'user': {
                'id': self.user.id,
                'name': self.user.full_name
            } if self.user else None
        }


@translatable('name', 'description')
class SubscriptionPlan(db.Model, TimestampMixin, TranslatableMixin):
    """Predefined subscription plans"""
    __tablename__ = 'subscription_plans'
    
    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)        # Default/fallback name (Uzbek)
    description = Column(Text, nullable=True)         # Default/fallback description (Uzbek)
    
    # Plan details
    price = Column(Numeric(precision=10, scale=2), nullable=False)
    billing_cycle = Column(String(20), nullable=False)  # daily, weekly, monthly
    delivery_frequency = Column(String(20), nullable=False)
    
    # Plan features
    features = Column(JSON, default=[])  # List of included features
    max_items_per_delivery = Column(Integer, nullable=True)
    free_delivery = Column(Boolean, default=False)
    discount_percentage = Column(Float, default=0.0)
    
    # Plan status
    is_active = Column(Boolean, default=True)
    is_popular = Column(Boolean, default=False)  # Mark popular plans
    sort_order = Column(Integer, default=0)
    
    # Restrictions
    minimum_commitment_months = Column(Integer, default=0)  # Minimum commitment period
    available_for_new_customers = Column(Boolean, default=True)
    available_for_existing_customers = Column(Boolean, default=True)
    
    def calculate_monthly_price(self):
        """Calculate equivalent monthly price for comparison"""
        if self.billing_cycle == 'monthly':
            return self.price
        elif self.billing_cycle == 'weekly':
            return self.price * 4.33  # Average weeks per month
        elif self.billing_cycle == 'daily':
            return self.price * 30
        else:
            return self.price
    
    def to_dict(self, language=None, include_all_translations=False):
        """Convert to dictionary with multilingual support"""
        result = self.to_dict_multilingual(language, include_all_translations)
        
        # Add plan-specific fields
        result.update({
            'price': float(self.price),
            'billing_cycle': self.billing_cycle,
            'delivery_frequency': self.delivery_frequency,
            'features': self.features,
            'max_items_per_delivery': self.max_items_per_delivery,
            'free_delivery': self.free_delivery,
            'discount_percentage': self.discount_percentage,
            'is_popular': self.is_popular,
            'monthly_equivalent_price': float(self.calculate_monthly_price()),
            'minimum_commitment_months': self.minimum_commitment_months,
            'available_for_new_customers': self.available_for_new_customers,
            'available_for_existing_customers': self.available_for_existing_customers
        })
        
        return result
